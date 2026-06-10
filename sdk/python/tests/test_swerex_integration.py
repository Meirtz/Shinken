"""shinken.integrations.swerex — the uni-agent/SWE-ReX deployment seam.

Unit tests drive the integration with a *vendored minimal protocol fixture*: tiny
attribute-bag classes mirroring the SWE-ReX request/action shapes uni-agent sends
(written fresh from the protocol surface; no SWE-ReX/uni-agent code is imported or
copied). The sandbox side is the in-process mock shinkend from conftest; the substrate
exec channel is a host-bash executor so the session env/cwd-persistence emulation is
exercised for real. The final test is a Docker-gated live end-to-end run
(``SHINKEN_LIVE_DOCKER=1`` with the ``shinken/sandbox-linux`` image built).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shinken.artifacts import ArtifactRef, sha256_file
from shinken.integrations.swerex import (
    CommandTimeoutError,
    DeploymentNotStartedError,
    NonZeroExitCodeError,
    SessionNotInitializedError,
    ShinkenDeployment,
    ShinkenDeploymentConfig,
)
from shinken.providers.base import ProviderCapabilities, SandboxHandle, SandboxProvider

# --- vendored protocol-shape fixture (what uni-agent sends, attribute-for-attribute) ---


class CreateBashSessionRequest:
    type = "bash"

    def __init__(self, session="default", startup_source=(), startup_timeout=60.0):
        self.session = session
        self.startup_source = list(startup_source)
        self.startup_timeout = startup_timeout


class BashAction:
    type = "bash"

    def __init__(self, command, timeout=None, check="ignore", session="default"):
        self.command = command
        self.timeout = timeout
        self.check = check
        self.session = session


class BashInterruptAction:
    type = "bash_interrupt"

    def __init__(self, timeout=0.2, session="default"):
        self.timeout = timeout
        self.session = session


class Command:
    def __init__(self, command, timeout=None, shell=False, check=False):
        self.command = command
        self.timeout = timeout
        self.shell = shell
        self.check = check


class ReadFileRequest:
    def __init__(self, path, encoding=None, errors=None):
        self.path = path
        self.encoding = encoding
        self.errors = errors


class WriteFileRequest:
    def __init__(self, content, path):
        self.content = content
        self.path = path


class UploadRequest:
    def __init__(self, source_path, target_path):
        self.source_path = source_path
        self.target_path = target_path


class CloseSessionRequest:
    def __init__(self, session="default"):
        self.session = session


# --- test doubles: provider over the mock shinkend + host-side exec/transfer channels ---


class HostBashExecutor:
    """GuestExecutor that runs on the host's bash — lets the session-state emulation
    (env/cwd persistence, exit codes, timeouts) be exercised without a container."""

    def run_argv(self, argv, *, timeout=None):
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    def run_script(self, script, *, timeout=None):
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr


class MemoryGuestTransport:
    """Guest-boundary file transport double: absolute guest paths mapped under a host
    root (the contract DockerGuestTransport implements with `docker cp`)."""

    def __init__(self, root):
        self.root = Path(root)

    def _resolve(self, guest_path: str) -> Path:
        assert guest_path.startswith("/"), guest_path
        return self.root / guest_path.lstrip("/")

    def put(self, local_path, guest_path, scope="session"):
        dst = self._resolve(guest_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dst)
        return ArtifactRef(guest_path, sha256_file(dst), dst.stat().st_size, scope, "put")

    def get(self, guest_path, local_path, *, expect_sha256=None, scope="session"):
        src = self._resolve(guest_path)
        shutil.copyfile(src, local_path)
        return ArtifactRef(guest_path, sha256_file(src), src.stat().st_size, scope, "get")


class FakeProvider(SandboxProvider):
    """Provider double over the mock shinkend: records lifecycle calls in order."""

    capabilities = ProviderCapabilities(
        name="fake",
        supports_lifecycle=True,
        supports_gui=True,
        supports_checkpoint=True,
        supports_resume=True,
    )

    def __init__(self, addr: str, transport_root) -> None:
        self.addr = addr
        self.transport_root = transport_root
        self.calls: list = []

    def create(self, spec=None):
        self.calls.append("create")
        return SandboxHandle(provider="fake", sandbox_id="sb-1", addr=self.addr)

    def connect(self, handle):
        self.calls.append("connect")
        env = super().connect(handle)
        env._set_guest_transport(MemoryGuestTransport(self.transport_root))
        return env

    def resume(self, handle_or_checkpoint):
        self.calls.append(("resume", handle_or_checkpoint))
        return SandboxHandle(provider="fake", sandbox_id="sb-fork", addr=self.addr)

    def destroy(self, handle):
        self.calls.append("destroy")


def _deployment(mock_shinkend, tmp_path, **kwargs):
    provider = FakeProvider(mock_shinkend, tmp_path / "guest-fs")
    kwargs.setdefault("executor", HostBashExecutor())
    kwargs.setdefault("retry_backoff", 0.0)
    return provider, ShinkenDeployment(provider, **kwargs)


# --- the out-of-tree rule ---


def test_module_imports_without_swerex_or_uniagent():
    # The integration must be importable (and tested here) with neither package
    # installed, and must not import them as a side effect.
    assert "swerex" not in sys.modules
    assert "uni_agent" not in sys.modules


# --- the protocol drive, in uni-agent's order ---


def test_uniagent_call_sequence_maps_to_shinken(mock_shinkend, tmp_path):
    provider, dep = _deployment(mock_shinkend, tmp_path)

    async def drive():
        # 1. create/start → provider.create + provider.connect + default bash session
        await dep.start(max_retries=5)
        assert provider.calls == ["create", "connect"]
        rt = dep.runtime
        assert "default" in rt._sessions

        # 2. execute command in the persistent session: env + cwd survive across calls
        r = await rt.run_in_session(BashAction(command=f"export FOO=bar && cd {tmp_path}"))
        assert r.exit_code == 0
        r = await rt.run_in_session(BashAction(command='echo "$FOO"; pwd'))
        assert r.exit_code == 0
        assert "bar" in r.output and str(tmp_path) in r.output

        # exit codes are real, and check="raise" enforces them
        r = await rt.run_in_session(BashAction(command="false"))
        assert r.exit_code == 1
        with pytest.raises(NonZeroExitCodeError):
            await rt.run_in_session(BashAction(command="false", check="raise"))

        # 3. one-shot execute (argv, no shell) → substrate exec
        out_dir = tmp_path / "made-by-execute"
        cr = await rt.execute(Command(command=["mkdir", "-p", str(out_dir)]))
        assert cr.exit_code == 0 and out_dir.is_dir()
        cr = await rt.execute(Command(command="echo via-shell", shell=True))
        assert cr.exit_code == 0 and "via-shell" in cr.stdout

        # 4. write/read file → put_file/get_file across the guest transport
        await rt.write_file(WriteFileRequest(content="hello from shinken", path="/work/a.txt"))
        rf = await rt.read_file(ReadFileRequest(path="/work/a.txt"))
        assert rf.content == "hello from shinken"

        # upload (file + directory walk) → put_file per file
        src_dir = tmp_path / "skill"
        (src_dir / "sub").mkdir(parents=True)
        (src_dir / "run.sh").write_text("echo hi\n")
        (src_dir / "sub" / "data.txt").write_text("payload")
        await rt.upload(UploadRequest(source_path=str(src_dir), target_path="/opt/skills/skill"))
        root = Path(provider.transport_root)
        assert (root / "opt/skills/skill/run.sh").read_text() == "echo hi\n"
        assert (root / "opt/skills/skill/sub/data.txt").read_text() == "payload"

        # 5. liveness + pixel observation stay available beside the swerex shim
        alive = await rt.is_alive()
        assert bool(alive)
        shot = dep.sandbox.screenshot()
        assert shot["png"].startswith(b"\x89PNG")

        # interrupt is a successful no-op (commands are bounded by their own timeout)
        r = await rt.run_in_session(BashInterruptAction())
        assert r.exit_code == 0

        # 6. stop → session closed, sandbox destroyed (created here, so owned here)
        await dep.stop()
        assert provider.calls == ["create", "connect", "destroy"]
        with pytest.raises(DeploymentNotStartedError):
            _ = dep.runtime

    asyncio.run(drive())


def test_command_timeout_maps_to_protocol_error(mock_shinkend, tmp_path):
    _, dep = _deployment(mock_shinkend, tmp_path)

    async def drive():
        await dep.start()
        with pytest.raises(CommandTimeoutError):
            await dep.runtime.run_in_session(BashAction(command="sleep 5", timeout=0.2))
        await dep.stop()

    asyncio.run(drive())


def test_unknown_session_and_explicit_close(mock_shinkend, tmp_path):
    _, dep = _deployment(mock_shinkend, tmp_path)

    async def drive():
        await dep.start()
        rt = dep.runtime
        with pytest.raises(SessionNotInitializedError):
            await rt.run_in_session(BashAction(command="true", session="nope"))
        await rt.create_session(CreateBashSessionRequest(session="extra"))
        await rt.close_session(CloseSessionRequest(session="extra"))
        with pytest.raises(SessionNotInitializedError):
            await rt.run_in_session(BashAction(command="true", session="extra"))
        await dep.stop()

    asyncio.run(drive())


def test_startup_source_is_sourced_into_the_session(mock_shinkend, tmp_path):
    rc_file = tmp_path / "rc.sh"
    rc_file.write_text("export FROM_RC=loaded\n")
    _, dep = _deployment(mock_shinkend, tmp_path, startup_source=[str(rc_file)])

    async def drive():
        await dep.start()
        r = await dep.runtime.run_in_session(BashAction(command='echo "$FROM_RC"'))
        assert "loaded" in r.output
        await dep.stop()

    asyncio.run(drive())


def test_transfers_invoke_executor_fixup_hook(mock_shinkend, tmp_path):
    """write_file/upload call the executor's optional post-transfer fixup (Docker uses
    it to re-own `docker cp`'d files to the guest user)."""

    class FixupExecutor(HostBashExecutor):
        def __init__(self):
            self.fixed: list[str] = []

        def fixup_path(self, path, *, timeout=None):
            self.fixed.append(path)

    executor = FixupExecutor()
    _, dep = _deployment(mock_shinkend, tmp_path, executor=executor)

    async def drive():
        await dep.start()
        await dep.runtime.write_file(WriteFileRequest(content="x", path="/work/w.txt"))
        src = tmp_path / "one.txt"
        src.write_text("1")
        await dep.runtime.upload(UploadRequest(source_path=str(src), target_path="/work/one.txt"))
        await dep.stop()

    asyncio.run(drive())
    assert executor.fixed == ["/work/w.txt", "/work/one.txt"]


