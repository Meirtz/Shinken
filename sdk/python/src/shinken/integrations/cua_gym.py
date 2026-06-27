"""CUA-Gym interop — exported task bundles as a TaskSource + fork-native env reset.

xlang-ai/CUA-Gym (<https://github.com/xlang-ai/CUA-Gym>, surveyed at commit ``1e50b797``)
mass-produces verifiable RLVR tasks for computer-use agents. Its two consumable surfaces,
mirrored here without importing it (protocol-shape duck typing; fixture-tested):

1. **Task bundles** — ``output/final/<task_id>/`` directories, each holding an
   OSWorld-shape ``config.json`` (``instruction``/``id``/``app_type``, a ``config`` list of
   ``download`` + ``execute`` setup steps, and an ``evaluator`` of ``type: python`` pointing
   at the reward script) next to the scripts themselves: ``initial_setup.py`` (reaches the
   task's initial state), ``golden_patch.py`` (initial → golden, used for verifier
   validation), and ``reward.py`` — an in-guest python evaluator whose **last output line is
   ``REWARD: X.X``** (their orchestrator's parse contract).
2. **Env surface** — ``utils/env.py``'s ``Env``: an OSWorld-Flask-protocol client with
   ``screenshot``/``execute``/``run_python``/``run_bash``/``upload``/``download``/``launch``/
   ``get_screen_size``/``get_accessibility_tree``/``get_directory_tree`` operations, whose
   *lifecycle* provisions a **fresh cloud VM per use** (and two per generated task for the
   golden/initial double-test) — minutes of provisioning per environment.

This adapter exposes Shinken sandboxes through the same shapes:

- :class:`CuaGymTaskSource` loads exported bundles from a directory (or ``$CUA_GYM_TASKS``)
  — task content is never embedded in-tree, matching the ``OSWORLD_PATH`` discipline.
- :class:`ShinkenCuaGymEnv` mirrors the ``Env`` method surface over a Shinken provider and
  replaces the fresh-VM lifecycle with the runtime-state primitive CUA-Gym lacks:
  **``reset()`` is a golden-checkpoint restore.** Bundle setup runs ONCE into a base
  sandbox and every reset materializes a replica from that checkpoint. Because CUA-Gym
  setup commonly leaves an application running, the default request requires
  process+memory fidelity; a filesystem-only provider must be selected explicitly only
  for tasks whose post-restore launch/focus is replayed.

``TaskSource``/scorers are consumer-side by design (``docs/design/agent-runtime.md`` §5);
nothing here touches the runtime waist. No CUA-Gym code is imported or copied.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import posixpath
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shinken._lifecycle import connect_owned_handle
from shinken.errors import ShinkenError
from shinken.providers.base import SandboxSpec

#: Mirror of the public method surface of CUA-Gym's ``utils/env.py`` ``Env`` (operations
#: only — their cloud-lifecycle classmethods are replaced by reset-via-fork). Kept as a
#: module constant so the protocol-shape test pins the adapter to the surveyed interface.
CUA_GYM_ENV_SURFACE = (
    "screenshot",
    "execute",
    "run_python",
    "run_bash",
    "upload",
    "download",
    "launch",
    "get_screen_size",
    "get_accessibility_tree",
    "get_directory_tree",
    "close",
)

#: Their reward contract: ``reward.py`` prints ``REWARD: X.X`` as its final output line;
#: the orchestrator parses that line. The LAST occurrence wins (scripts may echo earlier).
_REWARD_RE = re.compile(r"^\s*REWARD:\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*$", re.M)


class CuaGymError(ShinkenError):
    """A CUA-Gym bundle/setup/reward step could not be completed."""


def parse_reward(output: str) -> float | None:
    """Extract the score from a reward script's stdout per the CUA-Gym contract — the last
    ``REWARD: X.X`` line. Returns ``None`` when no such line exists (a broken reward run is
    typed, never silently scored 0.0)."""
    matches = _REWARD_RE.findall(output or "")
    if not matches:
        return None
    reward = float(matches[-1])
    if not math.isfinite(reward):
        raise CuaGymError(f"reward must be finite, got {reward!r}")
    if not 0.0 <= reward <= 1.0:
        raise CuaGymError(f"reward must be in [0.0, 1.0], got {reward!r}")
    return reward


# --------------------------------------------------------------------------- task source


@dataclass(frozen=True)
class CuaGymTask:
    """One exported CUA-Gym bundle: the parsed ``config.json`` plus its sibling scripts."""

    task_id: str
    instruction: str
    app_type: str
    path: Path  # the bundle directory
    config: dict = field(default_factory=dict)

    @property
    def setup_steps(self) -> list[dict]:
        """The OSWorld-shape setup steps (``download`` / ``execute`` dicts), in order."""
        steps = self.config.get("config") or []
        return list(steps) if isinstance(steps, list) else []

    @property
    def reward_script(self) -> Path:
        return self.path / "reward.py"

    @property
    def setup_script(self) -> Path | None:
        p = self.path / "initial_setup.py"
        return p if p.is_file() else None

    @property
    def golden_patch(self) -> Path | None:
        p = self.path / "golden_patch.py"
        return p if p.is_file() else None


class CuaGymTaskSource:
    """Load CUA-Gym exported bundles from a root directory (``output/final/`` shape).

    ``root`` defaults to ``$CUA_GYM_TASKS``; task content is external by construction.
    Directories without a parseable ``config.json`` + ``reward.py`` are skipped and listed
    in :attr:`skipped` as ``(path, reason)`` so a malformed bundle is visible, not silent.
    """

    def __init__(self, root: str | os.PathLike | None = None) -> None:
        raw = os.fspath(root) if root is not None else os.environ.get("CUA_GYM_TASKS", "")
        if not raw:
            raise CuaGymError(
                "no task root: pass root= or set $CUA_GYM_TASKS to a CUA-Gym "
                "output/final/-shaped directory (task content is never embedded in-tree)"
            )
        self.root = Path(raw).expanduser()
        if not self.root.is_dir():
            raise CuaGymError(f"task root is not a directory: {self.root}")
        self.skipped: list[tuple[Path, str]] = []
        self._tasks: dict[str, CuaGymTask] = {}
        for bundle in sorted(p for p in self.root.iterdir() if p.is_dir()):
            task, reason = self._load(bundle)
            if task is None:
                self.skipped.append((bundle, reason or "unknown"))
            else:
                previous = self._tasks.get(task.task_id)
                if previous is not None:
                    raise CuaGymError(
                        f"duplicate task_id {task.task_id!r}: {previous.path} and {task.path}"
                    )
                self._tasks[task.task_id] = task

    @staticmethod
    def _load(bundle: Path) -> tuple[CuaGymTask | None, str | None]:
        # exported bundles ship the config as config.json (pipeline output) or task.json
        # (the released HF artifact, cua_gym_tasks_v1) — same OSWorld-shape content.
        cfg_path = next(
            (p for n in ("config.json", "task.json") if (p := bundle / n).is_file()), None
        )
        if cfg_path is None:
            return None, "no config.json/task.json"
        try:
            cfg = json.loads(cfg_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"unreadable config.json: {exc}"
        if not isinstance(cfg, dict):
            return None, "config.json is not an object"
        if not (bundle / "reward.py").is_file():
            return None, "no reward.py"
        task_id = str(cfg.get("id") or bundle.name)
        return (
            CuaGymTask(
                task_id=task_id,
                instruction=str(cfg.get("instruction", "")),
                app_type=str(cfg.get("app_type", "")),
                path=bundle,
                config=cfg,
            ),
            None,
        )

    def tasks(self) -> list[CuaGymTask]:
        return list(self._tasks.values())

    def get(self, task_id: str) -> CuaGymTask:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"unknown task {task_id!r}; available: {sorted(self._tasks)}") from None

    def __iter__(self) -> Iterator[CuaGymTask]:
        return iter(self._tasks.values())

    def __len__(self) -> int:
        return len(self._tasks)


# --------------------------------------------------------------------------- guest exec

#: ``guest_exec(argv, *, timeout, workdir, detach) -> (returncode, stdout, stderr)`` — the
#: guest exec channel. The PREFERRED transport is the typed in-band ACI ``exec`` verb
#: (:class:`_AciExec` — substrate-agnostic: works wherever a shinkend runs, no host
#: docker CLI required); ``docker exec`` (:class:`_DockerExec`) remains the out-of-band
#: fallback for pre-exec runtimes, exactly as CUA-Gym drives OSWorld's ``/setup/execute``.
GuestExec = Callable[..., tuple[int, str, str]]


def _session_supports_exec(sess: Any) -> bool:
    """Whether the connected session's runtime advertises the typed ``exec`` verb."""
    try:
        return "exec" in (sess.capabilities.verbs or [])
    except Exception:
        return False


