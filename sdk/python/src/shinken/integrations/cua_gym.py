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
  **``reset()`` is a golden-checkpoint fork.** Bundle setup runs ONCE into a base sandbox,
  is checkpointed, and every reset materializes a replica from that single checkpoint
  (sub-second on the Docker disk tier vs minutes of cloud-VM provisioning per use).

``TaskSource``/scorers are consumer-side by design (``docs/design/agent-runtime.md`` §5);
nothing here touches the runtime waist. No CUA-Gym code is imported or copied.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shinken.errors import ShinkenError

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
    return float(matches[-1]) if matches else None


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
                self._tasks[task.task_id] = task

    @staticmethod
    def _load(bundle: Path) -> tuple[CuaGymTask | None, str | None]:
        cfg_path = bundle / "config.json"
        if not cfg_path.is_file():
            return None, "no config.json"
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
#: substrate's out-of-band exec channel. exec/file-transfer are deliberately NOT ACI wire
#: verbs in v0 (docs/design/tech-decisions.md, D2 sub-decision): setup/scoring flow over
#: the provider's own channel, exactly as CUA-Gym drives OSWorld's ``/setup/execute``.
GuestExec = Callable[..., tuple[int, str, str]]


class _DockerExec:
    """Default exec channel for Docker-backed providers: ``docker exec`` on the handle's
    container (duck-typed off ``provider.docker_bin`` + ``handle.metadata['container_id']``)."""

    def __init__(self, docker_bin: str, container_id: str) -> None:
        self.docker_bin = docker_bin
        self.container_id = container_id

    def __call__(
        self,
        argv: list[str],
        *,
        timeout: float = 60.0,
        workdir: str | None = None,
        detach: bool = False,
    ) -> tuple[int, str, str]:
        cmd = [self.docker_bin, "exec"]
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
      checkpoint — sub-second on the Docker disk tier, and every replica starts from the
      byte-identical golden state.

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
        self.spec = spec
        self.exec_factory = exec_factory
        self.guest_python = guest_python
        self.setup_timeout = setup_timeout
        self.exec_timeout = exec_timeout
        self.golden_checkpoint: str | None = None
        self._handle: Any = None  # current replica
        self._sess: Any = None  # connected Shinken session for the current replica
        self._exec: GuestExec | None = None

    # --- lifecycle: golden checkpoint once, fork per reset -------------------------------

    def reset(self) -> dict:
        """Materialize a fresh replica of the task's golden state and return the first
        observation (``{"instruction", "screenshot"}``). Setup runs only on the first call."""
        if self.golden_checkpoint is None:
            self.golden_checkpoint = self._build_golden()
        self._teardown_replica()
        self._handle = self.provider.resume(self.golden_checkpoint)
        self._sess = self.provider.connect(self._handle)
        self._exec = self.exec_factory(self.provider, self._handle)
        return {"instruction": self.task.instruction, "screenshot": self.screenshot()}

    def _build_golden(self) -> str:
        """Create the base sandbox, replay the bundle's setup steps once, checkpoint the
        golden state, and destroy the base (replicas resume from the checkpoint)."""
        base = self.provider.create(self.spec)
        try:
            sess = self.provider.connect(base)
            try:
                run = self.exec_factory(self.provider, base)
                for step in self.task.setup_steps:
                    self._apply_setup_step(step, sess, run)
                return self.provider.checkpoint(base)
            finally:
                with contextlib.suppress(Exception):
                    sess.close()
        finally:
            with contextlib.suppress(Exception):
                self.provider.destroy(base)

    def _apply_setup_step(self, step: dict, sess: Any, run: GuestExec) -> None:
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
                sess.put_file(str(src), str(f.get("path")))
        elif kind == "execute":
            command = str(params.get("command", ""))
            rc, out, err = run(["sh", "-c", command], timeout=self.setup_timeout)
            if rc != 0:
                raise CuaGymError(f"setup command failed (rc={rc}): {command!r}: {err or out}")
        else:
            raise CuaGymError(f"unsupported setup step type: {kind!r}")

    def evaluate(self) -> float:
        """Run the bundle's ``reward.py`` inside the current replica and parse its
        ``REWARD: X.X`` line (their contract). Raises :class:`CuaGymError` when the script
        fails or emits no reward line — a broken scorer is typed, never a fake 0.0."""
        result = self.run_python(self.task.reward_script)
        reward = parse_reward(str(result.get("output", "")))
        if reward is None:
            rc = result.get("returncode")
            raise CuaGymError(
                f"reward.py produced no 'REWARD: X.X' line (rc={rc}): "
                f"{str(result.get('error') or result.get('output'))[:300]}"
            )
        return reward

    def _teardown_replica(self) -> None:
        if self._sess is not None:
            with contextlib.suppress(Exception):
                self._sess.close()
            self._sess = None
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self.provider.destroy(self._handle)
            self._handle = None
        self._exec = None

    def close(self) -> None:
        """Tear down the current replica (mirrors their ``Env.close``: connection-level)."""
        self._teardown_replica()

    def dispose(self) -> None:
        """Full cleanup: the replica AND the golden checkpoint's snapshot image."""
        self._teardown_replica()
        if self.golden_checkpoint is not None and hasattr(self.provider, "delete_snapshot"):
            with contextlib.suppress(Exception):
                self.provider.delete_snapshot(self.golden_checkpoint)
        self.golden_checkpoint = None

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
