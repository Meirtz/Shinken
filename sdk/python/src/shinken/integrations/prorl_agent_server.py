"""ProRL-Agent-Server runtime plugin — serve Shinken sandboxes to an RL rollout server.

`ProRL-Agent-Server <https://github.com/NVIDIA-NeMo/ProRL-Agent-Server>`__ (Apache-2.0;
paper: *ProRL Agent: Rollout-as-a-Service for RL Training of Multi-Turn LLM Agents*,
<https://arxiv.org/abs/2603.18815>) is a rollout-as-a-service control plane for agent RL:
trainers submit task instances over HTTP and receive scored, token-level trajectories,
while distributed gateway nodes run an INIT -> RUN -> EVAL assembly line. Each rollout
session gets **one long-lived sandbox runtime**; custom runtime backends are loaded from
``RuntimeSpec.import_path`` (``"module:Class"``, validated upstream as a subclass of
``polar.runtime.base.BaseRuntime``).

This module implements that runtime contract on a Shinken provider-managed sandbox, so a
rollout server can use Shinken GUI sandboxes (the proven Linux/X11 slice) as its
per-session environment:

.. code-block:: yaml

    # topology.yaml (rollout-server side)
    runtime:
      import_path: "shinken.integrations.prorl_agent_server:ShinkenRuntime"
      image: "shinken/sandbox-linux"
      kwargs:
        provider: docker                     # Shinken provider-registry name
        golden_snapshot: "shinken-snap:..."  # optional: INIT = resume-from-golden (D5)

Contract implemented (mirrors ``polar.runtime.base.BaseRuntime`` verbatim):

- ``__init__(spec, session_id, session_dir)`` — one instance per rollout session.
- ``async start() / stop() / cancel()`` — lifecycle; ``stop`` is idempotent.
- ``async exec(command, *, cwd=None, env=None, timeout_sec=None) -> ExecResult`` —
  ``bash -lc`` inside the sandbox; default cwd is ``cwd or spec.workdir or
  /polar/session``; a timeout is reported as ``return_code == -1`` (the value the
  gateway maps to ``"timeout"``).
- ``async upload_file / upload_dir / download_file / download_dir`` — host <-> guest
  copies (guest paths must be absolute, no ``..``).
- Capability properties: CPU and memory limits are supported (mapped onto the Shinken
  ``SandboxSpec``); GPUs, storage limits, and internet-off are **not** advertised — the
  upstream factory then rejects specs this backend cannot honor, instead of silently
  ignoring them.

What the mapping buys an RL rollout server:

- **INIT stage = resume-from-golden.** ``kwargs.golden_snapshot`` (a Shinken
  snapshot/checkpoint id) makes ``start()`` materialize the session from a prepared
  golden state via ``provider.resume`` instead of cold-booting, the same primitive behind
  ``shinken.eval.run_eval_forked``.
- **A GUI ACI inside the sandbox.** The Guest Runtime (``shinkend``) listens on
  ``127.0.0.1:8765`` in-guest with ``SHINKEND_TOKEN`` already present in the container
  environment, so the harness command launched by the RUN stage can drive real
  pointer/keyboard/screenshot/screencast observation-action with the Shinken SDK.
  ``exec`` injects ``SHINKEND_ADDR=127.0.0.1:8765`` (caller env wins); the token is never
  placed on a host command line.
- **Host-side ACI for evaluators.** :meth:`ShinkenRuntime.connect_sandbox` opens the
  provider-attached session (screenshot, actions, ``put_file``/``get_file``,
  checkpoint/fork) for custom EVAL-stage scoring.

Caveats (documented, not hidden):

- **No host bind-mount.** Upstream container backends bind-mount the host session dir at
  ``/polar/session``; Shinken's provider does not, so host-side writes into
  ``session_dir`` are *not* implicitly visible in-guest. The prepare/exec/upload/download
  contract is fully supported; the one upstream path that relies on the bind-mount (the
  patch-apply evaluator, which writes ``patch.diff`` host-side and ``git apply``\\ s the
  in-guest path) needs ``kwargs.push_session_dir_on_exec: true``, which pushes the host
  session dir into the guest before every ``exec``.
- ``exec`` and the copy verbs need a container-substrate provider (a handle that exposes
  a container id — the in-tree ``docker`` provider does); ``spec.network`` is
  provider-managed and ignored.

No hard dependency: when ``polar`` (the ProRL-Agent-Server package) is importable, the
class genuinely subclasses its ``BaseRuntime`` (passing the upstream ``issubclass`` check
at plugin load); otherwise a minimal local shim with the same attribute surface stands
in, so this module imports, unit-tests, and duck-types on a clean Shinken checkout.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from shinken import providers as _providers
from shinken.providers import SandboxSpec
from shinken.providers.docker import _validate_guest_path

# In-guest path contract (mirrors polar.runtime.base verbatim — these names are the
# published contract of the external project, fixed whether or not it is installed).
RUNTIME_SESSION_DIR = "/polar/session"
RUNTIME_ARTIFACTS_DIR = f"{RUNTIME_SESSION_DIR}/artifacts"
RUNTIME_LOGS_DIR = f"{RUNTIME_SESSION_DIR}/logs"
RUNTIME_AGENT_LOG_DIR = f"{RUNTIME_LOGS_DIR}/agent"
RUNTIME_EVAL_LOG_DIR = f"{RUNTIME_LOGS_DIR}/eval"
RUNTIME_EVAL_ARTIFACT_DIR = f"{RUNTIME_SESSION_DIR}/eval_artifacts"

# Where shinkend listens inside the sandbox (the provider publishes it to the host).
GUEST_ACI_ADDR = "127.0.0.1:8765"


@dataclass
class ExecResult:
    """Local stand-in for ``polar.runtime.models.ExecResult`` (same field names)."""

    stdout: str | None = None
    stderr: str | None = None
    return_code: int = 0


class _ShimBaseRuntime:
    """Stand-in for ``polar.runtime.base.BaseRuntime`` when ProRL-Agent-Server is not
    installed — the same constructor/attribute/cancel surface, so the plugin imports and
    is fixture-testable without the external package."""

    def __init__(self, spec: Any, session_id: str, session_dir: Path) -> None:
        self.spec = spec
        self.session_id = session_id
        self.session_dir = Path(session_dir)
        self.artifacts_dir = self.session_dir / "artifacts"
        self.runtime_session_dir = RUNTIME_SESSION_DIR
        self.runtime_artifacts_dir = RUNTIME_ARTIFACTS_DIR
        self.runtime_logs_dir = RUNTIME_LOGS_DIR
        self.runtime_agent_log_dir = RUNTIME_AGENT_LOG_DIR
        self._active_process: asyncio.subprocess.Process | None = None
        self._destroyed = False

    @property
    def supports_gpus(self) -> bool:
        return False

    @property
    def can_disable_internet(self) -> bool:
        return False

    @property
    def supports_cpu_limits(self) -> bool:
        return False

    @property
    def supports_memory_limits(self) -> bool:
        return False

    @property
    def supports_storage_limits(self) -> bool:
        return False

    async def cancel(self) -> None:
        """Stop any in-flight command and tear the runtime down (upstream semantics)."""
        process = self._active_process
        if process is not None and process.returncode is None:
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()
        await self.stop()  # type: ignore[attr-defined]


try:  # bind to the real upstream base when ProRL-Agent-Server is installed
    from polar.runtime.base import BaseRuntime as _BaseRuntime  # type: ignore
    from polar.runtime.models import ExecResult as _UpstreamExecResult  # type: ignore
except ImportError:  # clean Shinken checkout: duck-typed shim
    _BaseRuntime = _ShimBaseRuntime  # type: ignore[assignment,misc]
    _UpstreamExecResult = None


def _make_exec_result(stdout: str | None, stderr: str | None, return_code: int) -> Any:
    if _UpstreamExecResult is not None:
        return _UpstreamExecResult(stdout=stdout, stderr=stderr, return_code=return_code)
    return ExecResult(stdout=stdout, stderr=stderr, return_code=return_code)


class ShinkenRuntime(_BaseRuntime):  # type: ignore[valid-type,misc]
    """One Shinken provider-managed sandbox as a rollout server's per-session runtime.

    Recognized ``spec.kwargs`` (all optional):

    - ``provider`` (default ``"docker"``): Shinken provider-registry name.
    - ``provider_kwargs``: kwargs for the provider factory (e.g. ``image``,
      ``name_prefix`` for the Docker provider).
    - ``golden_snapshot``: a Shinken snapshot/checkpoint id; ``start()`` resumes from it
      instead of cold-booting (INIT-stage fork-from-golden).
    - ``screen_geometry`` (default ``"1280x800x24"``): sandbox display geometry.
    - ``push_session_dir_on_exec`` (default ``False``): push the host session dir into
      the guest before every ``exec`` to emulate the upstream bind-mount for evaluators
      that write host-side and read in-guest.

    ``spec.image`` / ``spec.cpus`` / ``spec.memory_mb`` map onto the Shinken
    ``SandboxSpec``.
    """

    def __init__(self, spec: Any, session_id: str, session_dir: Path) -> None:
        super().__init__(spec, session_id, session_dir)
        kwargs = dict(getattr(spec, "kwargs", None) or {})
        self._provider_name = str(kwargs.get("provider", "docker"))
        self._provider_kwargs = dict(kwargs.get("provider_kwargs") or {})
        golden = kwargs.get("golden_snapshot") or kwargs.get("golden_checkpoint")
        self._golden: str | None = str(golden) if golden else None
        self._screen_geometry = str(kwargs.get("screen_geometry", "1280x800x24"))
        self._push_session_dir = bool(kwargs.get("push_session_dir_on_exec", False))
        self._provider: Any = None
        self._handle: Any = None
        # Cached ACI session for the typed in-band `exec` verb (G1) — the preferred
        # channel; None until first use, or when the runtime predates the verb.
        self._aci_sess: Any = None

    # -- identity & capabilities ----------------------------------------------------

    @property
    def runtime_id(self) -> str:
        if self._handle is not None:
            return str(self._handle.sandbox_id)
        return f"shinken-{self.session_id.replace('/', '-')}"

    @property
    def sandbox_handle(self) -> Any:
        """The live Shinken ``SandboxHandle`` (None before ``start()``)."""
        return self._handle

    @property
    def supports_cpu_limits(self) -> bool:
        return True

    @property
    def supports_memory_limits(self) -> bool:
        return True

    # GPUs / storage limits / internet-off keep the honest False default: the upstream
    # factory rejects specs asking for them rather than this backend ignoring them.

    # -- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        """Provision the sandbox (cold boot, or resume-from-golden) and prepare the
        in-guest ``/polar/session`` directory tree."""
        if self._destroyed:
            raise RuntimeError("shinken runtime was already destroyed")
        await asyncio.to_thread(self._provision)
        # The sandbox guest user is non-root, so the /polar tree is created as root and
        # opened up (a+rwX) — the same move upstream's DockerRuntime makes for its
        # bind-mounted session dir.
        dirs = " ".join(
            (
                self.runtime_artifacts_dir,
                self.runtime_agent_log_dir,
                RUNTIME_EVAL_LOG_DIR,
                RUNTIME_EVAL_ARTIFACT_DIR,
            )
        )
        rc, _, err = await self._run_host(
            self._docker_bin(),
            "exec",
            "--user",
            "root",
            self._container(),
            "sh",
            "-c",
            f"mkdir -p {dirs} && chmod -R a+rwX /polar",
        )
        if rc != 0:
            await self.stop()
            raise RuntimeError(f"failed to prepare {self.runtime_session_dir} in sandbox: {err}")

    def _provision(self) -> None:
        self._provider = _providers.get(self._provider_name, **self._provider_kwargs)
        if self._golden:
            self._handle = self._provider.resume(self._golden)
            return
        spec = SandboxSpec(
            image=getattr(self.spec, "image", None) or None,
            cpus=getattr(self.spec, "cpus", None),
            memory=self._memory_arg(),
            screen_geometry=self._screen_geometry,
        )
        self._handle = self._provider.create(spec)

    def _memory_arg(self) -> str | None:
        memory_mb = getattr(self.spec, "memory_mb", None)
        return f"{int(memory_mb)}m" if memory_mb else None

    async def stop(self) -> None:
        """Destroy the sandbox. Idempotent (upstream calls it from several paths)."""
        if self._destroyed:
            return
        self._destroyed = True
        if self._aci_sess is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._aci_sess.close)
            self._aci_sess = None
        if self._provider is not None and self._handle is not None:
            await asyncio.to_thread(self._provider.destroy, self._handle)

    def connect_sandbox(self) -> Any:
        """Open the host-side Shinken ACI session for this sandbox (blocking call).

        Returns the provider-connected :class:`shinken.client.Sandbox` — screenshot /
        actions / ``put_file`` / ``get_file`` / checkpoint / fork — for use by custom
        harness ``setup``/``postprocess`` hooks or EVAL-stage scorers. Caller closes it.
        """
        if self._provider is None or self._handle is None:
            raise RuntimeError("runtime not started")
        return self._provider.connect(self._handle)

    # -- exec ------------------------------------------------------------------------

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> Any:
        """Run one command in the sandbox (``bash -lc``): PREFERRING the typed
        in-band ACI ``exec`` verb (G1 — substrate-agnostic, no host docker CLI),
        falling back to ``docker exec`` against a pre-exec runtime."""
        if self._push_session_dir:
            await self._push_session_dir_to_guest()
        workdir = cwd or getattr(self.spec, "workdir", None) or self.runtime_session_dir
        merged_env = dict(env or {})
        merged_env.setdefault("SHINKEND_ADDR", GUEST_ACI_ADDR)
        sess = await self._aci_exec_session()
        if sess is not None:
            r = await asyncio.to_thread(
                sess.exec,
                ["bash", "-lc", command],
                cwd=str(workdir),
                env=merged_env,
                timeout=timeout_sec,
            )
            if r.get("timed_out"):
                # upstream timeout convention: rc -1, no output (gateway maps to "timeout")
                return _make_exec_result(None, None, -1)
            rc = r.get("exit_code")
            if rc is None:  # killed by a signal — report like a shell would
                rc = 128 + int(r.get("signal") or 1)
            return _make_exec_result(r.get("stdout"), r.get("stderr"), rc)
        cid = self._container()
        argv = [self._docker_bin(), "exec", "-w", str(workdir)]
        for key, value in merged_env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [cid, "bash", "-lc", command]
        rc, out, err = await self._run_host(*argv, timeout=timeout_sec)
        return _make_exec_result(out, err, rc)

    async def _aci_exec_session(self) -> Any:
        """The cached ACI session for in-band exec, or None when unavailable (no
        provider yet, connect failed, or a pre-exec runtime that doesn't advertise
        the verb — those fall back to the ``docker exec`` channel)."""
        if self._aci_sess is not None:
            return self._aci_sess
        if self._provider is None or self._handle is None:
            return None
        try:
            sess = await asyncio.to_thread(self._provider.connect, self._handle)
        except Exception:
            return None
        # Duck-typed probe: anything without a verbs-advertising session shape (or a
        # runtime that predates the verb) falls back to the docker exec channel.
        verbs = getattr(getattr(sess, "capabilities", None), "verbs", None) or []
        if "exec" not in verbs:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(sess.close)
            return None
        self._aci_sess = sess
        return sess

    # -- file transfer (guest paths validated: absolute, no `..`) ---------------------

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        cid = self._container()
        _validate_guest_path(remote_path)
        parent = str(PurePosixPath(remote_path).parent)
        await self._run_host(self._docker_bin(), "exec", cid, "mkdir", "-p", parent)
        rc, _, err = await self._run_host(
            self._docker_bin(), "cp", str(local_path), f"{cid}:{remote_path}"
        )
        if rc != 0:
            raise RuntimeError(f"upload_file to {remote_path} failed ({rc}): {err}")
        await self._chmod_guest(remote_path, recursive=False)

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        cid = self._container()
        _validate_guest_path(remote_path)
        await self._run_host(self._docker_bin(), "exec", cid, "mkdir", "-p", remote_path)
        rc, _, err = await self._run_host(
            self._docker_bin(), "cp", f"{local_path}/.", f"{cid}:{remote_path}"
        )
        if rc != 0:
            raise RuntimeError(f"upload_dir to {remote_path} failed ({rc}): {err}")
        await self._chmod_guest(remote_path, recursive=True)

    async def download_file(self, remote_path: str, local_path: str) -> None:
        cid = self._container()
        _validate_guest_path(remote_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = await self._run_host(
            self._docker_bin(), "cp", f"{cid}:{remote_path}", str(local_path)
        )
        if rc != 0:
            raise RuntimeError(f"download_file {remote_path} failed ({rc}): {err}")

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        cid = self._container()
        _validate_guest_path(remote_path)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        rc, _, err = await self._run_host(
            self._docker_bin(), "cp", f"{cid}:{remote_path}", str(local_path)
        )
        if rc != 0:
            raise RuntimeError(f"download_dir {remote_path} failed ({rc}): {err}")

    # -- plumbing ---------------------------------------------------------------------

    def _container(self) -> str:
        if self._handle is None:
            raise RuntimeError("runtime not started")
        cid = self._handle.metadata.get("container_id") or self._handle.sandbox_id
        if not cid:
            raise RuntimeError(
                "provider handle exposes no container id; ShinkenRuntime exec/copy need "
                "a container-substrate provider (the in-tree 'docker' provider)"
            )
        return str(cid)

    def _docker_bin(self) -> str:
        return str(getattr(self._provider, "docker_bin", "docker"))

    async def _chmod_guest(self, remote_path: str, *, recursive: bool) -> None:
        """``docker cp`` lands files root-owned; open them up for the non-root guest
        user (mirrors upstream DockerRuntime's post-upload chmod)."""
        argv = [self._docker_bin(), "exec", "--user", "root", self._container(), "chmod"]
        if recursive:
            argv.append("-R")
        argv += ["a+rwX", remote_path]
        rc, _, err = await self._run_host(*argv)
        if rc != 0:
            raise RuntimeError(f"chmod of uploaded {remote_path} failed ({rc}): {err}")

    async def _push_session_dir_to_guest(self) -> None:
        """One-way host -> guest sync of the session dir (bind-mount emulation)."""
        cid = self._container()
        rc, _, err = await self._run_host(
            self._docker_bin(), "cp", f"{self.session_dir}/.", f"{cid}:{self.runtime_session_dir}"
        )
        if rc != 0:
            raise RuntimeError(f"session-dir push to guest failed ({rc}): {err}")
        await self._chmod_guest(self.runtime_session_dir, recursive=True)

    async def _run_host(
        self, *argv: str, timeout: float | None = None
    ) -> tuple[int, str | None, str | None]:
        """Run a host subprocess, capturing output. Timeout -> ``(-1, None, None)``,
        matching upstream semantics (the gateway maps ``-1`` to ``"timeout"``)."""
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._active_process = process
        try:
            if timeout is None:
                out, err = await process.communicate()
            else:
                try:
                    out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    process.kill()
                    with contextlib.suppress(ProcessLookupError):
                        await process.wait()
                    return -1, None, None
        finally:
            self._active_process = None
        rc = process.returncode or 0
        return (
            rc,
            out.decode(errors="replace") if out else None,
            err.decode(errors="replace") if err else None,
        )
