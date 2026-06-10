"""Docker-backed local sandbox provider."""

from __future__ import annotations

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
import tempfile
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
    ProviderCapabilities,
    ProviderError,
    SandboxHandle,
    SandboxHealth,
    SandboxProvider,
    SandboxSpec,
)


def _free_port(host: str = "127.0.0.1") -> int:
    sock = socket.socket()
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


# Mask secret env values when rendering a command for an error/log (#153). Matches a
# `KEY=VALUE` arg whose KEY ends in TOKEN/SECRET/PASSWORD/KEY (e.g. SHINKEND_TOKEN).
_SECRET_ENV_RE = re.compile(r"^([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY))=.+$")


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
    ) -> None:
        self.image = image
        self.docker_bin = docker_bin
        self.host = host
        self.name_prefix = name_prefix
        self.startup_timeout = startup_timeout
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
        # Checkpoint registry (#206): ckpt_id -> {snapshot_id, event_seq, agent_state_ref}.
        # In-memory for the local reference tier; a real Control Plane persists the DAG.
        self._checkpoints: dict[str, dict[str, Any]] = {}
        # Snapshot registry: snapshot tag -> the SandboxSpec it was committed from, so
        # restore()/fork() can rebuild geometry + resource limits instead of silently
        # reverting to defaults. Also the reclamation set for cleanup_snapshots().
        self._snapshots: dict[str, SandboxSpec] = {}
        # Warm-pool state graft (opt-in, S8): keep `warm_pool_size` pre-booted BASE-image
        # containers ready (replenished by a background thread), and serve restore()/
        # fork() by claiming one and grafting the snapshot's filesystem delta onto it —
        # skipping the cold boot entirely. Files-only semantics, the SAME state tier as
        # `docker commit`: running processes are NOT restored, deletions/changes are the
        # `docker diff` set captured at snapshot time, and live runtime state
        # (/run, /dev, X sockets/locks) is deliberately excluded from the graft so the
        # warm container's own daemons keep their footing. A snapshot without a captured
        # delta (or with a spec the pool can't match) falls back to the classic
        # boot-from-committed-image path.
        self._deltas: dict[str, dict[str, Any]] = {}
        self._delta_dir: Path | None = None
        self._pool: queue.Queue[SandboxHandle] | None = None
        self._pool_target = max(0, int(warm_pool_size))
        self._pool_spec = warm_pool_spec or SandboxSpec()
        self._pool_claim_timeout = warm_pool_claim_timeout
        self._pool_stop = threading.Event()
        self._pool_thread: threading.Thread | None = None
        if self._pool_target > 0:
            self._delta_dir = Path(tempfile.mkdtemp(prefix="shinken-deltas-"))
            self._pool = queue.Queue()
            self._pool_thread = threading.Thread(
                target=self._replenish_pool, name="shinken-warm-pool", daemon=True
            )
            self._pool_thread.start()

    def connect(self, handle: SandboxHandle):
        """Connect, then wire file transfer through the **actual guest** filesystem via
        `docker cp` (#154) instead of the host-local reference store, so `put_file`/
        `get_file` move bytes across the real Sandbox boundary."""
        env = super().connect(handle)
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

    def _create_once(self, spec: SandboxSpec) -> SandboxHandle:
        token = secrets.token_hex(16)
        port = _free_port(self.host)
        name = f"{self.name_prefix}-{uuid.uuid4().hex[:10]}"
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
            "-e",
            # Dev-only: the token is delivered via env, so it is readable by any process
            # in the guest (and from /proc) — not a real boundary (#153). It is redacted
            # from provider errors/logs; a faithful boundary needs fd/mounted-secret
            # delivery plus the server-side Action Gateway (D6).
            f"SHINKEND_TOKEN={token}",
            "-e",
            f"SCREEN_GEOMETRY={spec.screen_geometry}",
        ]
        # Caller-supplied guest env (SandboxSpec.extra_env), e.g. SHINKEND_DAMAGE=off.
        # Provider-reserved names stay authoritative: they are set above and the LAST
        # -e wins in docker, so reserved keys are skipped here instead of trusted.
        for key, value in (spec.extra_env or {}).items():
            if key in ("SHINKEND_TOKEN", "SCREEN_GEOMETRY"):
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
        cmd.append(spec.image or self.image)

        created_at = time.time()
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
            },
        )
        try:
            self._wait_ready(handle)
        except Exception:
            self.destroy(handle)
            raise
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

    # --- runtime-state primitives (disk tier, #206) -----------------------------------
    # Reference implementation on Docker's filesystem layer via `docker commit` (no live
    # memory — that is the CRIU/process tier, and the sub-ms CoW fast tier is Phase-1).

    def _container_of(self, handle: SandboxHandle) -> str:
        cid = handle.metadata.get("container_id") or handle.sandbox_id
        if not cid:
            raise ProviderError("sandbox handle has no container id to snapshot")
        return str(cid)

    def _spec_from_handle(self, handle: SandboxHandle) -> SandboxSpec:
        """Rebuild the originating SandboxSpec from a handle's metadata (geometry +
        resource limits), so a snapshot restored later boots at the SAME resolution and
        limits as the golden state instead of silently reverting to defaults."""
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
                if k not in {"container_id", "port", "destroyed"}
            },
        )

    def snapshot(self, handle: SandboxHandle, name: str | None = None) -> str:
        """Disk snapshot via `docker commit` → a tagged image id (snapshot_kind=disk).
        Records the originating spec so restore()/fork() preserve geometry + limits.
        When the warm pool is enabled, also captures the snapshot's filesystem delta
        (``docker diff`` + a tar of the added/changed paths) so restore() can graft it
        onto a pre-booted container instead of cold-booting the committed image."""
        tag = f"shinken-snap:{name or uuid.uuid4().hex[:12]}"
        cid = self._container_of(handle)
        commit = [self.docker_bin, "commit", cid, tag]
        _run(commit, timeout=self.startup_timeout)
        self._snapshots[tag] = self._spec_from_handle(handle)
        if self._pool is not None:
            try:
                self._deltas[tag] = self._capture_delta(cid, tag)
            except ProviderError:
                # No delta -> this snapshot simply restores via the classic path.
                self._deltas.pop(tag, None)
        return tag

    def restore(self, snapshot_id: str) -> SandboxHandle:
        """Launch a fresh sandbox from a snapshot image (or a checkpoint id) — a new live
        container off the committed filesystem layer, at the geometry/limits captured when
        the snapshot was taken (not provider defaults). With the warm pool enabled and a
        captured delta available, restore instead claims a pre-booted container and
        grafts the delta onto it (files-only, same tier as `docker commit`) — falling
        back to the classic cold boot when the pool is empty or incompatible."""
        image = self._checkpoints.get(snapshot_id, {}).get("snapshot_id", snapshot_id)
        spec = self._snapshots.get(image)
        if self._pool is not None:
            handle = self._restore_from_pool(image, spec)
            if handle is not None:
                return handle
        spec = replace(spec, image=image) if spec is not None else SandboxSpec(image=image)
        return self.create(spec)

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Reclaim a committed snapshot image (`docker rmi`). Resolves a checkpoint id to
        its snapshot first. Idempotent and best-effort — a still-referenced image is left."""
        image = self._checkpoints.get(snapshot_id, {}).get("snapshot_id", snapshot_id)
        subprocess.run(
            [self.docker_bin, "rmi", "-f", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        self._snapshots.pop(image, None)
        delta = self._deltas.pop(image, None)
        if delta and delta.get("tar"):
            with contextlib.suppress(OSError):
                os.remove(delta["tar"])

    def cleanup_snapshots(self) -> int:
        """Reclaim every snapshot image this provider committed (`docker rmi`). Returns the
        count removed. Pairs with cleanup_orphans() (which only removes containers)."""
        tags = list(self._snapshots)
        for tag in tags:
            self.delete_snapshot(tag)
        return len(tags)

    def resume(self, handle_or_checkpoint: SandboxHandle | str) -> SandboxHandle:
        """Bring a snapshot/checkpoint back live. Docker containers are ephemeral (`--rm`),
        so resume takes a snapshot/checkpoint id, not a live handle."""
        if isinstance(handle_or_checkpoint, str):
            return self.restore(handle_or_checkpoint)
        raise ProviderError(
            "Docker resume needs a snapshot/checkpoint id "
            "(containers are ephemeral; snapshot() first)"
        )

    def fork(self, handle: SandboxHandle) -> SandboxHandle:
        """Branch a new live sandbox from the current state: snapshot + restore. Disk CoW
        via the new container's writable layer (~0.3–0.5 s) — not the sub-ms fast tier."""
        return self.restore(self.snapshot(handle))

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
        snapshot_id = self.snapshot(handle, name=name)
        ckpt_id = f"ckpt-{uuid.uuid4().hex[:12]}"
        self._checkpoints[ckpt_id] = {
            "snapshot_id": snapshot_id,
            "event_seq": event_seq,
            "agent_state_ref": agent_state_ref,
        }
        return ckpt_id

    def cleanup_orphans(self) -> int:
        # Select strictly by the labels we stamp at create time (#157). Docker's `name`
        # filter is substring-based, not the anchored regex `name=^prefix-` implies, so
        # it could miss intended containers (or match unintended ones) and leave orphans;
        # the two labels already identify our containers precisely.
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
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return 0
        _run([self.docker_bin, "rm", "-f", *ids], timeout=30.0)
        return len(ids)

    # --- warm-pool state graft (opt-in, S8) -------------------------------------------
    # fork→usable without a cold boot: pre-booted base-image containers + the snapshot's
    # filesystem delta applied in place. SEMANTIC LIMITS (documented, on purpose):
    # files only — exactly the state tier `docker commit` already captures (no process/
    # memory state; that is the CRIU tier); the delta is the `docker diff` set at
    # snapshot time, applied as tar-extract (adds/changes, as root) + rm (deletions),
    # which is the operational equivalent of applying the commit layer's .wh. whiteouts;
    # live runtime state (/run, /var/run, /dev, X sockets/locks, /proc, /sys) is
    # excluded so the warm container's own daemons keep their footing; and a graft onto
    # a container whose processes are concurrently writing the same paths is
    # last-writer-wins. Pool sizing: each warm container costs one cold boot (paid in
    # the background) and one idle desktop's RSS; bursts beyond the pool fall back to
    # the classic cold path.

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

    def _capture_delta(self, cid: str, tag: str) -> dict[str, Any]:
        """Record the live container's filesystem delta vs its base image, right after
        `docker commit`: `docker diff` enumerates added (A) / changed (C) / deleted (D)
        paths, the A/C set is tarred OUT of the container (one `docker exec tar`), and
        the D set is kept as a deletion list. Equivalent to extracting the commit
        layer's tar (whiteout files included) but without serializing the whole image
        through `docker save`. Window caveat: the container keeps running between the
        commit and this capture, so a concurrently-written file may differ from the
        committed layer by that sliver."""
        diff = _run([self.docker_bin, "diff", cid], timeout=self.startup_timeout)
        adds: list[str] = []
        deletions: list[str] = []
        for line in diff.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            kind, path = parts
            if self._graft_excluded(path):
                continue
            if kind == "D":
                deletions.append(path)
            elif kind in ("A", "C"):
                adds.append(path)
        tar_path: str | None = None
        if adds:
            assert self._delta_dir is not None
            tar_path = str(self._delta_dir / f"{tag.split(':', 1)[-1]}.tar")
            # --no-recursion: docker diff already lists every changed path individually,
            # so directory entries must not drag their unchanged children along.
            with open(tar_path, "wb") as out:
                proc = subprocess.run(
                    [
                        self.docker_bin,
                        "exec",
                        "-i",
                        cid,
                        "tar",
                        "-cf",
                        "-",
                        "--no-recursion",
                        "-C",
                        "/",
                        "-T",
                        "-",
                    ],
                    input="\n".join(p.lstrip("/") for p in adds).encode(),
                    stdout=out,
                    stderr=subprocess.PIPE,
                    timeout=self.startup_timeout,
                )
            if proc.returncode != 0:
                with contextlib.suppress(OSError):
                    os.remove(tar_path)
                err = proc.stderr.decode(errors="replace").strip()
                raise ProviderError(f"delta capture failed for {tag}: {err}")
        return {"tar": tar_path, "deletions": deletions}

    def _pool_compatible(self, spec: SandboxSpec | None) -> bool:
        """A warm container can only impersonate a snapshot whose originating spec
        matches the pool's (same base image, geometry and resource limits) — anything
        else falls back to the classic restore path rather than mislead."""
        if spec is None:
            return False
        pool = self._pool_spec
        return (
            (spec.image or self.image) == (pool.image or self.image)
            and spec.screen_geometry == pool.screen_geometry
            and spec.memory == pool.memory
            and spec.cpus == pool.cpus
            and spec.pids_limit == pool.pids_limit
            and spec.shm_size == pool.shm_size
        )

    def _restore_from_pool(self, image: str, spec: SandboxSpec | None) -> SandboxHandle | None:
        """Claim a warm container and graft `image`'s delta onto it. Returns None when
        the pool path does not apply (no delta captured, incompatible spec, or pool
        empty past the claim timeout) — the caller falls back to the classic boot."""
        delta = self._deltas.get(image)
        if delta is None or self._pool is None or not self._pool_compatible(spec):
            return None
        try:
            if self._pool_claim_timeout > 0:
                handle = self._pool.get(timeout=self._pool_claim_timeout)
            else:
                handle = self._pool.get_nowait()
        except queue.Empty:
            return None
        try:
            cid = self._container_of(handle)
            if delta["deletions"]:
                _run(
                    [
                        self.docker_bin,
                        "exec",
                        "-u",
                        "0",
                        cid,
                        "rm",
                        "-rf",
                        "--",
                        *delta["deletions"],
                    ],
                    timeout=self.startup_timeout,
                )
            if delta["tar"]:
                with open(delta["tar"], "rb") as tar_in:
                    proc = subprocess.run(
                        [
                            self.docker_bin,
                            "exec",
                            "-i",
                            "-u",
                            "0",
                            cid,
                            "tar",
                            "-xpf",
                            "-",
                            "-C",
                            "/",
                        ],
                        stdin=tar_in,
                        capture_output=True,
                        timeout=self.startup_timeout,
                    )
                if proc.returncode != 0:
                    raise ProviderError(
                        f"delta graft failed: {proc.stderr.decode(errors='replace').strip()}"
                    )
            # Readiness barrier: the warm container was ready when pooled and the graft
            # restarts nothing, so this is one fast guest-side `ready` round trip.
            self._wait_ready(handle)
        except Exception:
            self.destroy(handle)
            raise
        handle.metadata["image"] = image  # restored-state lineage, like the classic path
        handle.metadata["pool_graft"] = True
        return handle

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
