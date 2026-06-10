"""ProRL-Agent-Server runtime plugin: fixture tests (no external package, no Docker).

Exercises the published rollout-server runtime contract (constructor shape, lifecycle,
``exec`` semantics including timeout -> ``return_code == -1``, file-transfer verbs,
capability honesty) against fakes, plus one env-gated live Docker roundtrip.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from shinken.artifacts import FileScopeError
from shinken.integrations import prorl_agent_server as mod
from shinken.integrations.prorl_agent_server import (
    GUEST_ACI_ADDR,
    RUNTIME_SESSION_DIR,
    ExecResult,
    ShinkenRuntime,
)
from shinken.providers import SandboxSpec
from shinken.providers.base import SandboxHandle


def run(coro):
    return asyncio.run(coro)


@dataclass
class SpecStub:
    """Duck-typed stand-in for the upstream RuntimeSpec (same attribute names)."""

    image: str = "shinken/sandbox-linux"
    kwargs: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    workdir: str | None = None
    cpus: float | None = None
    memory_mb: int | None = None


class FakeProvider:
    """Records lifecycle calls; returns a container-shaped handle."""

    docker_bin = "fakedocker"

    def __init__(self) -> None:
        self.created: list[SandboxSpec] = []
        self.resumed: list[str] = []
        self.destroyed: list[Any] = []
        self.connected: list[Any] = []

    def _handle(self, sandbox_id: str) -> SandboxHandle:
        return SandboxHandle(
            provider="fake",
            sandbox_id=sandbox_id,
            addr="127.0.0.1:1",
            metadata={"container_id": f"cid-{sandbox_id}"},
        )

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created.append(spec)
        return self._handle("created")

    def resume(self, ref: str) -> SandboxHandle:
        self.resumed.append(ref)
        return self._handle("resumed")

    def destroy(self, handle: Any) -> None:
        self.destroyed.append(handle)

    def connect(self, handle: Any) -> str:
        self.connected.append(handle)
        return "sandbox-session"


def make_runtime(tmp_path: Path, spec: SpecStub | None = None, **kwargs) -> ShinkenRuntime:
    spec = spec or SpecStub(kwargs=kwargs)
    return ShinkenRuntime(spec, "sess-01", tmp_path)


def wire_fake(monkeypatch, runtime: ShinkenRuntime) -> tuple[FakeProvider, list[tuple]]:
    """Route provider resolution to a FakeProvider and capture _run_host argv."""
    provider = FakeProvider()
    monkeypatch.setattr(mod._providers, "get", lambda name, **kw: provider)
    calls: list[tuple] = []

    async def fake_run_host(*argv: str, timeout: float | None = None):
        calls.append((argv, timeout))
        return 0, "", None

    monkeypatch.setattr(runtime, "_run_host", fake_run_host)
    return provider, calls


# --- contract surface ---------------------------------------------------------------


def test_imports_without_external_package(tmp_path):
    # The whole point of the duck-typed shim: a clean checkout has no `polar`.
    rt = make_runtime(tmp_path)
    for member in (
        "start",
        "stop",
        "cancel",
        "exec",
        "upload_file",
        "upload_dir",
        "download_file",
        "download_dir",
    ):
        assert callable(getattr(rt, member)), member
    assert rt.runtime_session_dir == RUNTIME_SESSION_DIR == "/polar/session"
    assert rt.runtime_id == "shinken-sess-01"
    assert rt.session_dir == tmp_path
    assert rt.artifacts_dir == tmp_path / "artifacts"


def test_capabilities_are_honest(tmp_path):
    rt = make_runtime(tmp_path)
    assert rt.supports_cpu_limits is True
    assert rt.supports_memory_limits is True
    # not advertised -> the upstream factory rejects specs needing them.
    assert rt.supports_gpus is False
    assert rt.supports_storage_limits is False
    assert rt.can_disable_internet is False


# --- lifecycle ------------------------------------------------------------------------


def test_start_maps_spec_onto_sandbox_spec(monkeypatch, tmp_path):
    spec = SpecStub(image="shinken/custom", cpus=2.0, memory_mb=512)
    rt = ShinkenRuntime(spec, "sess-01", tmp_path)
    provider, calls = wire_fake(monkeypatch, rt)

    run(rt.start())

    assert provider.resumed == []
    (sspec,) = provider.created
    assert sspec.image == "shinken/custom"
    assert sspec.cpus == 2.0
    assert sspec.memory == "512m"
    assert sspec.screen_geometry == "1280x800x24"
    assert rt.runtime_id == "created"
    # start() prepares the in-guest session tree as root (the guest user is non-root)
    # and opens it up for the guest user.
    (argv, _timeout) = calls[-1]
    assert argv[:5] == ("fakedocker", "exec", "--user", "root", "cid-created")
    assert "mkdir -p" in argv[-1]
    assert "/polar/session/artifacts" in argv[-1]
    assert "chmod -R a+rwX /polar" in argv[-1]


def test_start_from_golden_snapshot_resumes(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path, golden_snapshot="shinken-snap:golden1")
    provider, _calls = wire_fake(monkeypatch, rt)

    run(rt.start())

    assert provider.created == []
    assert provider.resumed == ["shinken-snap:golden1"]
    assert rt.runtime_id == "resumed"


def test_stop_is_idempotent_and_destroys_once(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    provider, _calls = wire_fake(monkeypatch, rt)
    run(rt.start())

    run(rt.stop())
    run(rt.stop())

    assert len(provider.destroyed) == 1
    with pytest.raises(RuntimeError):
        run(rt.start())  # destroyed runtimes do not restart


def test_cancel_without_active_process_stops(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    provider, _calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    run(rt.cancel())
    assert len(provider.destroyed) == 1


def test_connect_sandbox_uses_provider_session(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    provider, _calls = wire_fake(monkeypatch, rt)
    with pytest.raises(RuntimeError):
        rt.connect_sandbox()
    run(rt.start())
    assert rt.connect_sandbox() == "sandbox-session"
    assert provider.connected == [rt.sandbox_handle]


# --- exec ------------------------------------------------------------------------------


def test_exec_argv_assembly(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    provider, calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    calls.clear()

    result = run(rt.exec("echo hi", env={"TASK_ID": "t1"}, timeout_sec=7.5))

    (argv, timeout) = calls[0]
    assert timeout == 7.5
    assert argv[:4] == ("fakedocker", "exec", "-w", "/polar/session")  # default cwd
    flat = " ".join(argv)
    assert f"-e SHINKEND_ADDR={GUEST_ACI_ADDR}" in flat  # in-guest ACI coordinates
    assert "-e TASK_ID=t1" in flat
    assert "SHINKEND_TOKEN" not in flat  # token never appears on a host command line
    assert argv[-4:] == ("cid-created", "bash", "-lc", "echo hi")
    assert result.return_code == 0


def test_exec_cwd_priority(monkeypatch, tmp_path):
    spec = SpecStub(workdir="/polar/session/workspace")
    rt = ShinkenRuntime(spec, "sess-01", tmp_path)
    _provider, calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    calls.clear()

    run(rt.exec("true"))
    assert calls[0][0][3] == "/polar/session/workspace"  # spec.workdir
    run(rt.exec("true", cwd="/tmp"))
    assert calls[1][0][3] == "/tmp"  # explicit cwd wins


def test_exec_result_shape_and_timeout_semantics(tmp_path):
    rt = make_runtime(tmp_path)
    rc, out, err = run(rt._run_host("sh", "-c", "echo out; echo err >&2"))
    assert (rc, out, err) == (0, "out\n", "err\n")
    rc, out, err = run(rt._run_host("sleep", "5", timeout=0.2))
    assert (rc, out, err) == (-1, None, None)  # the value the gateway maps to "timeout"
    result = ExecResult(stdout="x", stderr=None, return_code=3)
    assert (result.stdout, result.stderr, result.return_code) == ("x", None, 3)


def test_exec_before_start_raises(tmp_path):
    rt = make_runtime(tmp_path)
    with pytest.raises(RuntimeError, match="not started"):
        run(rt.exec("true"))


def test_push_session_dir_on_exec_opt_in(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path, push_session_dir_on_exec=True)
    _provider, calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    calls.clear()

    run(rt.exec("true"))

    push_argv = calls[0][0]
    assert push_argv[1] == "cp"
    assert push_argv[2] == f"{tmp_path}/."
    assert push_argv[3] == "cid-created:/polar/session"
    assert calls[1][0][-3:] == ("-R", "a+rwX", "/polar/session")  # opened for the guest user
    assert calls[2][0][-1] == "true"  # then the command itself


# --- file transfer ----------------------------------------------------------------------


def test_upload_file_makes_parent_then_copies(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    _provider, calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    calls.clear()

    run(rt.upload_file("/host/task.json", "/polar/session/task.json"))

    mkdir_argv, cp_argv, chmod_argv = calls[0][0], calls[1][0], calls[2][0]
    assert mkdir_argv[1:] == ("exec", "cid-created", "mkdir", "-p", "/polar/session")
    assert cp_argv[1:] == ("cp", "/host/task.json", "cid-created:/polar/session/task.json")
    # docker cp lands root-owned files; they are opened up for the non-root guest user.
    assert chmod_argv[1:] == (
        "exec",
        "--user",
        "root",
        "cid-created",
        "chmod",
        "a+rwX",
        "/polar/session/task.json",
    )


def test_dir_transfer_argv(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    _provider, calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    calls.clear()

    run(rt.upload_dir("/host/skel", "/polar/session/workspace"))
    run(rt.download_dir("/polar/session/artifacts", str(tmp_path / "artifacts")))
    run(rt.download_file("/polar/session/out.txt", str(tmp_path / "out.txt")))

    assert calls[1][0][1:] == ("cp", "/host/skel/.", "cid-created:/polar/session/workspace")
    assert calls[2][0][-3:] == ("-R", "a+rwX", "/polar/session/workspace")  # recursive chmod
    assert calls[3][0][1:] == (
        "cp",
        "cid-created:/polar/session/artifacts",
        str(tmp_path / "artifacts"),
    )
    assert calls[4][0][1:] == (
        "cp",
        "cid-created:/polar/session/out.txt",
        str(tmp_path / "out.txt"),
    )


def test_guest_paths_are_validated(monkeypatch, tmp_path):
    rt = make_runtime(tmp_path)
    _provider, _calls = wire_fake(monkeypatch, rt)
    run(rt.start())
    with pytest.raises(FileScopeError):
        run(rt.upload_file("/host/x", "relative/path"))
    with pytest.raises(FileScopeError):
        run(rt.download_file("/polar/../etc/shadow", str(tmp_path / "x")))


# --- gateway-shaped session fixture ------------------------------------------------------


def test_rollout_session_lifecycle_order(monkeypatch, tmp_path):
    """The INIT -> RUN -> collect -> teardown sequence the rollout gateway drives."""
    rt = make_runtime(tmp_path)
    provider, calls = wire_fake(monkeypatch, rt)

    async def session():
        await rt.start()  # INIT: provision + session tree
        await rt.upload_file("/host/task.json", "/polar/session/task.json")  # prepare
        await rt.exec("run-agent --task /polar/session/task.json")  # RUN
        await rt.download_dir("/polar/session/artifacts", str(tmp_path / "artifacts"))
        await rt.stop()  # teardown

    run(session())

    kinds = [argv[1] for argv, _t in calls]
    # prepare tree, mkdir parent, put, chmod, run, get
    assert kinds == ["exec", "exec", "cp", "exec", "exec", "cp"]
    assert len(provider.created) == 1
    assert len(provider.destroyed) == 1
    assert rt._destroyed is True


# --- live (env-gated) ---------------------------------------------------------------------


needs_docker_live = pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCKER_LIVE") != "1",
    reason="live Docker roundtrip; set SHINKEN_DOCKER_LIVE=1 with shinken/sandbox-linux built",
)


@needs_docker_live
def test_live_docker_roundtrip(tmp_path):
    rt = make_runtime(tmp_path)

    async def roundtrip():
        await rt.start()
        try:
            result = await rt.exec("echo -n alive")
            assert (result.return_code, result.stdout) == (0, "alive")
            payload = tmp_path / "payload.txt"
            payload.write_text("roundtrip")
            await rt.upload_file(str(payload), "/polar/session/payload.txt")
            result = await rt.exec("cat /polar/session/payload.txt")
            assert result.stdout == "roundtrip"
            fetched = tmp_path / "fetched.txt"
            await rt.download_file("/polar/session/payload.txt", str(fetched))
            assert fetched.read_text() == "roundtrip"
            assert (await rt.exec("sleep 5", timeout_sec=0.5)).return_code == -1
        finally:
            await rt.stop()

    run(roundtrip())
