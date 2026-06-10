"""swerex-shaped deployment/runtime backed by a Shinken provider + session.

uni-agent (https://github.com/verl-project/uni-agent, studied at commit
``75788ab91980115fe847863fe97246b311a43f42``) reaches every sandbox backend through the
SWE-ReX protocol (https://github.com/SWE-agent/SWE-ReX): an async **deployment**
(``start`` / ``stop`` / ``is_alive`` + a ``runtime`` property) whose **runtime** exposes
``create_session`` / ``run_in_session`` / ``execute`` / ``read_file`` / ``write_file`` /
``upload`` / ``close``. Its ``AgentEnv`` drives only that surface, so anything
deployment-shaped is a uni-agent (and therefore verl-rollout) sandbox backend.

This module implements that shape fresh on Shinken's own surfaces (no SWE-ReX code is
vendored; the protocol semantics are reimplemented):

================================  =====================================================
swerex operation                  Shinken surface
================================  =====================================================
``Deployment.start()``            ``provider.create(spec)`` + ``provider.connect()``
                                  (ACI handshake = readiness); or fork-native:
                                  ``provider.resume(<golden checkpoint>)`` (D5/#206)
``Deployment.stop()``             session ``close()`` + ``provider.destroy(handle)``
``Deployment.is_alive()``         session ``ping()`` (ACI control plane)
``runtime.create_session``        registered bash session emulated over substrate exec
                                  (env + cwd persisted in a guest state file)
``runtime.run_in_session``        substrate exec ``bash -lc`` (the same channel
                                  ``shinken.inject`` uses), exit code + output captured
``runtime.execute``               one-shot substrate exec (argv or shell)
``runtime.read_file``             ``sandbox.get_file`` (hash-verified transfer, #85)
``runtime.write_file``            ``sandbox.put_file`` (+ ``mkdir -p`` of the parent)
``runtime.upload``                ``sandbox.put_file`` per file (dirs walked)
``runtime.close``                 drop session state files
================================  =====================================================

**Out-of-tree rule:** this module never imports ``swerex`` or ``uni_agent`` at import
time. Requests/actions are duck-typed (any object with the protocol's attribute names
works); responses/exceptions use the real ``swerex`` classes when that package happens to
be installed (so consumer-side ``isinstance``/``except`` clauses hold) and fall back to
local stand-ins with identical field names otherwise.

**Fidelity note:** sessions are emulated — each ``run_in_session`` is a bounded
``bash -lc`` execution with exported environment + cwd persisted to a per-session guest
state file, not a long-lived PTY. Consequently ``expect`` patterns and interactive
programs are out of scope, and a ``BashInterruptAction`` is a successful no-op (every
command is already bounded by its own host-side timeout). uni-agent's ``AgentEnv`` only
relies on output/exit-code/env/cwd semantics, which are preserved.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ShinkenDeployment",
    "ShinkenDeploymentConfig",
    "ShinkenRuntime",
    "DockerExecExecutor",
    "GuestExecutor",
]


# ---------------------------------------------------------------------------
# Protocol response/exception shapes — real swerex classes when installed,
# duck-typed stand-ins (same field names) otherwise. Never imported at module
# import time.
# ---------------------------------------------------------------------------


@dataclass
class IsAliveResponse:
    is_alive: bool
    message: str = ""

    def __bool__(self) -> bool:
        return self.is_alive


@dataclass
class CreateBashSessionResponse:
    output: str = ""
    session_type: str = "bash"


@dataclass
class BashObservation:
    output: str = ""
    exit_code: int | None = None
    failure_reason: str = ""
    expect_string: str = ""
    session_type: str = "bash"


@dataclass
class CloseBashSessionResponse:
    session_type: str = "bash"


@dataclass
class CommandResponse:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None


@dataclass
class ReadFileResponse:
    content: str = ""


@dataclass
class WriteFileResponse:
    pass


@dataclass
class UploadResponse:
    pass


@dataclass
class CloseResponse:
    pass


class SwerexException(RuntimeError):
    """Fallback base for protocol errors when ``swerex`` is not installed."""


class CommandTimeoutError(SwerexException):
    pass


class NonZeroExitCodeError(SwerexException):
    pass


class SessionNotInitializedError(SwerexException):
    pass


class SessionExistsError(SwerexException):
    pass


class DeploymentNotStartedError(SwerexException):
    pass


_FALLBACK_RESPONSES: dict[str, type] = {
    "IsAliveResponse": IsAliveResponse,
    "CreateBashSessionResponse": CreateBashSessionResponse,
    "BashObservation": BashObservation,
    "CloseBashSessionResponse": CloseBashSessionResponse,
    "CommandResponse": CommandResponse,
    "ReadFileResponse": ReadFileResponse,
    "WriteFileResponse": WriteFileResponse,
    "UploadResponse": UploadResponse,
    "CloseResponse": CloseResponse,
}

_FALLBACK_EXCEPTIONS: dict[str, type[Exception]] = {
    "CommandTimeoutError": CommandTimeoutError,
    "NonZeroExitCodeError": NonZeroExitCodeError,
    "SessionNotInitializedError": SessionNotInitializedError,
    "SessionExistsError": SessionExistsError,
    "DeploymentNotStartedError": DeploymentNotStartedError,
}


def _resp(kind: str, **kwargs: Any) -> Any:
    """Build a protocol response: the real swerex model if importable (so pydantic
    validation and isinstance checks in consumers hold), else the local stand-in."""
    try:
        from swerex.runtime import abstract as _abs  # lazy by design (out-of-tree rule)
    except Exception:
        _abs = None
    if _abs is not None:
        cls = getattr(_abs, kind, None)
        if isinstance(cls, type):
            try:
                return cls(**kwargs)
            except Exception:
                pass  # field drift in an unknown swerex version → duck-typed stand-in
    return _FALLBACK_RESPONSES[kind](**kwargs)


def _exc(*candidates: str) -> type[Exception]:
    """Resolve a protocol exception class: first matching name from the installed
    ``swerex.exceptions`` (so consumer ``except`` clauses catch it), else the local
    fallback named by the first candidate."""
    try:
        from swerex import exceptions as _ex  # lazy by design (out-of-tree rule)
    except Exception:
        _ex = None
    if _ex is not None:
        for name in candidates:
            cls = getattr(_ex, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                return cls
    return _FALLBACK_EXCEPTIONS[candidates[0]]


# ---------------------------------------------------------------------------
# Substrate exec channel — how shell commands reach the guest. Docker is the
# in-tree reference (the same `docker exec` channel shinken.inject uses); other
# substrates plug in by implementing GuestExecutor.
# ---------------------------------------------------------------------------


class _Request:
    """Minimal attribute bag for internally-issued protocol requests."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