# --- fork-native + attach lifecycle modes ---


def test_checkpoint_start_resumes_from_golden_not_cold_boot(mock_shinkend, tmp_path):
    provider, dep = _deployment(mock_shinkend, tmp_path, checkpoint="ckpt-golden")

    async def drive():
        await dep.start()
        assert provider.calls == [("resume", "ckpt-golden"), "connect"]
        await dep.stop()
        assert provider.calls[-1] == "destroy"

    asyncio.run(drive())


def test_attached_handle_is_never_destroyed(mock_shinkend, tmp_path):
    provider = FakeProvider(mock_shinkend, tmp_path / "guest-fs")
    handle = SandboxHandle(provider="fake", sandbox_id="external", addr=mock_shinkend)
    dep = ShinkenDeployment(provider, handle=handle, executor=HostBashExecutor())

    async def drive():
        await dep.start()
        assert provider.calls == ["connect"]  # no create
        await dep.stop()
        assert "destroy" not in provider.calls

    asyncio.run(drive())


def test_runtime_access_before_start_raises():
    dep = ShinkenDeployment(provider=object())
    with pytest.raises(DeploymentNotStartedError):
        _ = dep.runtime

    async def drive():
        with pytest.raises(DeploymentNotStartedError):
            await dep.is_alive()

    asyncio.run(drive())


