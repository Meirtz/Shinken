"""Docker-backed local sandbox provider."""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import json
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import subprocess
import tarfile
import threading
import time
import uuid
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactRef, FileScopeError, HashMismatch, sha256_file
from ..errors import SandboxDied
from .base import (
    GcReport,
    ProviderCapabilities,
    ProviderError,
    SandboxHandle,
    SandboxHealth,
    SandboxProvider,
    SandboxSpec,
    UnsatisfiedSandboxSpec,
)


def _free_port(host: str = "127.0.0.1") -> int:
    sock = socket.socket()
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process on THIS host (signal-0 probe). A pid we
    cannot signal (another user's) counts as alive — the conservative answer for
    ownership checks: never reclaim what might still be someone's session."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError etc. — the process exists
        return True
    return True


def _maybe_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _maybe_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


# Mask secret env values when rendering a command for an error/log (#153). Matches a
# `KEY=VALUE` arg whose KEY ends in TOKEN/SECRET/PASSWORD/KEY (e.g. SHINKEND_TOKEN).
_SECRET_ENV_RE = re.compile(r"^([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY))=.+$", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|KEY)$", re.IGNORECASE)


def _redact_cmd(cmd: list[str]) -> str:
    """Render a Docker command for error messages with secret env values masked (#153)
    — e.g. ``SHINKEND_TOKEN=deadbeef`` → ``SHINKEND_TOKEN=***`` — so a failing invocation
    never echoes the runtime token into an exception or log."""
    return " ".join(_SECRET_ENV_RE.sub(r"\1=***", arg) for arg in cmd)


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ProviderError(f"command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        raise ProviderError(f"{_redact_cmd(cmd)} failed: {stderr}") from exc


class DockerLocalProvider(SandboxProvider):
    """Run the reference Linux sandbox image with the Docker CLI."""

    capabilities = ProviderCapabilities(
        name="docker-local",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=True,
        supports_fork=True,
        supports_gpu=False,
        supports_vsock=False,
        supports_egress_policy=False,
        supports_checkpoint=True,
        supports_resume=True,
        reset_strategy="recreate",
        isolation="container",
        transport="tcp_ws",
        display="x11",
        snapshot_kind="disk",
        tier="local",
        max_sessions=None,
        notes=("Local/CI compatibility provider; Docker is not the production isolation tier.",),
    )

    _SNAPSHOT_RECORD_VERSION = 1
    _SNAPSHOT_RECORD_LABEL = "shinken.snapshot_record.v1"

    def __init__(
        self,
        image: str = "shinken/sandbox-linux",
        *,
        docker_bin: str = "docker",
        host: str = "127.0.0.1",
        name_prefix: str = "shinken-local",
        startup_timeout: float = 45.0,
        network_mode: str = "bridge",
        warm_pool_size: int = 0,
        warm_pool_spec: SandboxSpec | None = None,
        warm_pool_claim_timeout: float = 0.0,
        hostname: str | None = "shinken",
    ) -> None:
        self.image = image
        self.docker_bin = docker_bin
        self.host = host
        self.name_prefix = name_prefix
        self.startup_timeout = startup_timeout
        # Deterministic guest hostname (None = let Docker derive one from the container
        # id). A FIXED default is the fork-faithful posture: without it every replica
        # resumed from one checkpoint boots with a different hostname, so hostname-
        # derived guest state — the shell prompt painted in the xterm, $HOSTNAME baked
        # into files — silently diverges from the golden (and between replicas), which
        # both breaks checkpointed state that references the hostname and defeats
        # cross-replica observation dedup (near-identical screens differing only in the
        # prompt). Memory-tier forks (CRIU) clone the hostname anyway; the disk tier
        # should match.
        self.hostname = hostname
        # Guest network posture (#152). Default "bridge": the guest shares Docker's bridge
        # and HAS egress — this local reference provider is not an egress boundary (true
        # isolation needs the out-of-VM egress proxy, D6). "none" gives the guest no
        # network, but also removes the published-port host->shinkend WS path, so the
        # provider can't connect/health-check it — only for self-contained, unsupervised
        # runs. The actual mode is recorded in handle metadata so callers aren't misled.
        if network_mode not in ("bridge", "none"):
            raise ProviderError(
                f"unsupported network_mode {network_mode!r} (expected 'bridge' or 'none')"
            )
        self.network_mode = network_mode
        # These dictionaries are caches, not the source of truth. Every snapshot record
        # is persisted on the committed image and can repopulate them after provider
        # reconstruction (or in another process with access to the same Docker daemon).
        self._checkpoints: dict[str, dict[str, Any]] = {}
        # Snapshot registry: snapshot tag -> the SandboxSpec it was committed from, so
        # restore()/fork() can rebuild geometry + resource limits instead of silently
        # reverting to defaults. Also the reclamation set for cleanup_snapshots().
        self._snapshots: dict[str, SandboxSpec] = {}
        self._snapshot_records: dict[str, dict[str, Any]] = {}
        # Public snapshot id -> immutable Docker image id returned by `docker commit`.
        # Restore never trusts the mutable convenience tag once this is known.
        self._snapshot_images: dict[str, str] = {}
        # Legacy warm-pool storage remains so shutdown/introspection stay source-compatible,
        # but construction with a non-zero pool is rejected below. Immutable source deltas
        # alone are insufficient: grafting onto live guest writers is not equivalent to a
        # cold restore.
        self._deltas: dict[str, dict[str, Any]] = {}
        self._delta_dir: Path | None = None
        self._pool: queue.Queue[SandboxHandle] | None = None
        self._pool_target = max(0, int(warm_pool_size))
        if self._pool_target:
            raise ProviderError(
                "Docker warm-pool graft is disabled: a running target cannot yet be "
                "proven equivalent to a cold restore; use the CRIU tier for fast_reset"
            )
        self._pool_spec = warm_pool_spec or SandboxSpec()
        self._pool_claim_timeout = warm_pool_claim_timeout
        self._pool_stop = threading.Event()
        self._pool_thread: threading.Thread | None = None

    def connect(self, handle: SandboxHandle, **connect_kwargs):
        """Connect, then wire file transfer through the **actual guest** filesystem via
        `docker cp` (#154) instead of the host-local reference store, so `put_file`/
        `get_file` move bytes across the real Sandbox boundary. Extra keyword
        arguments pass through to :func:`shinken.connect` (see the base class)."""
        env = super().connect(handle, **connect_kwargs)
        container_id = handle.metadata.get("container_id") or handle.sandbox_id
        if container_id:
            env._set_guest_transport(DockerGuestTransport(str(container_id), self.docker_bin))
        return env

    # `_free_port` probes a port and closes it before `docker run` binds it — an
    # unavoidable TOCTOU window. Under concurrent create() bursts two sandboxes can race
    # for the same port; retry with a fresh port instead of failing the whole create.
    _PORT_RACE_MARKERS = ("port is already allocated", "address already in use")
    _CREATE_ATTEMPTS = 3

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        spec = (
            replace(spec, image=spec.image or self.image)
            if spec is not None
            else SandboxSpec(image=self.image)
        )
        self._validate_spec(spec)
        if self.network_mode == "none":
            raise ProviderError(
                "network_mode='none' cannot expose shinkend's tcp_ws transport; "
                "refusing before docker run instead of entering readiness retries"
            )
        for attempt in range(self._CREATE_ATTEMPTS):
            try:
                return self._create_once(spec)
            except ProviderError as exc:
                msg = str(exc).lower()
                lost_race = any(m in msg for m in self._PORT_RACE_MARKERS)
                # The documented residual ~1%-of-boots readiness flake (x11_up but the
                # root never paints before the deadline; benchmarks.md §9 caveat 5):
                # _create_once already destroyed the container, so one bounded retry
                # with a fresh container is safe and keeps large-N boot runs alive.
                boot_flake = "timed out waiting for ready sandbox" in msg
                if not (lost_race or boot_flake) or attempt == self._CREATE_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _fast_reset_supported(self) -> bool:
        """Whether this instance has an equivalence-preserving fast restore path."""
        return False

    def _validate_spec(self, spec: SandboxSpec) -> None:
        if spec.os != "linux":
            raise UnsatisfiedSandboxSpec(
                "os", spec.os, f"{self.capabilities.name} supports Linux sandboxes only"
            )
        if spec.needs_gui and not self.capabilities.supports_gui:
            raise UnsatisfiedSandboxSpec(
                "needs_gui", True, f"{self.capabilities.name} does not provide a GUI"
            )
        if spec.needs_gpu and not self.capabilities.supports_gpu:
            raise UnsatisfiedSandboxSpec(
                "needs_gpu", True, f"{self.capabilities.name} does not provide GPU passthrough"
            )
        if spec.state_fidelity not in ("filesystem", "process_memory"):
            raise UnsatisfiedSandboxSpec(
                "state_fidelity", spec.state_fidelity, "unknown state-fidelity contract"
            )
        if spec.state_fidelity == "process_memory" and self.capabilities.snapshot_kind != "process":
            raise UnsatisfiedSandboxSpec(
                "state_fidelity",
                spec.state_fidelity,
                f"{self.capabilities.name} snapshots filesystem state only",
            )
        if spec.fast_reset and not self._fast_reset_supported():
            raise UnsatisfiedSandboxSpec(
                "fast_reset",
                True,
                f"{self.capabilities.name} has no configured equivalence-preserving fast path",
            )

    def _create_once(self, spec: SandboxSpec) -> SandboxHandle:
        handle = self._launch_container(spec)
        try:
            self._wait_ready(handle)
        except Exception:
            self.destroy(handle)
            raise
        return handle

    def _tier_run_args(self, spec: SandboxSpec) -> list[str]:
        """Extra ``docker run`` flags a derived tier needs on EVERY container (e.g. the
        CRIU memory tier's ``--privileged``/``--init``/images-volume trio). Base: none."""
        return []

    def _launch_container(
        self,
        spec: SandboxSpec,
        *,
        command: tuple[str, ...] = (),
        token: str | None = None,
    ) -> SandboxHandle:
        """``docker run`` one sandbox container and return its handle WITHOUT waiting
        for readiness — callers gate readiness themselves (``create()`` immediately;
        the CRIU memory tier only after ``criu restore`` brings the process tree back).
        ``command`` overrides the image CMD (a memory-tier restore target must boot
        IDLE so nothing races the restored desktop for the display/port/PIDs);
        ``token`` reuses an existing runtime token (a restored ``shinkend`` keeps the
        token it held in memory at dump time)."""
        self._validate_spec(spec)
        if self.network_mode == "none":
            raise ProviderError(
                "network_mode='none' cannot expose shinkend's tcp_ws transport; "
                "refusing before docker run"
            )
        token = token or secrets.token_hex(16)
        port = _free_port(self.host)
        name = f"{self.name_prefix}-{uuid.uuid4().hex[:10]}"
        created_at = time.time()
        cmd = [
            self.docker_bin,
            "run",
            "-d",
            "--rm",
            "--network",
            self.network_mode,
            "--name",
            name,
            "--label",
            "shinken.provider=docker-local",
            "--label",
            f"shinken.name_prefix={self.name_prefix}",
            # Ownership stamp: which PROCESS created this sandbox and when — what makes
            # cleanup_orphans()/gc() owner-aware (a dead owner pid = a reclaimable
            # orphan; a live one is never touched without force).
            "--label",
            f"shinken.owner_pid={os.getpid()}",
            "--label",
            f"shinken.created_at={created_at}",
            "-e",
            # Dev-only: the token is delivered via env, so it is readable by any process
            # in the guest (and from /proc) — not a real boundary (#153). It is redacted
            # from provider errors/logs; a faithful boundary needs fd/mounted-secret
            # delivery plus the server-side Action Gateway (D6).
            f"SHINKEND_TOKEN={token}",
            "-e",
            f"SCREEN_GEOMETRY={spec.screen_geometry}",
            "-e",
            # The daemon now defaults arbitrary exec off. Provider-owned, isolated,
            # bearer-token-protected sandboxes are the explicit opt-in boundary.
            "SHINKEND_ENABLE_EXEC=1",
        ]
        if self.hostname:
            cmd += ["--hostname", self.hostname]
        # Caller-supplied guest env (SandboxSpec.extra_env), e.g. SHINKEND_DAMAGE=off.
        # Provider-reserved names stay authoritative: they are set above and the LAST
        # -e wins in docker, so reserved keys are skipped here instead of trusted.
        for key, value in (spec.extra_env or {}).items():
            if key in ("SHINKEND_TOKEN", "SCREEN_GEOMETRY", "SHINKEND_ENABLE_EXEC"):
                continue
            cmd += ["-e", f"{key}={value}"]
        # Publish the shinkend port only when the guest has a network; --network none is
        # incompatible with -p (and leaves the guest unreachable by the WS transport).
        if self.network_mode != "none":
            cmd += ["-p", f"{self.host}:{port}:8765"]
        if spec.memory:
            cmd += ["--memory", spec.memory]
        if spec.cpus is not None:
            cmd += ["--cpus", str(spec.cpus)]
        if spec.pids_limit is not None:
            cmd += ["--pids-limit", str(spec.pids_limit)]
        if spec.shm_size:
            cmd += ["--shm-size", spec.shm_size]
        cmd += self._tier_run_args(spec)
        cmd.append(spec.image or self.image)
        cmd += list(command)

        result = _run(cmd, timeout=self.startup_timeout)
        handle = SandboxHandle(
            provider=self.capabilities.name,
            sandbox_id=name,
            addr=f"{self.host}:{port}",
            token=token,
            created_at=created_at,
            metadata={
                **spec.metadata,
                "container_id": result.stdout.strip(),
                "image": spec.image or self.image,
                "port": port,
                "screen_geometry": spec.screen_geometry,
                # Honest egress posture (#152): record the actual network mode so callers
                # aren't misled — bridge means the guest HAS egress (not isolated here).
                "network_mode": self.network_mode,
                "guest_egress": self.network_mode != "none",
                "resources": {
                    "memory": spec.memory,
                    "cpus": spec.cpus,
                    "pids_limit": spec.pids_limit,
                    "shm_size": spec.shm_size,
                },
                "sandbox_spec": self._spec_to_record(spec),
            },
        )
        return handle

    def check_alive(self, handle: SandboxHandle) -> None:
        """Raise :class:`~shinken.errors.SandboxDied` (carrying the container's exit code and
        an OOM signal when applicable) if the container has exited/vanished; return if it is
        still running. Used to confirm whether a dropped session was real sandbox death."""
        cid = handle.metadata.get("container_id") or handle.sandbox_id
        if not cid:
            return
        try:
            result = subprocess.run(
                [
                    self.docker_bin,
                    "inspect",
                    "-f",
                    "{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}",
                    str(cid),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return  # cannot introspect — do not assert death
        out = result.stdout.strip()
        if result.returncode != 0 or not out:
            # `docker inspect` exits nonzero both when the container is genuinely gone AND
            # when the daemon is merely unreachable / permission-denied. Only the former is
            # confirmed death; the latter is "cannot introspect" (the base-class contract:
            # never assert death when we cannot see). Distinguish on stderr.
            err = (result.stderr or "").strip()
            if "no such" in err.lower() or "no such" in out.lower():
                raise SandboxDied(
                    f"sandbox {handle.sandbox_id} no longer exists", detail=err or out or None
                )
            return  # daemon down / transient / permissions — do not assert death
        parts = out.split()
        status = parts[0] if parts else "unknown"
        # `running` (and the transient `restarting`/`paused`) are not death — only an
        # exited/dead container is confirmed death.
        if status in ("running", "restarting", "paused", "created"):
            return
        exit_code = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else None
        oom = len(parts) > 2 and parts[2].lower() == "true"
        raise SandboxDied(
            f"sandbox {handle.sandbox_id} is not running (status={status})",
            exit_code=exit_code,
            signal=9 if oom else None,
            detail="OOMKilled" if oom else None,
        )

    # Readiness poll cadence (S8): 15 ms — fine enough that the SDK observes the guest
    # ready within ~one desktop paint, vs the legacy 200 ms loop whose every poll also
    # opened a fresh WS, pulled+decoded a FULL screenshot PNG in pure Python, and shelled
    # out to a blocking `docker stats` (seconds per poll).
    _READY_POLL_S = 0.015

    def _wait_ready(self, handle: SandboxHandle) -> None:
        """Block until the sandbox is usable — the cheap, push-shaped way (S8).

        Phase 1: connect-with-retry at ~15 ms granularity until ONE WebSocket session
        completes the ACI handshake (shinkend now listens within milliseconds of
        container start; the desktop boots behind it). Phase 2: poll the guest-side
        ``ready`` query on that SAME connection — answered in microseconds inside the
        guest from sampled root pixels. No full-PNG pulls, no host-side PNG decode, no
        ``docker stats`` on this hot path (``_container_rss`` remains for explicit
        ``health()`` calls only). A runtime that predates the ``ready`` query gets a
        same-connection screenshot probe at the legacy 200 ms cadence."""
        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        env = None
        try:
            while env is None:
                if time.monotonic() >= deadline:
                    raise ProviderError(
                        f"timed out waiting for ready sandbox {handle.addr}: {last_error}"
                    )
                try:
                    env = self.connect(handle)
                except Exception as exc:  # not listening yet / proxy up before guest
                    last_error = exc
                    time.sleep(self._READY_POLL_S)
            while time.monotonic() < deadline:
                try:
                    value = env.query("ready")
                except RuntimeError as exc:
                    if "unknown query" not in str(exc):
                        raise
                    # Old runtime: same-connection screenshot probe (still no fresh
                    # WS per poll and no `docker stats`).
                    self._legacy_ready_poll(env, handle, deadline)
                    return
                if isinstance(value, dict) and value.get("ready"):
                    return
                last_error = ProviderError(f"guest not ready: {value}")
                time.sleep(self._READY_POLL_S)
            raise ProviderError(f"timed out waiting for ready sandbox {handle.addr}: {last_error}")
        finally:
            if env is not None:
                env.close()

    def _legacy_ready_poll(self, env: Any, handle: SandboxHandle, deadline: float) -> None:
        """Readiness for a runtime without the ``ready`` query: poll a screenshot on the
        already-open session until it decodes non-black (or is undecodable — treated as
        ready, matching the legacy ``health()`` semantics)."""
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                shot = env.screenshot()
                if _png_has_non_black_pixel(shot["png"]) is not False:
                    return
                last_error = ProviderError("screenshot is all black")
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        raise ProviderError(f"timed out waiting for ready sandbox {handle.addr}: {last_error}")

    def health(self, handle: SandboxHandle) -> SandboxHealth:
        """Rich, EXPLICIT diagnostic: round-trip, full screenshot (+ host-side non-black
        decode) and container RSS via ``docker stats``. Deliberately heavyweight —
        seconds per call — and therefore no longer used by the boot readiness path
        (see :meth:`_wait_ready`, which polls the guest-side ``ready`` query instead)."""
        started = time.perf_counter()
        try:
            env = self.connect(handle)
            try:
                rtt_ms = env.ping() * 1000.0
                shot_started = time.perf_counter()
                shot = env.screenshot()
                screenshot_ms = (time.perf_counter() - shot_started) * 1000.0
            finally:
                env.close()
        except Exception as exc:
            return SandboxHealth(
                ok=False,
                ready=False,
                detail=str(exc),
                rss_bytes=self._container_rss(handle),
            )

        non_black = _png_has_non_black_pixel(shot["png"])
        ready = non_black is not False
        return SandboxHealth(
            ok=ready,
            ready=ready,
            detail="ready" if ready else "screenshot is all black",
            rtt_ms=rtt_ms,
            screenshot_ms=screenshot_ms,
            screenshot_bytes=len(shot["png"]),
            rss_bytes=self._container_rss(handle),
            metadata={
                "readiness_ms": (time.perf_counter() - started) * 1000.0,
                "screenshot_non_black": non_black,
            },
        )

    def reset(self, handle: SandboxHandle) -> SandboxHandle:
        spec = SandboxSpec(
            image=str(handle.metadata.get("image") or self.image),
            screen_geometry=str(handle.metadata.get("screen_geometry") or "1280x800x24"),
            metadata={
                k: v for k, v in handle.metadata.items() if k not in {"container_id", "port"}
            },
        )
        resources: dict[str, Any] = dict(handle.metadata.get("resources") or {})
        spec.memory = resources.get("memory")
        spec.cpus = resources.get("cpus")
        spec.pids_limit = resources.get("pids_limit")
        spec.shm_size = resources.get("shm_size")
        self.destroy(handle)
        return self.create(spec)

    def destroy(self, handle: SandboxHandle) -> None:
        try:
            subprocess.run(
                [self.docker_bin, "rm", "-f", handle.sandbox_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"timed out destroying sandbox {handle.sandbox_id}") from exc
        handle.metadata["destroyed"] = True
        # fork() records its intermediate commit on the child handle; reclaim it with
        # the child (the container is gone, so `docker rmi` actually frees the layer)
        # — fork no longer leaks one shinken-snap:* image per call.
        fork_snapshot = handle.metadata.get("fork_snapshot")
        if fork_snapshot:
            self.delete_snapshot(str(fork_snapshot))

    # --- runtime-state primitives (disk tier, #206) -----------------------------------
    # Reference implementation on Docker's filesystem layer via `docker commit` (no live
    # memory — that is the CRIU/process tier, and the sub-ms CoW fast tier is Phase-1).

    def _container_of(self, handle: SandboxHandle) -> str:
        cid = handle.metadata.get("container_id") or handle.sandbox_id
        if not cid:
            raise ProviderError("sandbox handle has no container id to snapshot")
        return str(cid)

    def _spec_from_handle(self, handle: SandboxHandle) -> SandboxSpec:
        """Rebuild the complete originating request from provider-owned metadata."""
        recorded = handle.metadata.get("sandbox_spec")
        if isinstance(recorded, dict):
            return self._spec_from_record(recorded)
        resources: dict[str, Any] = dict(handle.metadata.get("resources") or {})
        return SandboxSpec(
            image=str(handle.metadata.get("image") or self.image),
            screen_geometry=str(handle.metadata.get("screen_geometry") or "1280x800x24"),
            memory=resources.get("memory"),
            cpus=resources.get("cpus"),
            pids_limit=resources.get("pids_limit"),
            shm_size=resources.get("shm_size"),
            metadata={
                k: v
                for k, v in handle.metadata.items()
                # per-container state must not propagate into descendants: most
                # importantly fork_snapshot — inheriting it would let a grandchild's
                # destroy() reclaim an image its parent generation still needs.
                if k not in {"container_id", "port", "destroyed", "fork_snapshot", "pool_graft"}
            },
        )

    @classmethod
    def _label_safe_value(cls, value: Any) -> Any:
        """Return JSON-safe metadata without embedding secret-like field values."""
        if isinstance(value, dict):
            return {
                str(key): "<redacted>"
                if _SECRET_KEY_RE.search(str(key))
                else cls._label_safe_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [cls._label_safe_value(item) for item in value]
        if value is None or isinstance(value, str | int | float | bool):
            return value
        return f"<opaque:{type(value).__name__}>"

    @classmethod
    def _metadata_redactions(cls, value: Any, prefix: str = "") -> list[str]:
        """Paths whose exact values cannot be persisted safely in an image label."""
        if isinstance(value, dict):
            paths: list[str] = []
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if _SECRET_KEY_RE.search(str(key)):
                    paths.append(path)
                else:
                    paths.extend(cls._metadata_redactions(item, path))
            return paths
        if isinstance(value, list | tuple):
            paths = []
            for index, item in enumerate(value):
                path = f"{prefix}[{index}]" if prefix else f"[{index}]"
                paths.extend(cls._metadata_redactions(item, path))
            return paths
        if value is None or isinstance(value, str | int | float | bool):
            return []
        return [prefix or "<root>"]

    @classmethod
    def _spec_to_record(cls, spec: SandboxSpec, *, for_label: bool = False) -> dict[str, Any]:
        extra_env = dict(spec.extra_env or {})
        metadata: dict[str, Any] = dict(spec.metadata or {})
        redacted_env_keys: list[str] = []
        redacted_metadata_paths: list[str] = []
        if for_label:
            redacted_env_keys = [key for key in extra_env if _SECRET_KEY_RE.search(key)]
            extra_env = {
                key: value for key, value in extra_env.items() if key not in redacted_env_keys
            }
            redacted_metadata_paths = cls._metadata_redactions(metadata)
            metadata = cls._label_safe_value(metadata)
        return {
            "image": spec.image,
            "os": spec.os,
            "needs_gui": spec.needs_gui,
            "needs_gpu": spec.needs_gpu,
            "fast_reset": spec.fast_reset,
            "state_fidelity": spec.state_fidelity,
            "memory": spec.memory,
            "cpus": spec.cpus,
            "pids_limit": spec.pids_limit,
            "shm_size": spec.shm_size,
            "screen_geometry": spec.screen_geometry,
            "extra_env": extra_env,
            "redacted_extra_env_keys": redacted_env_keys,
            "redacted_metadata_paths": redacted_metadata_paths,
            "metadata": metadata,
        }

    @staticmethod
    def _spec_from_record(data: dict[str, Any]) -> SandboxSpec:
        metadata = dict(data.get("metadata") or {})
        redacted_env_keys = list(data.get("redacted_extra_env_keys") or [])
        redacted_metadata_paths = list(data.get("redacted_metadata_paths") or [])
        if redacted_env_keys:
            metadata["_shinken_unresolved_secret_env"] = redacted_env_keys
        if redacted_metadata_paths:
            metadata["_shinken_unresolved_metadata"] = redacted_metadata_paths
        return SandboxSpec(
            image=data.get("image"),
            os=str(data.get("os", "linux")),
            needs_gui=bool(data.get("needs_gui", True)),
            needs_gpu=bool(data.get("needs_gpu", False)),
            fast_reset=bool(data.get("fast_reset", False)),
            state_fidelity=str(data.get("state_fidelity", "filesystem")),
            memory=data.get("memory"),
            cpus=data.get("cpus"),
            pids_limit=data.get("pids_limit"),
            shm_size=data.get("shm_size"),
            screen_geometry=str(data.get("screen_geometry", "1280x800x24")),
            extra_env=dict(data.get("extra_env") or {}),
            metadata=metadata,
        )

    def _snapshot_record(
        self,
        *,
        snapshot_id: str,
        spec: SandboxSpec,
        name: str | None,
        checkpoint_id: str | None,
        event_seq: int | None,
        agent_state_ref: str | None,
        tier_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "version": self._SNAPSHOT_RECORD_VERSION,
            "provider": self.capabilities.name,
            "snapshot_kind": self.capabilities.snapshot_kind,
            "snapshot_id": snapshot_id,
            "checkpoint_id": checkpoint_id,
            "name": name,
            "event_seq": event_seq,
            "agent_state_ref": agent_state_ref,
            "spec": self._spec_to_record(spec, for_label=True),
            "tier_metadata": dict(tier_metadata or {}),
        }

    def _encode_snapshot_record(self, record: dict[str, Any]) -> str:
        try:
            raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"snapshot metadata is not JSON-serializable: {exc}") from exc
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def _decode_snapshot_record(self, encoded: str) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
            record = json.loads(raw)
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderError(f"invalid persisted snapshot record: {exc}") from exc
        if not isinstance(record, dict):
            raise ProviderError("invalid persisted snapshot record: expected an object")
        return record

    def _snapshot_commit_changes(self, record: dict[str, Any]) -> list[str]:
        labels = [
            "LABEL shinken.snapshot=true",
            f"LABEL shinken.provider={self.capabilities.name}",
            f"LABEL shinken.owner_pid={os.getpid()}",
            f"LABEL shinken.created_at={time.time()}",
            f"LABEL shinken.snapshot_id={record['snapshot_id']}",
            f"LABEL {self._SNAPSHOT_RECORD_LABEL}={self._encode_snapshot_record(record)}",
            # docker commit inherits parent image labels. Clear optional identity labels
            # first so a plain descendant snapshot cannot masquerade as its parent's
            # checkpoint/name during global label discovery.
            "LABEL shinken.snapshot_name=",
            "LABEL shinken.checkpoint_id=",
        ]
        checkpoint_id = record.get("checkpoint_id")
        if checkpoint_id:
            labels.append(f"LABEL shinken.checkpoint_id={checkpoint_id}")
        changes = ["-c", "ENV SHINKEND_TOKEN="]
        for key in record.get("spec", {}).get("redacted_extra_env_keys", []):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ProviderError(f"invalid secret environment key {key!r}")
            changes += ["-c", f"ENV {key}="]
        for label in labels:
            changes += ["-c", label]
        return changes

    def _remember_snapshot_record(
        self, record: dict[str, Any], *, image_ref: str | None = None
    ) -> None:
        if record.get("version") != self._SNAPSHOT_RECORD_VERSION:
            raise ProviderError(f"unsupported snapshot record version {record.get('version')!r}")
        if record.get("provider") != self.capabilities.name:
            raise ProviderError(
                f"snapshot belongs to provider {record.get('provider')!r}, "
                f"not {self.capabilities.name!r}"
            )
        if record.get("snapshot_kind") != self.capabilities.snapshot_kind:
            raise ProviderError(
                f"snapshot fidelity {record.get('snapshot_kind')!r} is incompatible with "
                f"{self.capabilities.snapshot_kind!r}"
            )
        snapshot_id = record.get("snapshot_id")
        spec_data = record.get("spec")
        if not isinstance(snapshot_id, str) or not isinstance(spec_data, dict):
            raise ProviderError("persisted snapshot record is missing snapshot_id/spec")
        spec = self._spec_from_record(spec_data)
        self._snapshots[snapshot_id] = spec
        self._snapshot_records[snapshot_id] = record
        if image_ref:
            self._snapshot_images[snapshot_id] = image_ref
        checkpoint_id = record.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id:
            self._checkpoints[checkpoint_id] = {
                "snapshot_id": snapshot_id,
                "event_seq": record.get("event_seq"),
                "agent_state_ref": record.get("agent_state_ref"),
                "name": record.get("name"),
            }
            self._snapshot_records[checkpoint_id] = record
        self._hydrate_tier_metadata(record)

    def _hydrate_tier_metadata(self, record: dict[str, Any]) -> None:
        """Hook for tiers whose persistent record has additional restore metadata."""

    def _inspect_snapshot_record(self, image: str) -> tuple[dict[str, Any] | None, str]:
        try:
            result = _run([self.docker_bin, "image", "inspect", image], timeout=30.0)
        except ProviderError:
            return None, image
        try:
            data = json.loads(result.stdout)
            item = data[0] if isinstance(data, list) else data
            labels = (item.get("Config") or {}).get("Labels") or {}
        except (AttributeError, IndexError, json.JSONDecodeError, TypeError):
            return None, image
        encoded = labels.get(self._SNAPSHOT_RECORD_LABEL)
        immutable_id = str(item.get("Id") or image)
        return (self._decode_snapshot_record(encoded) if encoded else None), immutable_id

    def _discover_labeled_image(self, label: str, value: str) -> str | None:
        try:
            result = _run(
                [
                    self.docker_bin,
                    "images",
                    "--filter",
                    f"label={label}={value}",
                    "--no-trunc",
                    "--format",
                    "{{.ID}}",
                ]
            )
        except ProviderError:
            return None
        return next(
            (line.strip() for line in result.stdout.splitlines() if line.strip()),
            None,
        )

    def _resolve_snapshot(
        self, snapshot_or_checkpoint_id: str
    ) -> tuple[str, str, dict[str, Any] | None]:
        key = str(snapshot_or_checkpoint_id)
        cached = self._checkpoints.get(key)
        if cached is not None:
            snapshot_key = str(cached["snapshot_id"])
            image_ref = self._snapshot_images.get(snapshot_key)
            if image_ref is None:
                image_ref = self._discover_labeled_image("shinken.snapshot_id", snapshot_key)
            if image_ref is None:
                raise ProviderError(f"snapshot image for checkpoint {key!r} is unavailable")
            return (
                snapshot_key,
                image_ref,
                self._snapshot_records.get(key) or self._snapshot_records.get(snapshot_key),
            )
        if key in self._snapshot_records:
            image_ref = self._snapshot_images.get(key)
            if image_ref is None:
                image_ref = self._discover_labeled_image("shinken.snapshot_id", key)
            if image_ref is None:
                raise ProviderError(f"snapshot image {key!r} is unavailable")
            return key, image_ref, self._snapshot_records[key]
        is_checkpoint = key.startswith("ckpt-")
        label = "shinken.checkpoint_id" if is_checkpoint else "shinken.snapshot_id"
        image_ref = self._discover_labeled_image(label, key)
        if image_ref is None and not is_checkpoint:
            image_ref = key  # legacy snapshots used the Docker image ref as their id
        if image_ref is None:
            raise ProviderError(f"unknown checkpoint {key!r}")
        record, immutable_id = self._inspect_snapshot_record(image_ref)
        if record is not None:
            if not immutable_id.startswith("sha256:"):
                raise ProviderError(
                    f"persisted snapshot resolved to non-immutable image ref {immutable_id!r}"
                )
            if is_checkpoint and record.get("checkpoint_id") != key:
                raise ProviderError(f"checkpoint label/record mismatch for {key!r}")
            if not is_checkpoint and record.get("snapshot_id") != key:
                raise ProviderError(f"snapshot label/record mismatch for {key!r}")
            self._remember_snapshot_record(record, image_ref=immutable_id)
            snapshot_key = str(record["snapshot_id"])
            return snapshot_key, immutable_id, record
        if is_checkpoint:
            raise ProviderError(f"checkpoint {key!r} has no persisted snapshot record")
        return key, immutable_id, None  # legacy image: Docker itself remains the authority

    @staticmethod
    def _lineage_metadata(
        snapshot_id: str, record: dict[str, Any] | None, requested_id: str
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"snapshot_id": snapshot_id}
        if record is not None:
            metadata.update(
                {
                    "checkpoint_id": record.get("checkpoint_id"),
                    "checkpoint_name": record.get("name"),
                    "event_seq": record.get("event_seq"),
                    "agent_state_ref": record.get("agent_state_ref"),
                    "snapshot_record_version": record.get("version"),
                }
            )
        if requested_id.startswith("ckpt-"):
            metadata["checkpoint_id"] = requested_id
        return metadata

    @staticmethod
    def _validate_replay_metadata(
        spec: SandboxSpec | None, *, process_memory: bool = False
    ) -> None:
        if spec is None:
            return
        unresolved_metadata = list(spec.metadata.get("_shinken_unresolved_metadata", []))
        if unresolved_metadata:
            raise UnsatisfiedSandboxSpec(
                "metadata",
                unresolved_metadata,
                "exact metadata was intentionally not persisted; restore requires "
                "caller-side reinjection",
            )
        unresolved_env = list(spec.metadata.get("_shinken_unresolved_secret_env", []))
        if unresolved_env and not process_memory:
            raise UnsatisfiedSandboxSpec(
                "extra_env",
                unresolved_env,
                "secret values are intentionally absent from the checkpoint image; "
                "restore requires caller-side secret reinjection",
            )

    def _take_snapshot(
        self,
        handle: SandboxHandle,
        *,
        name: str | None = None,
        checkpoint_id: str | None = None,
        event_seq: int | None = None,
        agent_state_ref: str | None = None,
    ) -> str:
        """Commit one immutable, UUID-addressed disk snapshot and its full record."""
        tag = f"shinken-snap:{uuid.uuid4().hex}"
        cid = self._container_of(handle)
        spec = self._spec_from_handle(handle)
        self._validate_spec(spec)
        record = self._snapshot_record(
            snapshot_id=tag,
            spec=spec,
            name=name,
            checkpoint_id=checkpoint_id,
            event_seq=event_seq,
            agent_state_ref=agent_state_ref,
        )
        commit = [
            self.docker_bin,
            "commit",
            *self._snapshot_commit_changes(record),
            cid,
            tag,
        ]
        committed = _run(commit, timeout=self.startup_timeout)
        image_ref = committed.stdout.strip().splitlines()[-1]
        if not image_ref.startswith("sha256:"):
            raise ProviderError(
                f"docker commit did not return an immutable sha256 image id: {image_ref!r}"
            )
        self._remember_snapshot_record(record, image_ref=image_ref)
        # The creating process can replay caller-provided secret env values without
        # persisting them. Reconstructed providers get an explicit unresolved marker.
        self._snapshots[tag] = spec
        if self._pool is not None:
            try:
                self._deltas[tag] = self._capture_delta(cid, image_ref)
            except ProviderError as exc:
                self._deltas.pop(tag, None)
                if spec.fast_reset:
                    self.delete_snapshot(tag)
                    raise UnsatisfiedSandboxSpec(
                        "fast_reset", True, f"immutable delta capture failed: {exc}"
                    ) from exc
        return tag

    def snapshot(self, handle: SandboxHandle, name: str | None = None) -> str:
        """Create a UUID-addressed disk snapshot; ``name`` is metadata only."""
        return self._take_snapshot(handle, name=name)

    def restore(self, snapshot_id: str) -> SandboxHandle:
        """Launch a fresh sandbox from a snapshot image (or a checkpoint id) — a new live
        container off the committed filesystem layer, at the geometry/limits captured when
        the snapshot was taken (not provider defaults). The unproven live warm-graft path
        is rejected at construction, so this path always restores the immutable image id."""
        snapshot_key, image_ref, record = self._resolve_snapshot(snapshot_id)
        spec = self._snapshots.get(snapshot_key)
        self._validate_replay_metadata(spec)
        lineage = self._lineage_metadata(snapshot_key, record, str(snapshot_id))
        pool_status = "disabled"
        if self._pool is not None:
            if self._deltas.get(snapshot_key) is None:
                pool_status = "no_immutable_delta"
            elif not self._pool_compatible(spec):
                pool_status = "incompatible"
            else:
                pool_status = "empty"
            handle = self._restore_from_pool(snapshot_key, spec)
            if handle is not None:
                restored_spec = replace(
                    spec,
                    image=image_ref,
                    metadata={**spec.metadata, **lineage},
                )
                self._apply_spec_metadata(handle, restored_spec)
                handle.metadata.update(
                    {**lineage, "restore_path": "warm_pool", "pool_status": "hit"}
                )
                return handle
        if spec is not None and spec.fast_reset:
            raise UnsatisfiedSandboxSpec(
                "fast_reset",
                True,
                f"warm-pool restore unavailable ({pool_status}); cold fallback is forbidden",
            )
        launch_spec = (
            replace(spec, image=image_ref, metadata={**spec.metadata, **lineage})
            if spec is not None
            else SandboxSpec(image=image_ref, metadata=lineage)
        )
        handle = self.create(launch_spec)
        handle.metadata.update({**lineage, "restore_path": "cold", "pool_status": pool_status})
        return handle

    def _apply_spec_metadata(self, handle: SandboxHandle, spec: SandboxSpec) -> None:
        """Make a claimed warm handle describe the state it now represents."""
        handle.metadata.update(spec.metadata)
        handle.metadata.update(
            {
                "image": spec.image or self.image,
                "screen_geometry": spec.screen_geometry,
                "resources": {
                    "memory": spec.memory,
                    "cpus": spec.cpus,
                    "pids_limit": spec.pids_limit,
                    "shm_size": spec.shm_size,
                },
                "sandbox_spec": self._spec_to_record(spec),
            }
        )

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Reclaim a committed snapshot image (`docker rmi`). Resolves a checkpoint id to
        its snapshot first. Idempotent and best-effort — a still-referenced image is left."""
        try:
            snapshot_key, image_ref, _record = self._resolve_snapshot(snapshot_id)
        except ProviderError:
            return
        subprocess.run(
            [self.docker_bin, "rmi", "-f", image_ref],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        self._snapshots.pop(snapshot_key, None)
        self._snapshot_records.pop(snapshot_key, None)
        self._snapshot_images.pop(snapshot_key, None)
        for checkpoint_id, checkpoint in list(self._checkpoints.items()):
            if checkpoint.get("snapshot_id") == snapshot_key:
                self._checkpoints.pop(checkpoint_id, None)
                self._snapshot_records.pop(checkpoint_id, None)
        delta = self._deltas.pop(snapshot_key, None)
        if delta and delta.get("tar"):
            with contextlib.suppress(OSError):
                os.remove(delta["tar"])

    def snapshot_spec(self, snapshot_or_checkpoint_id: str) -> SandboxSpec | None:
        """The originating :class:`SandboxSpec` recorded when this provider instance
        took the snapshot/checkpoint (geometry + resource limits — the spec-compat
        info a :class:`~shinken.Checkpoint` carries), or ``None`` when unknown."""
        snapshot_key, _image_ref, _record = self._resolve_snapshot(str(snapshot_or_checkpoint_id))
        return self._snapshots.get(snapshot_key)

    def cleanup_snapshots(self) -> int:
        """Reclaim the snapshot images THIS PROCESS committed (`docker rmi`): the
        in-memory registry plus — global-by-label — every image stamped
        ``shinken.snapshot=true`` with this process's ``shinken.owner_pid``, so a
        re-created provider instance (whose registry is empty) still reclaims its own
        process's commits. Returns the count removed. Cross-process reclamation (dead
        owners) is :meth:`gc` with ``snapshots=True``."""
        tags = set(self._snapshots)
        with contextlib.suppress(ProviderError):
            result = _run(
                [
                    self.docker_bin,
                    "images",
                    "--filter",
                    "label=shinken.snapshot=true",
                    "--filter",
                    f"label=shinken.owner_pid={os.getpid()}",
                    "--format",
                    "{{.Repository}}:{{.Tag}}",
                ]
            )
            tags |= {line.strip() for line in result.stdout.splitlines() if line.strip()}
        for tag in tags:
            self.delete_snapshot(tag)
        return len(tags)

    def resume(self, handle_or_checkpoint: SandboxHandle | str) -> SandboxHandle:
        """**Deprecated alias of** :meth:`restore` — kept for back-compat. RESTORE
        semantics, plainly: it launches a NEW live sandbox from the snapshot/checkpoint
        id; it does NOT un-pause anything, and calling it while the source sandbox is
        alive mints a SIBLING, not the same sandbox. Docker containers are ephemeral
        (`--rm`), so only snapshot/checkpoint ids are accepted, never a live handle."""
        if isinstance(handle_or_checkpoint, str):
            return self.restore(handle_or_checkpoint)
        raise ProviderError(
            "Docker resume needs a snapshot/checkpoint id "
            "(containers are ephemeral; snapshot() first)"
        )

    def fork(self, handle: SandboxHandle) -> SandboxHandle:
        """Branch a new live sandbox from the current state: snapshot + restore. Disk CoW
        via the new container's writable layer (~0.3–0.5 s) — not the sub-ms fast tier.

        The intermediate commit is recorded on the child handle (``fork_snapshot``
        metadata) and reclaimed by :meth:`destroy` along with the child — fork no
        longer leaks one ``shinken-snap:*`` image per call."""
        snapshot_id = self.snapshot(handle)
        child = self.restore(snapshot_id)
        child.metadata["fork_snapshot"] = snapshot_id
        return child

    def checkpoint(
        self,
        handle: SandboxHandle,
        *,
        name: str | None = None,
        event_seq: int | None = None,
        agent_state_ref: str | None = None,
    ) -> str:
        """Named restore point binding a disk snapshot to optional agent state — a node
        in the checkpoint DAG (D5). `resume(ckpt_id)` restores it. ``name`` labels the
        underlying snapshot image."""
        ckpt_id = f"ckpt-{uuid.uuid4().hex}"
        self._take_snapshot(
            handle,
            name=name,
            checkpoint_id=ckpt_id,
            event_seq=event_seq,
            agent_state_ref=agent_state_ref,
        )
        return ckpt_id

    def _labeled_container_ids(self) -> list[str]:
        """Ids of every container stamped with this provider's labels at create time
        (#157). Selecting strictly by label: Docker's `name` filter is substring-based,
        not the anchored regex `name=^prefix-` implies, so it could miss intended
        containers (or match unintended ones) and leave orphans; the two labels
        already identify our containers precisely."""
        result = _run(
            [
                self.docker_bin,
                "ps",
                "-aq",
                "--filter",
                "label=shinken.provider=docker-local",
                "--filter",
                f"label=shinken.name_prefix={self.name_prefix}",
            ]
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _ownership_rows(self, ids: list[str], *, image: bool = False) -> list[dict[str, Any]]:
        """``[{id, owner_pid, created_at}]`` for containers (or, with ``image=True``,
        images) — the ownership stamp read back from the labels. A missing label
        (an older SDK's resource) yields ``None`` for that field."""
        if not ids:
            return []
        fmt = (
            "{{.Id}}\t"
            '{{index .Config.Labels "shinken.owner_pid"}}\t'
            '{{index .Config.Labels "shinken.created_at"}}'
        )
        cmd = [self.docker_bin]
        if image:
            cmd.append("image")
        cmd += ["inspect", "-f", fmt, *ids]
        result = _run(cmd, timeout=30.0)
        rows: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            rows.append(
                {
                    "id": parts[0].strip(),
                    "owner_pid": _maybe_int(parts[1]),
                    "created_at": _maybe_float(parts[2]),
                }
            )
        return rows

    def cleanup_orphans(self, max_age_s: float | None = None) -> int:
        """Reclaim **orphaned** containers only — never a live owner's session.

        An orphan is a labeled container whose owning process (the
        ``shinken.owner_pid`` stamped at create) is dead, OR — when ``max_age_s`` is
        given, an explicit operator opt-in (e.g. a CI janitor) — one older than
        ``max_age_s`` seconds regardless of owner. With the default ``max_age_s=None``
        a live owner's sandboxes are NEVER touched, so two SDK processes sharing one
        Docker daemon can each clean up without killing the other's sessions.
        Containers without ownership labels (an older SDK) are only reclaimed by age.
        Returns the count removed. The old reclaim-everything-labeled sweep this
        method used to be is :meth:`destroy_all`."""
        now = time.time()
        doomed: list[str] = []
        for row in self._ownership_rows(self._labeled_container_ids()):
            pid, created = row["owner_pid"], row["created_at"]
            dead_owner = pid is not None and not _pid_alive(pid)
            expired = max_age_s is not None and created is not None and (now - created) > max_age_s
            if dead_owner or expired:
                doomed.append(row["id"])
        if doomed:
            _run([self.docker_bin, "rm", "-f", *doomed], timeout=60.0)
        return len(doomed)

    def destroy_all(self) -> int:
        """Force-remove EVERY container carrying this provider's labels — **including
        live sessions owned by other processes** (the blunt sweep
        :meth:`cleanup_orphans` used to be; renamed because a label sweep kills
        sibling runs). For routine cleanup prefer :meth:`cleanup_orphans` (owner-aware)
        or :meth:`gc`. Returns the count removed."""
        ids = self._labeled_container_ids()
        if not ids:
            return 0
        _run([self.docker_bin, "rm", "-f", *ids], timeout=30.0)
        return len(ids)

    def list(self) -> list[SandboxHandle]:
        """Rebuild a :class:`SandboxHandle` for every RUNNING labeled container, from
        Docker state alone (no in-memory registry needed — works across processes):
        the address from the published port map, ``created_at`` from the ownership
        label, and the token from the container's environment (``SHINKEND_TOKEN``).

        **Token recovery requires local Docker access**: the dev token is delivered
        via the container env (#153), so anyone who can `docker inspect` can read it —
        the same trust boundary as the env delivery itself; `list()` adds no new
        exposure (and :class:`SandboxHandle`'s repr redacts it)."""
        result = _run(
            [
                self.docker_bin,
                "ps",
                "-q",
                "--filter",
                "label=shinken.provider=docker-local",
                "--filter",
                f"label=shinken.name_prefix={self.name_prefix}",
            ]
        )
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return []
        inspected = _run([self.docker_bin, "inspect", *ids], timeout=30.0)
        try:
            data = json.loads(inspected.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"docker inspect returned unparseable JSON: {exc}") from exc
        handles: list[SandboxHandle] = []
        for item in data:
            config = item.get("Config") or {}
            labels = config.get("Labels") or {}
            env_list = config.get("Env") or []
            token = next(
                (e.split("=", 1)[1] for e in env_list if e.startswith("SHINKEND_TOKEN=")),
                None,
            )
            ports = ((item.get("NetworkSettings") or {}).get("Ports") or {}).get("8765/tcp") or []
            addr = ""
            port: int | None = None
            if ports:
                host = ports[0].get("HostIp") or self.host
                host_port = ports[0].get("HostPort")
                if host_port:
                    port = int(host_port)
                    addr = f"{host}:{host_port}"
            name = (item.get("Name") or "").lstrip("/")
            handles.append(
                SandboxHandle(
                    provider=self.capabilities.name,
                    sandbox_id=name or str(item.get("Id", ""))[:12],
                    addr=addr,
                    token=token,
                    created_at=_maybe_float(labels.get("shinken.created_at") or ""),
                    metadata={
                        "container_id": item.get("Id"),
                        "image": config.get("Image"),
                        "port": port,
                        "owner_pid": _maybe_int(labels.get("shinken.owner_pid") or ""),
                        "rebuilt_from_labels": True,
                    },
                )
            )
        return handles

    def gc(self, snapshots: bool = False, force: bool = False) -> GcReport:
        """Garbage-collect labeled Shinken containers (and, with ``snapshots=True``,
        labeled ``shinken.snapshot=true`` images): reclaim those whose OWNING PROCESS
        is dead; skip live owners — and unknown owners (resources from an SDK that
        predates the ownership labels) — unless ``force=True``. Containers go first so
        the images they reference are reclaimable. Returns a
        :class:`~shinken.providers.base.GcReport` with the counts."""
        report = GcReport()
        doomed: list[str] = []
        for row in self._ownership_rows(self._labeled_container_ids()):
            pid = row["owner_pid"]
            if force or (pid is not None and not _pid_alive(pid)):
                doomed.append(row["id"])
            else:
                report.skipped += 1
        if doomed:
            _run([self.docker_bin, "rm", "-f", *doomed], timeout=60.0)
        report.containers = len(doomed)
        if snapshots:
            result = _run(
                [
                    self.docker_bin,
                    "images",
                    "-q",
                    "--filter",
                    "label=shinken.snapshot=true",
                    "--filter",
                    f"label=shinken.provider={self.capabilities.name}",
                ]
            )
            image_ids = list(
                dict.fromkeys(  # `images -q` repeats an id per tag; dedupe, keep order
                    line.strip() for line in result.stdout.splitlines() if line.strip()
                )
            )
            img_doomed: list[str] = []
            for row in self._ownership_rows(image_ids, image=True):
                pid = row["owner_pid"]
                if force or (pid is not None and not _pid_alive(pid)):
                    img_doomed.append(row["id"])
                else:
                    report.skipped += 1
            for image_id in img_doomed:
                record, immutable_id = self._inspect_snapshot_record(image_id)
                if record is not None:
                    self._remember_snapshot_record(record, image_ref=immutable_id)
                    self.delete_snapshot(str(record["snapshot_id"]))
                else:
                    subprocess.run(
                        [self.docker_bin, "rmi", "-f", image_id],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                    )
            report.images = len(img_doomed)
        return report

    # --- quarantined warm-pool utilities (restore is intentionally disabled) ---------
    # Delta extraction is kept correct and testable for a future frozen-target design,
    # but `_restore_from_pool` refuses to apply it to a running container.

    _GRAFT_EXCLUDES = (
        "/tmp/.X11-unix*",
        "/tmp/.X*-lock",
        "/run/*",
        "/var/run/*",
        "/dev/*",
        "/proc/*",
        "/sys/*",
    )

    @classmethod
    def _graft_excluded(cls, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pat) for pat in cls._GRAFT_EXCLUDES)

    def _capture_delta(self, _cid: str, image_ref: str) -> dict[str, Any]:
        """Build a graft from the immutable committed image's top layer.

        Reading the live donor after ``docker commit`` creates a consistency race. A
        committed image id is immutable, so ``docker image save`` is the source of truth.
        Overlay whiteouts become explicit deletions and are never extracted into the
        target container.
        """
        assert self._delta_dir is not None
        suffix = image_ref.split(":", 1)[-1]
        image_tar_path = self._delta_dir / f"{suffix}.image.tar"
        graft_tar_path = self._delta_dir / f"{suffix}.tar"
        deletions: list[str] = []
        added = 0
        try:
            _run(
                [self.docker_bin, "image", "save", "-o", str(image_tar_path), image_ref],
                timeout=max(self.startup_timeout, 60.0),
            )
            with tarfile.open(image_tar_path, "r:*") as image_tar:
                manifest_file = image_tar.extractfile("manifest.json")
                if manifest_file is None:
                    raise ProviderError("docker image save omitted manifest.json")
                manifest = json.load(manifest_file)
                layer_name = manifest[0]["Layers"][-1]
                layer_file = image_tar.extractfile(layer_name)
                if layer_file is None:
                    raise ProviderError(f"docker image save omitted layer {layer_name}")
                with (
                    tarfile.open(fileobj=layer_file, mode="r|*") as layer_tar,
                    tarfile.open(graft_tar_path, mode="w") as graft_tar,
                ):
                    for member in layer_tar:
                        name = member.name
                        while name.startswith("./"):
                            name = name[2:]
                        name = name.lstrip("/")
                        if not name or name == ".." or name.startswith("../") or "/../" in name:
                            raise ProviderError(
                                f"unsafe path in committed image layer: {member.name}"
                            )
                        parent, basename = os.path.split(name)
                        if basename == ".wh..wh..opq":
                            target = f"/{parent}" if parent else "/"
                            if target != "/" and not self._graft_excluded(target):
                                deletions.append(target)
                            continue
                        if basename.startswith(".wh."):
                            target_name = os.path.join(parent, basename[4:])
                            target = f"/{target_name}"
                            if not self._graft_excluded(target):
                                deletions.append(target)
                            continue
                        path = f"/{name}"
                        if self._graft_excluded(path):
                            continue
                        member.name = name
                        payload = layer_tar.extractfile(member) if member.isfile() else None
                        graft_tar.addfile(member, payload)
                        added += 1
        except ProviderError:
            raise
        except (
            OSError,
            KeyError,
            IndexError,
            TypeError,
            json.JSONDecodeError,
            tarfile.TarError,
        ) as exc:
            raise ProviderError(f"immutable delta capture failed for {image_ref}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                image_tar_path.unlink()
        if not added:
            with contextlib.suppress(OSError):
                graft_tar_path.unlink()
        return {
            "tar": str(graft_tar_path) if added else None,
            "deletions": list(dict.fromkeys(deletions)),
            "source": "committed_image_layer",
            "image_ref": image_ref,
        }

    def _pool_compatible(self, spec: SandboxSpec | None) -> bool:
        """A warm container can only impersonate a snapshot whose originating spec
        matches the pool's (same base image, geometry and resource limits) — anything
        else falls back to the classic restore path rather than mislead."""
        if spec is None:
            return False
        pool = self._pool_spec
        return (
            (spec.image or self.image) == (pool.image or self.image)
            and spec.os == pool.os
            and spec.needs_gui == pool.needs_gui
            and spec.needs_gpu == pool.needs_gpu
            and spec.state_fidelity == pool.state_fidelity
            and spec.screen_geometry == pool.screen_geometry
            and spec.memory == pool.memory
            and spec.cpus == pool.cpus
            and spec.pids_limit == pool.pids_limit
            and spec.shm_size == pool.shm_size
            and spec.extra_env == pool.extra_env
        )

    def _restore_from_pool(self, image: str, spec: SandboxSpec | None) -> SandboxHandle | None:
        """Reject the live-target graft until process/filesystem parity is proven."""
        if self._pool is None:
            return None
        raise ProviderError(
            f"warm restore for {image!r} is disabled: live-target equivalence is unproven"
        )

    def _replenish_pool(self) -> None:
        """Background thread: keep the pool at its target size by cold-booting base
        containers (each fully readiness-gated before it is claimable)."""
        assert self._pool is not None
        while not self._pool_stop.is_set():
            if self._pool.qsize() >= self._pool_target:
                self._pool_stop.wait(0.05)
                continue
            try:
                handle = self.create(replace(self._pool_spec))
            except Exception:
                self._pool_stop.wait(0.5)  # daemon hiccup — retry, don't spin
                continue
            if self._pool_stop.is_set():
                with contextlib.suppress(Exception):
                    self.destroy(handle)
                return
            self._pool.put(handle)

    def warm_pool_available(self) -> int:
        """How many pre-booted warm containers are claimable right now (0 when the
        pool is disabled). Lets a caller distinguish steady-state pool service from
        exhaustion fallback when interpreting fork latencies."""
        return self._pool.qsize() if self._pool is not None else 0

    def shutdown_pool(self) -> int:
        """Stop replenishment and destroy every unclaimed warm container. Returns the
        count destroyed. Idempotent; also reclaims the cached delta tars."""
        if self._pool is None:
            return 0
        self._pool_stop.set()
        if self._pool_thread is not None:
            self._pool_thread.join(timeout=self.startup_timeout + 5)
        drained = 0
        while True:
            try:
                handle = self._pool.get_nowait()
            except queue.Empty:
                break
            with contextlib.suppress(Exception):
                self.destroy(handle)
            drained += 1
        if self._delta_dir is not None:
            shutil.rmtree(self._delta_dir, ignore_errors=True)
            self._delta_dir = None
        self._deltas.clear()
        self._pool = None
        return drained

    def _container_rss(self, handle: SandboxHandle) -> int | None:
        try:
            result = _run(
                [
                    self.docker_bin,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    handle.sandbox_id,
                ],
                timeout=10.0,
            )
        except ProviderError:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return _parse_mem_usage(data.get("MemUsage", ""))


def _parse_mem_usage(value: str) -> int | None:
    used = value.split("/", 1)[0].strip()
    if not used:
        return None
    units = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
    }
    number = "".join(ch for ch in used if ch.isdigit() or ch == ".")
    suffix = used[len(number) :].strip().lower()
    if not number:
        return None
    return int(float(number) * units.get(suffix, 1))


def _png_has_non_black_pixel(data: bytes) -> bool | None:
    """Best-effort PNG readiness check without adding an image dependency."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    if not width or not height or bit_depth != 8 or color_type not in {0, 2, 6}:
        return None
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    prev = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        _unfilter(row, prev, channels, filter_type)
        if color_type == 0:
            if any(row):
                return True
        else:
            for idx in range(0, len(row), channels):
                if any(row[idx : idx + 3]):
                    return True
        prev = row
    return False


def _unfilter(row: bytearray, prev: bytearray, bpp: int, filter_type: int) -> None:
    for idx, value in enumerate(row):
        left = row[idx - bpp] if idx >= bpp else 0
        up = prev[idx]
        upper_left = prev[idx - bpp] if idx >= bpp else 0
        if filter_type == 1:
            row[idx] = (value + left) & 0xFF
        elif filter_type == 2:
            row[idx] = (value + up) & 0xFF
        elif filter_type == 3:
            row[idx] = (value + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            row[idx] = (value + _paeth(left, up, upper_left)) & 0xFF


def _paeth(left: int, up: int, upper_left: int) -> int:
    p = left + up - upper_left
    pa = abs(p - left)
    pb = abs(p - up)
    pc = abs(p - upper_left)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return upper_left


def _validate_guest_path(guest_path: str) -> str:
    """Guest paths are absolute paths inside the container; reject relative paths and any
    ``..`` traversal so a transfer can't walk outside the intended location (#154). The
    capability-level `fs.scope` decision is enforced separately by the SDK gateway."""
    if not guest_path.startswith("/"):
        raise FileScopeError(f"guest path must be absolute: {guest_path!r}")
    if ".." in guest_path.split("/"):
        raise FileScopeError(f"guest path may not contain '..': {guest_path!r}")
    return guest_path


class DockerGuestTransport:
    """Move files across the real guest boundary with ``docker cp`` (#154).

    Same ``put``/``get`` contract as :class:`~shinken.artifacts.LocalArtifactStore`, so the
    Sandbox uses it transparently when a Docker provider attaches it — but bytes land in
    (and come from) the actual container filesystem, and the returned
    :class:`~shinken.artifacts.ArtifactRef` is content-hashed for auditability."""

    def __init__(self, container_id: str, docker_bin: str = "docker"):
        self.container_id = container_id
        self.docker_bin = docker_bin

    def put(
        self, local_path: str | os.PathLike, guest_path: str, scope: str = "session"
    ) -> ArtifactRef:
        _validate_guest_path(guest_path)
        sha = sha256_file(local_path)
        size = os.path.getsize(local_path)
        _run([self.docker_bin, "cp", os.fspath(local_path), f"{self.container_id}:{guest_path}"])
        return ArtifactRef(guest_path, sha, size, scope, "put")

    def get(
        self,
        guest_path: str,
        local_path: str | os.PathLike,
        *,
        expect_sha256: str | None = None,
        scope: str = "session",
    ) -> ArtifactRef:
        _validate_guest_path(guest_path)
        _run([self.docker_bin, "cp", f"{self.container_id}:{guest_path}", os.fspath(local_path)])
        digest = sha256_file(local_path)
        if expect_sha256 is not None and digest != expect_sha256:
            with contextlib.suppress(OSError):
                os.remove(local_path)
            raise HashMismatch(f"{guest_path}: expected {expect_sha256[:12]}…, got {digest[:12]}…")
        return ArtifactRef(guest_path, digest, os.path.getsize(local_path), scope, "get")