@runtime_checkable
class GuestExecutor(Protocol):
    """Run a command inside the sandbox; return ``(exit_code, stdout, stderr)``.

    On timeout, implementations raise :class:`subprocess.TimeoutExpired` (or
    :class:`TimeoutError`); the runtime converts it to the protocol's timeout error."""

    def run_argv(self, argv: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
        """Execute an argv directly (no shell)."""
        ...

    def run_script(self, script: str, *, timeout: float | None = None) -> tuple[int, str, str]:
        """Execute a shell script via the guest's bash."""
        ...


@dataclass
class DockerExecExecutor:
    """Substrate exec over ``docker exec`` — the channel :mod:`shinken.inject` proves."""

    container_id: str
    docker_bin: str = "docker"
    shell: str = "/bin/bash"
    _exec_user: tuple[str, str] | None = field(default=None, init=False, repr=False)

    def _run(self, cmd: list[str], timeout: float | None) -> tuple[int, str, str]:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr

    def run_argv(self, argv: list[str], *, timeout: float | None = None) -> tuple[int, str, str]:
        return self._run([self.docker_bin, "exec", self.container_id, *argv], timeout)

    def run_script(self, script: str, *, timeout: float | None = None) -> tuple[int, str, str]:
        cmd = [self.docker_bin, "exec", self.container_id, self.shell, "-lc", script]
        return self._run(cmd, timeout)

    def fixup_path(self, path: str, *, timeout: float | None = None) -> None:
        """``docker cp`` preserves the HOST file's uid/gid (and mode), which the
        container's default exec user typically cannot read — let alone ``chmod +x``
        (uni-agent's tool install does exactly that after a copy). Re-own the path to
        that user via a root exec (a Docker capability, not a guest one). Best-effort:
        readability is additionally guaranteed by the host-side mode set in
        ``write_file``."""
        try:
            if self._exec_user is None:
                rc_u, uid, _ = self._run(
                    [self.docker_bin, "exec", self.container_id, "id", "-u"], timeout
                )
                rc_g, gid, _ = self._run(
                    [self.docker_bin, "exec", self.container_id, "id", "-g"], timeout
                )
                if rc_u != 0 or rc_g != 0:
                    return
                self._exec_user = (uid.strip(), gid.strip())
            uid, gid = self._exec_user
            self._run(
                [
                    self.docker_bin,
                    "exec",
                    "-u",
                    "0",
                    self.container_id,
                    "chown",
                    "-R",
                    f"{uid}:{gid}",
                    path,
                ],
                timeout,
            )
        except Exception:
            pass  # rootless/odd daemons: leave ownership as docker cp made it


# ---------------------------------------------------------------------------
# Runtime — the swerex AbstractRuntime shape over one Shinken session.
# ---------------------------------------------------------------------------


class ShinkenRuntime:
    """swerex ``AbstractRuntime``-shaped facade over a Shinken Sandbox session.

    ``sandbox`` is a connected :class:`shinken.Sandbox` (provider-managed, so file
    transfer crosses the real guest boundary); ``executor`` is the substrate exec
    channel for shell commands (``None`` disables exec-backed operations with a clear
    error — file transfer still works)."""

    def __init__(
        self,
        sandbox: Any,
        executor: GuestExecutor | None = None,
        *,
        default_timeout: float = 60.0,
    ) -> None:
        self.sandbox = sandbox
        self.executor = executor
        self.default_timeout = default_timeout
        self._nonce = uuid.uuid4().hex[:8]
        self._sessions: dict[str, str] = {}  # session name -> guest state-file path

    # -- helpers ------------------------------------------------------------

    def _require_executor(self) -> GuestExecutor:
        if self.executor is None:
            raise RuntimeError(
                "this runtime has no substrate exec channel (GuestExecutor); shell "
                "operations need one — DockerExecExecutor is attached automatically for "
                "docker-backed handles, other substrates must pass executor= explicitly"
            )
        return self.executor

    def _state_path(self, session: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in session)
        return f"/tmp/.shinken-swerex-{self._nonce}-{safe}.env"

    def _session_script(self, state: str, command: str) -> str:
        """Wrap ``command`` so exported env + cwd persist across calls of a session."""
        q = shlex.quote(state)
        return (
            f"__shk_state={q}\n"
            '[ -f "$__shk_state" ] && . "$__shk_state" >/dev/null 2>&1\n'
            '[ -n "${__SHINKEN_CWD:-}" ] && cd "$__SHINKEN_CWD" 2>/dev/null\n'
            f"{command}\n"
            "__shk_rc=$?\n"
            "{ export -p; printf 'export __SHINKEN_CWD=%q\\n' \"$PWD\"; } "
            '> "$__shk_state" 2>/dev/null\n'
            "exit $__shk_rc\n"
        )

    async def _exec_script(self, script: str, timeout: float | None) -> tuple[int, str, str]:
        executor = self._require_executor()
        try:
            return await asyncio.to_thread(executor.run_script, script, timeout=timeout)
        except (TimeoutError, subprocess.TimeoutExpired) as e:
            raise _exc("CommandTimeoutError")(f"command timed out after {timeout}s") from e

    # -- protocol surface ----------------------------------------------------

    async def is_alive(self, *, timeout: float | None = None) -> Any:
        try:
            ping = asyncio.to_thread(self.sandbox.ping)
            if timeout is not None:
                await asyncio.wait_for(ping, timeout)
            else:
                await ping
        except Exception as e:  # liveness probes report, never raise
            return _resp("IsAliveResponse", is_alive=False, message=str(e))
        return _resp("IsAliveResponse", is_alive=True, message="")

    async def wait_until_alive(self, *, timeout: float = 60.0) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            alive = await self.is_alive(timeout=min(timeout, 5.0))
            if getattr(alive, "is_alive", False):
                return alive
            if time.monotonic() >= deadline:
                raise TimeoutError(f"runtime not alive within {timeout}s: {alive.message}")
            await asyncio.sleep(0.2)

    async def create_session(self, request: Any = None) -> Any:
        name = str(getattr(request, "session", "default"))
        if name in self._sessions:
            raise _exc("SessionExistsError")(f"session {name!r} already exists")
        state = self._state_path(name)
        sources = list(getattr(request, "startup_source", []) or [])
        timeout = getattr(request, "startup_timeout", None) or self.default_timeout
        init = "\n".join(f". {shlex.quote(str(s))} >/dev/null 2>&1 || true" for s in sources)
        script = self._session_script(state, init or "true")
        rc, out, err = await self._exec_script(script, timeout)
        if rc != 0:
            raise _exc("NonZeroExitCodeError", "CommandFailedError")(
                f"session {name!r} bootstrap failed (rc={rc}): {(out + err)[:400]}"
            )
        self._sessions[name] = state
        return _resp("CreateBashSessionResponse", output=out + err)

    async def run_in_session(self, action: Any) -> Any:
        # BashInterruptAction (or anything command-less): every command here is already
        # bounded by its own host-side timeout, so there is no foreground process left to
        # interrupt — report success (the session stays usable).
        if getattr(action, "type", None) == "bash_interrupt" or not hasattr(action, "command"):
            return _resp("BashObservation", output="", exit_code=0, failure_reason="")
        name = str(getattr(action, "session", "default"))
        state = self._sessions.get(name)
        if state is None:
            raise _exc("SessionNotInitializedError")(
                f"session {name!r} does not exist; call create_session first"
            )
        timeout = getattr(action, "timeout", None) or self.default_timeout
        rc, out, err = await self._exec_script(self._session_script(state, action.command), timeout)
        output = out + err
        if getattr(action, "check", "ignore") == "raise" and rc != 0:
            raise _exc("NonZeroExitCodeError", "CommandFailedError")(
                f"command failed (rc={rc}): {output[:400]}"
            )
        return _resp("BashObservation", output=output, exit_code=rc, failure_reason="")

    async def close_session(self, request: Any = None) -> Any:
        name = str(getattr(request, "session", "default"))
        state = self._sessions.pop(name, None)
        if state is None:
            raise _exc("SessionNotInitializedError")(f"session {name!r} does not exist")
        if self.executor is not None:
            try:
                await self._exec_script(f"rm -f {shlex.quote(state)}", 10.0)
            except Exception:
                pass  # best-effort cleanup; the sandbox may already be gone
        return _resp("CloseBashSessionResponse")

    async def execute(self, command: Any) -> Any:
        executor = self._require_executor()
        cmd = command.command
        timeout = getattr(command, "timeout", None) or self.default_timeout
        shell = bool(getattr(command, "shell", False))
        try:
            if isinstance(cmd, str) or shell:
                script = cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
                rc, out, err = await asyncio.to_thread(executor.run_script, script, timeout=timeout)
            else:
                rc, out, err = await asyncio.to_thread(
                    executor.run_argv, list(cmd), timeout=timeout
                )
        except (TimeoutError, subprocess.TimeoutExpired) as e:
            raise _exc("CommandTimeoutError")(f"command timed out after {timeout}s") from e
        if getattr(command, "check", False) and rc != 0:
            raise _exc("NonZeroExitCodeError", "CommandFailedError")(
                f"command failed (rc={rc}): {err[:400] or out[:400]}"
            )
        return _resp("CommandResponse", stdout=out, stderr=err, exit_code=rc)

    async def read_file(self, request: Any) -> Any:
        path = str(request.path)
        fd, tmp = tempfile.mkstemp(prefix="shinken-swerex-read-")
        os.close(fd)
        try:
            await asyncio.to_thread(self.sandbox.get_file, path, tmp)
            content = Path(tmp).read_text(
                encoding=getattr(request, "encoding", None),
                errors=getattr(request, "errors", None),
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return _resp("ReadFileResponse", content=content)

    async def write_file(self, request: Any) -> Any:
        path = str(request.path)
        await self._ensure_parent_dir(path)
        fd, tmp = tempfile.mkstemp(prefix="shinken-swerex-write-")
        os.close(fd)
        try:
            Path(tmp).write_text(str(request.content))
            # mkstemp creates 0600 host-owned temps; transports like `docker cp`
            # preserve that, leaving the guest user unable to read what it "wrote".
            os.chmod(tmp, 0o644)
            await asyncio.to_thread(self.sandbox.put_file, tmp, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        await self._fixup_guest_path(path)
        return _resp("WriteFileResponse")

    async def upload(self, request: Any) -> Any:
        source = Path(str(request.source_path))
        target = str(request.target_path)
        if source.is_dir():
            for f in sorted(p for p in source.rglob("*") if p.is_file()):
                guest = str(PurePosixPath(target) / f.relative_to(source).as_posix())
                await self._ensure_parent_dir(guest)
                await asyncio.to_thread(self.sandbox.put_file, str(f), guest)
            await self._fixup_guest_path(target)
        elif source.is_file():
            await self._ensure_parent_dir(target)
            await asyncio.to_thread(self.sandbox.put_file, str(source), target)
            await self._fixup_guest_path(target)
        else:
            raise ValueError(f"source path {source} is not a file or directory")
        return _resp("UploadResponse")

    async def _ensure_parent_dir(self, guest_path: str) -> None:
        """`docker cp`-style transfers need the destination directory to exist."""
        parent = str(PurePosixPath(guest_path).parent)
        if self.executor is not None and parent not in ("/", "."):
            await asyncio.to_thread(
                self._require_executor().run_argv,
                ["mkdir", "-p", parent],
                timeout=self.default_timeout,
            )

    async def _fixup_guest_path(self, guest_path: str) -> None:
        """Give executors a post-transfer hook (e.g. Docker re-owns `docker cp`'d files
        to the guest user, so the guest can read and ``chmod +x`` them)."""
        fixup = getattr(self.executor, "fixup_path", None)
        if callable(fixup):
            await asyncio.to_thread(fixup, guest_path, timeout=self.default_timeout)

    async def close(self) -> Any:
        for name in list(self._sessions):
            try:
                await self.close_session(_Request(session=name))
            except Exception:
                pass
        return _resp("CloseResponse")


# ---------------------------------------------------------------------------
# Deployment — the swerex AbstractDeployment shape over a Shinken provider.
# ---------------------------------------------------------------------------


class ShinkenDeployment:
    """swerex ``AbstractDeployment``-shaped sandbox lifecycle over a Shinken provider.

    Three start modes:

    - **cold boot** (default): ``provider.create(spec)`` builds a fresh sandbox;
    - **fork-native**: pass ``checkpoint=<golden checkpoint id>`` and every ``start()``
      materializes from that committed golden state via ``provider.resume`` — the
      runtime-state loop ``eval.run_eval_forked`` proves (D5/#206), exposed at the
      uni-agent/verl seam so RL rollouts reset from golden instead of cold-booting;
    - **attach**: pass ``handle=`` for a sandbox managed elsewhere (``stop()`` then
      closes the session but never destroys the sandbox).

    ``executor`` overrides the substrate exec channel; by default a
    :class:`DockerExecExecutor` is attached when the handle carries a container id
    (DockerLocalProvider does).
    """

    def __init__(
        self,
        provider: Any,
        *,
        spec: Any = None,
        checkpoint: str | None = None,
        handle: Any = None,
        executor: GuestExecutor | None = None,
        startup_source: list[str] | None = None,
        default_timeout: float = 60.0,
        retry_backoff: float = 1.0,
        run_id: str | None = None,
    ) -> None:
        self.provider = provider
        self.spec = spec
        self.checkpoint = checkpoint
        self.startup_source = list(startup_source or [])
        self.default_timeout = default_timeout
        self.retry_backoff = retry_backoff
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._attached_handle = handle
        self._executor_override = executor
        self.handle: Any = None
        self.sandbox: Any = None
        self._runtime: ShinkenRuntime | None = None
        self._hooks: list[Any] = []

    # -- protocol surface ----------------------------------------------------

    def add_hook(self, hook: Any) -> None:
        self._hooks.append(hook)

    def _notify(self, message: str) -> None:
        for hook in self._hooks:
            fn = getattr(hook, "on_custom_step", None)
            if callable(fn):
                try:
                    fn(message)
                except Exception:
                    pass

    @property
    def runtime(self) -> ShinkenRuntime:
        if self._runtime is None:
            raise _exc("DeploymentNotStartedError")("deployment not started")
        return self._runtime

    @property
    def tool_install_dir(self) -> Path:
        """Directory inside the sandbox where consumer tool scripts are installed."""
        return Path("/usr/local/bin")

    async def is_alive(self, *, timeout: float | None = None) -> Any:
        if self._runtime is None:
            raise _exc("DeploymentNotStartedError")("deployment not started")
        return await self._runtime.is_alive(timeout=timeout)

    async def start(self, max_retries: int = 1) -> None:
        last: Exception | None = None
        for attempt in range(max(1, max_retries)):
            self._notify("Creating Shinken sandbox")
            try:
                await self._start_once()
                return
            except Exception as e:
                last = e
                await self.stop()
                if attempt < max_retries - 1 and self.retry_backoff:
                    await asyncio.sleep(min(self.retry_backoff * 2**attempt, 30.0))
        raise RuntimeError(
            f"failed to start Shinken deployment after {max_retries} tries"
        ) from last

    async def _start_once(self) -> None:
        if self._attached_handle is not None:
            self.handle = self._attached_handle
        elif self.checkpoint is not None:
            # Fork-native start: materialize from the golden checkpoint, not a cold boot.
            self.handle = await asyncio.to_thread(self.provider.resume, self.checkpoint)
        else:
            self.handle = await asyncio.to_thread(self.provider.create, self.spec)
        # provider.connect completes the ACI handshake (this is the readiness probe) and
        # attaches the guest file-transfer transport + runtime-state context.
        self.sandbox = await asyncio.to_thread(self.provider.connect, self.handle)
        self._runtime = ShinkenRuntime(
            self.sandbox,
            self._executor_override or self._default_executor(),
            default_timeout=self.default_timeout,
        )
        if self._runtime.executor is not None:
            # Parity with swerex deployments: a ready deployment has its default bash
            # session open, so consumers can communicate immediately.
            await self._runtime.create_session(
                _Request(session="default", startup_source=self.startup_source)
            )

    def _default_executor(self) -> GuestExecutor | None:
        """Docker-backed handles get the `docker exec` channel automatically; other
        substrates supply ``executor=`` (returning None leaves exec ops cleanly errored)."""
        meta = getattr(self.handle, "metadata", None) or {}
        container = meta.get("container_id") or (
            getattr(self.handle, "sandbox_id", None)
            if getattr(self.provider.capabilities, "name", "") == "docker-local"
            else None
        )
        if container:
            return DockerExecExecutor(
                str(container), docker_bin=getattr(self.provider, "docker_bin", "docker")
            )
        return None

    async def stop(self) -> None:
        if self._runtime is not None:
            try:
                await self._runtime.close()
            except Exception:
                pass
            self._runtime = None
        if self.sandbox is not None:
            try:
                await asyncio.to_thread(self.sandbox.close)
            except Exception:
                pass
            self.sandbox = None
        # Destroy only sandboxes this deployment created; attached ones are not ours.
        if self.handle is not None and self._attached_handle is None:
            try:
                await asyncio.to_thread(self.provider.destroy, self.handle)
            except Exception:
                pass
        self.handle = None

    async def __aenter__(self) -> ShinkenDeployment:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()


@dataclass
class ShinkenDeploymentConfig:
    """Config-object shape matching uni-agent's ``*DeploymentConfig`` convention:
    a ``type`` discriminator plus ``get_deployment(run_id)``. Plain dataclass (the
    Shinken SDK carries no pydantic dependency); a uni-agent fork that wants it inside
    the ``DeployConfig`` union wraps it in a pydantic model with these same fields."""

    provider: str = "docker"  # shinken provider registry name (see shinken.providers)
    provider_kwargs: dict[str, Any] = field(default_factory=dict)
    image: str | None = None
    checkpoint: str | None = None  # golden checkpoint id -> fork-native start()
    startup_source: list[str] = field(default_factory=list)
    timeout: float = 60.0
    type: str = "shinken"

    def get_deployment(self, run_id: str) -> ShinkenDeployment:
        from shinken import providers as _providers

        provider = _providers.get(self.provider, **self.provider_kwargs)
        spec = _providers.SandboxSpec(image=self.image) if self.image else None
        return ShinkenDeployment(
            provider,
            spec=spec,
            checkpoint=self.checkpoint,
            startup_source=self.startup_source,
            default_timeout=self.timeout,
            run_id=run_id,
        )