def test_config_shape_builds_a_docker_backed_deployment():
    cfg = ShinkenDeploymentConfig(image="shinken/sandbox-linux")
    assert cfg.type == "shinken"  # the uni-agent DeployConfig discriminator convention
    dep = cfg.get_deployment("run-1")
    assert isinstance(dep, ShinkenDeployment)
    assert dep.provider.capabilities.name == "docker-local"
    assert dep.spec.image == "shinken/sandbox-linux"
    assert dep.run_id == "run-1"


# --- live end-to-end over Docker (opt-in, like the other live smokes) ---


@pytest.mark.skipif(
    os.environ.get("SHINKEN_LIVE_DOCKER") != "1",
    reason="live Docker test: set SHINKEN_LIVE_DOCKER=1 with the sandbox image built",
)
def test_live_docker_uniagent_sequence():
    from shinken.providers import DockerLocalProvider

    image = os.environ.get("SHINKEN_IMAGE", "shinken/sandbox-linux")
    dep = ShinkenDeployment(DockerLocalProvider(image=image))

    async def drive():
        await dep.start(max_retries=2)
        try:
            rt = dep.runtime
            # persistent bash session over docker exec: env + cwd survive
            r = await rt.run_in_session(BashAction(command="export MARKER=live && cd /tmp"))
            assert r.exit_code == 0
            r = await rt.run_in_session(BashAction(command='echo "$MARKER"; pwd'))
            assert "live" in r.output and "/tmp" in r.output
            # one-shot execute + file round-trip across the real guest boundary
            cr = await rt.execute(Command(command=["mkdir", "-p", "/tmp/uniagent"]))
            assert cr.exit_code == 0
            await rt.write_file(WriteFileRequest(content="ping", path="/tmp/uniagent/f.txt"))
            rf = await rt.read_file(ReadFileRequest(path="/tmp/uniagent/f.txt"))
            assert rf.content == "ping"
            cr = await rt.execute(Command(command=["cat", "/tmp/uniagent/f.txt"]))
            assert cr.stdout.strip() == "ping"
            # GUI observation still first-class beside the swerex surface
            shot = dep.sandbox.screenshot()
            assert len(shot["png"]) > 1000
            assert bool(await rt.is_alive())
        finally:
            await dep.stop()

    asyncio.run(drive())