class _AciExec:
    """In-band exec channel over the typed ACI ``exec`` verb (G1) — the preferred
    transport: setup/verify flows over the SAME WebSocket as actions/observations,
    so it works on substrates where ``docker exec`` does not exist (a remote
    shinkend over WS, non-Docker providers)."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        workdir: str | None = None,
        detach: bool = False,
    ) -> tuple[int, str, str]:
        if detach:
            # mirror `docker exec -d`: fire-and-forget via an explicit shell opt-in
            line = " ".join(shlex.quote(str(a)) for a in argv)
            self.session.exec(shell=f"({line}) >/dev/null 2>&1 &", cwd=workdir, timeout=timeout)
            return (0, "", "")
        r = self.session.exec(list(argv), cwd=workdir, timeout=timeout)
        if r.get("timed_out"):
            # the _DockerExec timeout convention: rc 124 + a typed stderr message
            return (124, r.get("stdout", ""), f"timeout after {timeout}s")
        rc = r.get("exit_code")
        if rc is None:  # killed by a signal without a timeout — report like a shell
            rc = 128 + int(r.get("signal") or 1)
        return (rc, r.get("stdout", ""), r.get("stderr", ""))


class _DockerExec:
    """Default exec channel for Docker-backed providers: ``docker exec`` on the handle's
    container (duck-typed off ``provider.docker_bin`` + ``handle.metadata['container_id']``)."""

    def __init__(self, docker_bin: str, container_id: str, *, user: str | None = None) -> None:
        self.docker_bin = docker_bin
        self.container_id = container_id
        self.user = user

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        workdir: str | None = None,
        detach: bool = False,
    ) -> tuple[int, str, str]:
        cmd = [self.docker_bin, "exec"]
        if self.user:
            cmd += ["-u", self.user]
        if detach:
            cmd.append("-d")
        if workdir:
            cmd += ["-w", workdir]
        cmd.append(self.container_id)
        cmd += argv
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return 124, str(exc.stdout or ""), f"timeout after {timeout}s"
        except FileNotFoundError as exc:
            raise CuaGymError(f"container engine not found: {self.docker_bin}") from exc
        return res.returncode, res.stdout, res.stderr


def default_exec_factory(provider: Any, handle: Any) -> GuestExec:
    """Build the guest-exec channel for a (provider, handle) pair. Knows the Docker shape;
    a non-Docker provider supplies its own ``exec_factory``."""
    docker_bin = getattr(provider, "docker_bin", None)
    metadata = getattr(handle, "metadata", None) or {}
    container = metadata.get("container_id") or getattr(handle, "sandbox_id", None)
    if not docker_bin or not container:
        raise CuaGymError(
            "provider exposes no guest-exec channel; pass exec_factory=(provider, handle) "
            "-> callable returning (returncode, stdout, stderr)"
        )
    return _DockerExec(str(docker_bin), str(container))


# --------------------------------------------------------------------------- the env


class ShinkenCuaGymEnv:
    """One CUA-Gym task on a Shinken provider, with **fork-native reset**.

    Method surface mirrors CUA-Gym's ``utils/env.py`` ``Env`` (see
    :data:`CUA_GYM_ENV_SURFACE`), so code written against their env drives a Shinken
    sandbox unchanged. The lifecycle is where Shinken differs:

    - their ``Env.create()``: a fresh cloud VM per use (~minutes of provisioning; two VMs
      per generated task for the golden/initial double-test);
    - :meth:`reset` here: the FIRST call creates one base sandbox, replays the bundle's
      setup steps (``download`` → ``put_file`` from the bundle dir, ``execute`` → guest
      exec), checkpoints the result as the **golden state**, and destroys the base; every
      call (including the first) then materializes a fresh replica from that single
      checkpoint. The Docker disk tier restores persistent filesystem state and restarts
      processes; setup that depends on a live GUI/window must be replayed after restore or
      use a provider whose declared fidelity includes process+memory state.

    ``download`` steps are resolved **from the bundle directory** by URL basename (their
    exported URLs point at a private object store); a referenced file missing from the
    bundle raises :class:`CuaGymError` rather than fetching the network.
    """

    def __init__(
        self,
        task: CuaGymTask,
        provider: Any,
        *,
        spec: Any = None,
        exec_factory: Callable[[Any, Any], GuestExec] = default_exec_factory,
        guest_python: str = "python3",
        setup_timeout: float = 300.0,
        exec_timeout: float = 60.0,
    ) -> None:
        self.task = task
        self.provider = provider
        # CUA-Gym setup is GUI state preparation, not merely file installation. Fail
        # closed on Docker's filesystem tier instead of silently checkpointing files
        # while losing the application/window/process state the agent is meant to see.
        # Only the trusted caller may lower this requirement after auditing that every
        # required launch/focus step is replayed after restore. Bundle content is an
        # untrusted workload, so a self-declared fidelity field must never authorize a
        # weaker state contract.
        self.spec = spec if spec is not None else SandboxSpec(state_fidelity="process_memory")
        self.exec_factory = exec_factory
        self.guest_python = guest_python
        self.setup_timeout = setup_timeout
        self.exec_timeout = exec_timeout
        self.golden_checkpoint: str | None = None
        self._handle: Any = None  # current replica
        self._sess: Any = None  # connected Shinken session for the current replica
        self._exec: GuestExec | None = None
        # Failed cleanup from an aborted golden build remains owned by this env so close()
        # (and the engine's pending-close queue) can retry instead of forgetting handles.
        self._pending_build_sessions: list[Any] = []
        self._pending_build_handles: list[Any] = []
        self._pending_build_snapshots: list[str] = []

    # --- lifecycle: golden checkpoint once, fork per reset -------------------------------

    def reset(self) -> dict:
        """Materialize a fresh replica of the task's golden state and return the first
        observation (``{"instruction", "screenshot"}``). Setup runs only on the first call."""
        if self.golden_checkpoint is None:
            self.golden_checkpoint = self._build_golden()
        self._teardown_replica()
        handle = self.provider.resume(self.golden_checkpoint)
        sess = connect_owned_handle(self.provider, handle)
        self._handle = handle
        self._sess = sess
        try:
            self._exec = self._exec_channel(sess, handle)
            return {"instruction": self.task.instruction, "screenshot": self.screenshot()}
        except BaseException:
            self._teardown_replica()
            raise

    def _exec_channel(self, sess: Any, handle: Any) -> GuestExec:
        """The guest exec channel for one connected replica: PREFER the typed in-band
        ACI ``exec`` verb when the runtime advertises it (substrate-agnostic — no host
        docker CLI), falling back to the configured out-of-band factory (``docker
        exec`` by default) for pre-exec runtimes. An explicitly-passed ``exec_factory``
        is caller intent and always wins."""
        if self.exec_factory is default_exec_factory and _session_supports_exec(sess):
            return _AciExec(sess)
        return self.exec_factory(self.provider, handle)

    def _build_golden(self) -> str:
        """Create the base sandbox, replay the bundle's setup steps once, checkpoint the
        golden state, and destroy the base (replicas resume from the checkpoint)."""
        base = self.provider.create(self.spec)
        sess = None
        snapshot: str | None = None
        primary_error: BaseException | None = None
        try:
            sess = self.provider.connect(base)
            run = self._exec_channel(sess, base)
            for step in self.task.setup_steps:
                self._apply_setup_step(step, sess, run, handle=base)
            snapshot = self.provider.checkpoint(base)
        except BaseException as exc:
            primary_error = exc

        cleanup_errors: list[Exception] = []
        if sess is not None:
            try:
                sess.close()
            except Exception as exc:
                self._pending_build_sessions.append(sess)
                cleanup_errors.append(exc)
        try:
            self.provider.destroy(base)
        except Exception as exc:
            self._pending_build_handles.append(base)
            cleanup_errors.append(exc)

        if primary_error is not None:
            if cleanup_errors:
                raise primary_error from cleanup_errors[0]
            raise primary_error
        if cleanup_errors:
            if snapshot is not None:
                self._pending_build_snapshots.append(snapshot)
            raise cleanup_errors[0]
        if snapshot is None:
            raise CuaGymError("provider checkpoint returned no golden snapshot id")
        return snapshot

    def _apply_setup_step(self, step: dict, sess: Any, run: GuestExec, handle: Any = None) -> None:
        kind = step.get("type")
        params = step.get("parameters") or {}
        if kind == "download":
            for f in params.get("files") or []:
                name = os.path.basename(str(f.get("url", "")))
                src = self.task.path / name
                if not src.is_file():
                    raise CuaGymError(
                        f"setup references {name!r} which is not in the bundle "
                        f"{self.task.path} (remote URLs are not fetched)"
                    )
                dest = str(f.get("path"))
                self._ensure_guest_dir(posixpath.dirname(dest), run, handle)
                sess.put_file(str(src), dest)
                # docker cp stages as root; hand the file to the unprivileged exec user
                # (best-effort: a non-Docker provider may stage with correct ownership).
                uid_rc, uid_out, _ = run(["id", "-u"])
                owner = uid_out.strip() if uid_rc == 0 and uid_out.strip() else "1000"
                with contextlib.suppress(Exception):
                    self._root_exec(handle)(["chown", owner, dest])
        elif kind == "execute":
            command = params.get("command", "")
            # OSWorld-shape configs carry the command as a string (shell) OR an argv list
            # (the released CUA-Gym bundles use ["bash", "-c", ...]).
            argv = (
                [str(c) for c in command]
                if isinstance(command, list)
                else ["sh", "-c", str(command)]
            )
            rc, out, err = run(argv, timeout=self.setup_timeout)
            if rc != 0:
                raise CuaGymError(f"setup command failed (rc={rc}): {command!r}: {err or out}")
        elif kind == "launch":
            command = params.get("command", "")
            argv = (
                [str(c) for c in command]
                if isinstance(command, list)
                else ["sh", "-c", str(command)]
            )
            run(argv, detach=True)
        elif kind == "sleep":
            time.sleep(float(params.get("seconds", 1)))
        elif kind == "open":
            # open a file with the desktop's default handler (OSWorld vocabulary); the
            # session environment supplies DISPLAY for GUI handlers.
            run(["xdg-open", str(params.get("path", ""))], detach=True)
        else:
            raise CuaGymError(f"unsupported setup step type: {kind!r}")

    def _root_exec(self, handle: Any = None) -> GuestExec:
        """Root-level guest channel for provisioning (docker exec -u 0); the typed ACI
        exec runs as the unprivileged session user by design."""
        handle = handle if handle is not None else self._handle
        return _DockerExec(
            str(getattr(self.provider, "docker_bin", "docker")),
            str(
                (getattr(handle, "metadata", None) or {}).get("container_id")
                or getattr(handle, "sandbox_id", "")
            ),
            user="0",
        )

    def _ensure_guest_dir(self, path: str, run: GuestExec, handle: Any = None) -> None:
        """Make a download destination directory exist and be writable by the guest exec
        user. Task bundles assume OSWorld-style paths (``/home/user/...``) that a lean
        image may not ship; a plain ``mkdir -p`` covers writable parents, and on a
        permission failure a root-level ``docker exec`` provisions + chowns the directory
        (non-Docker providers must ship images with the expected paths)."""
        if not path or path == "/":
            return
        rc, _, _ = run(["mkdir", "-p", path])
        if rc == 0:
            return
        root_run = self._root_exec(handle)
        uid_rc, uid_out, _ = run(["id", "-u"])
        owner = uid_out.strip() if uid_rc == 0 and uid_out.strip() else "1000"
        rc, _, err = root_run(
            ["sh", "-c", f"mkdir -p {shlex.quote(path)} && chown -R {owner} {shlex.quote(path)}"]
        )
        if rc != 0:
            raise CuaGymError(f"cannot provision guest dir {path!r}: {err}")

    def evaluate(self) -> float:
        """Run the bundle's ``reward.py`` inside the current replica and parse its
        ``REWARD: X.X`` line (their contract). Raises :class:`CuaGymError` when the script
        fails or emits no reward line — a broken scorer is typed, never a fake 0.0."""
        result = self.run_python(self.task.reward_script)
        rc = result.get("returncode")
        if rc != 0:
            raise CuaGymError(
                f"reward.py failed (rc={rc}): "
                f"{str(result.get('error') or result.get('output'))[:300]}"
            )
        reward = parse_reward(str(result.get("output", "")))
        if reward is None:
            raise CuaGymError(
                f"reward.py failed with no 'REWARD: X.X' line (rc={rc}): "
                f"{str(result.get('error') or result.get('output'))[:300]}"
            )
        return reward

    def _teardown_replica(self) -> None:
        session_error: Exception | None = None
        if self._sess is not None:
            try:
                self._sess.close()
            except Exception as exc:
                session_error = exc
            else:
                self._sess = None
        destroy_error: Exception | None = None
        if self._handle is not None:
            try:
                self.provider.destroy(self._handle)
            except Exception as exc:
                destroy_error = exc
            else:
                self._handle = None
                # A successfully destroyed substrate makes a failed connection close moot;
                # retain no unusable session object, but still surface its close error below.
                self._sess = None
        self._exec = None
        build_cleanup_error: Exception | None = None
        try:
            self._drain_build_cleanup()
        except Exception as exc:
            build_cleanup_error = exc
        if destroy_error is not None:
            if session_error is not None:
                raise destroy_error from session_error
            if build_cleanup_error is not None:
                raise destroy_error from build_cleanup_error
            raise destroy_error
        if session_error is not None:
            if build_cleanup_error is not None:
                raise session_error from build_cleanup_error
            raise session_error
        if build_cleanup_error is not None:
            raise build_cleanup_error

    def _drain_build_cleanup(self) -> None:
        errors: list[Exception] = []
        remaining_sessions = []
        for sess in self._pending_build_sessions:
            try:
                sess.close()
            except Exception as exc:
                remaining_sessions.append(sess)
                errors.append(exc)
        self._pending_build_sessions = remaining_sessions

        remaining_handles = []
        for handle in self._pending_build_handles:
            try:
                self.provider.destroy(handle)
            except Exception as exc:
                remaining_handles.append(handle)
                errors.append(exc)
        self._pending_build_handles = remaining_handles

        # Do not discard the snapshot that proves ownership while its base cleanup is still
        # unresolved. Once handles are gone, deletion failures stay queued for the next close.
        if not self._pending_build_handles:
            remaining_snapshots = []
            delete_snapshot = getattr(self.provider, "delete_snapshot", None)
            for snapshot in self._pending_build_snapshots:
                if not callable(delete_snapshot):
                    remaining_snapshots.append(snapshot)
                    errors.append(CuaGymError("provider cannot delete an aborted golden snapshot"))
                    continue
                try:
                    delete_snapshot(snapshot)
                except Exception as exc:
                    remaining_snapshots.append(snapshot)
                    errors.append(exc)
            self._pending_build_snapshots = remaining_snapshots

        if errors:
            raise errors[0]

    def close(self) -> None:
        """Tear down the current replica (mirrors their ``Env.close``: connection-level)."""
        self._teardown_replica()

    def dispose(self) -> None:
        """Full cleanup: the replica AND the golden checkpoint's snapshot image."""
        replica_error: Exception | None = None
        try:
            self._teardown_replica()
        except Exception as exc:
            replica_error = exc
        snapshot_error: Exception | None = None
        if self.golden_checkpoint is not None:
            delete_snapshot = getattr(self.provider, "delete_snapshot", None)
            if not callable(delete_snapshot):
                snapshot_error = CuaGymError("provider cannot delete the CUA-Gym golden checkpoint")
            else:
                try:
                    delete_snapshot(self.golden_checkpoint)
                except Exception as exc:
                    snapshot_error = exc
                else:
                    self.golden_checkpoint = None
        if replica_error is not None:
            if snapshot_error is not None:
                raise replica_error from snapshot_error
            raise replica_error
        if snapshot_error is not None:
            raise snapshot_error

    def __enter__(self) -> ShinkenCuaGymEnv:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.dispose()

    # --- their Env operation surface ------------------------------------------------------

    def _session(self) -> Any:
        if self._sess is None:
            raise CuaGymError("no live replica — call reset() first")
        return self._sess

    def _guest_exec(self) -> GuestExec:
        if self._exec is None:
            raise CuaGymError("no live replica — call reset() first")
        return self._exec

    @staticmethod
    def _result(rc: int, out: str, err: str) -> dict:
        # Their Flask controller's response shape: {"output", "error", "returncode"}.
        return {"output": out, "error": err, "returncode": rc}

    def _put_text(self, text: str, guest_path: str) -> None:
        """Stage ``text`` into the guest at ``guest_path``, world-readable — the transfer
        preserves host file modes and the guest exec user is unprivileged, so a 0600 temp
        file would land unreadable."""
        with tempfile.NamedTemporaryFile("w", suffix=Path(guest_path).suffix, delete=False) as f:
            f.write(text)
            local = f.name
        try:
            os.chmod(local, 0o644)
            self._session().put_file(local, guest_path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(local)

    def screenshot(self) -> bytes | None:
        """Current screen as PNG bytes (their ``Env.screenshot`` returns raw bytes)."""
        try:
            return self._session().screenshot()["bytes"]
        except CuaGymError:
            raise
        except Exception:
            return None  # their contract: None on failure, never an exception

    def execute(self, command: str) -> dict:
        """Run a shell command in the guest → ``{"output", "error", "returncode"}``."""
        rc, out, err = self._guest_exec()(["sh", "-c", command], timeout=self.exec_timeout)
        return self._result(rc, out, err)

    def run_python(self, script: str | os.PathLike) -> dict:
        """Run a python script (path or source text) in the guest with ``guest_python``."""
        text = (
            Path(script).read_text()
            if isinstance(script, str | Path) and os.path.isfile(str(script))
            else str(script)
        )
        guest_path = f"/tmp/shinken_cua_{uuid.uuid4().hex[:8]}.py"
        self._put_text(text, guest_path)
        rc, out, err = self._guest_exec()(
            [self.guest_python, guest_path], timeout=self.setup_timeout
        )
        return self._result(rc, out, err)

    def run_bash(
        self, script: str | os.PathLike, timeout: int = 30, working_dir: str | None = None
    ) -> dict:
        """Run a bash/sh script (path or source text) in the guest."""
        text = (
            Path(script).read_text()
            if isinstance(script, str | Path) and os.path.isfile(str(script))
            else str(script)
        )
        rc, out, err = self._guest_exec()(
            ["sh", "-c", text], timeout=float(timeout), workdir=working_dir
        )
        return self._result(rc, out, err)

    def upload(self, local_path: str | os.PathLike, remote_path: str) -> None:
        if not Path(local_path).exists():
            raise CuaGymError(f"local file not found: {local_path}")
        self._session().put_file(str(local_path), remote_path)

    def download(self, remote_path: str) -> bytes | None:
        out = Path(tempfile.mkdtemp(prefix="shinken-cua-")) / "download.bin"
        try:
            self._session().get_file(remote_path, str(out))
            return out.read_bytes()
        except CuaGymError:
            raise
        except Exception:
            return None  # their contract: None on failure
        finally:
            with contextlib.suppress(OSError):
                out.unlink()

    def launch(self, command: str) -> None:
        """Launch an application in the guest (non-blocking, like their ``/setup/launch``)."""
        self._guest_exec()(shlex.split(command), detach=True, timeout=self.exec_timeout)

    def get_screen_size(self) -> dict:
        """``{"width", "height"}`` (their key names; the ACI reports ``w``/``h``)."""
        size = self._session().screen_size()
        return {"width": size.get("w"), "height": size.get("h")}

    def get_accessibility_tree(self) -> str | None:
        # Parity note: CUA-Gym ships this passthrough and never calls it — its verification
        # runs on file/app state (see docs/design/tech-decisions.md D3 field priors). The
        # over-the-wire structured observation engine is not wired here yet.
        return None

    def get_directory_tree(self, path: str) -> dict:
        """List a guest directory (their ``/list_directory`` shape, best-effort)."""
        probe = (
            "import json,os,sys\n"
            "p=sys.argv[1]\n"
            "es=[{'name':e.name,'is_dir':e.is_dir()} for e in os.scandir(p)]\n"
            "print(json.dumps({'path':p,'entries':es}))\n"
        )
        guest_path = f"/tmp/shinken_cua_ls_{uuid.uuid4().hex[:8]}.py"
        self._put_text(probe, guest_path)
        rc, out, _err = self._guest_exec()(
            [self.guest_python, guest_path, path], timeout=self.exec_timeout
        )
        if rc != 0:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {}
