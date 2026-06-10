"""Agentix interop adapter (shinken.integrations.agentix).

Fixture-tested against a frozen mirror of the surveyed Agentix ``SandboxProvider``
Protocol (Agentix-Project/Agentix @ 41d2ac9c — agentix/provider/base.py: async
create/delete/get + the session() async context manager + the (sandbox_id, runtime_url,
status) handle shape), with a fake Shinken provider + the mock runtime; one short
Docker-gated live test (SHINKEN_DOCKER_TESTS=1).
"""

from __future__ import annotations

import asyncio
import collections
import inspect
import os
from types import SimpleNamespace

import pytest

import shinken
from shinken.errors import SandboxDied
from shinken.integrations import agentix as ax
from shinken.providers.base import SandboxHandle

# Frozen mirror of the surveyed Protocol surface (agentix/provider/base.py @ 41d2ac9c):
# the three async lifecycle methods plus the session() async context manager helper.
AGENTIX_PROVIDER_SURFACE = ("create", "delete", "get", "session")
AGENTIX_SANDBOX_FIELDS = ("sandbox_id", "runtime_url", "status", "call_deadline")
AGENTIX_INFO_FIELDS = ("sandbox_id", "runtime_url", "status")


def _run(coro):
    return asyncio.run(coro)


class _FakeShinkenProvider:
    """Shinken-provider-shaped fake: records lifecycle calls; connect() opens a real SDK
    session against the mock shinkend."""

    def __init__(self, addr: str | None = None):
        self.addr = addr
        self.calls: collections.Counter = collections.Counter()
        self.specs: list = []
        self.alive = True

    def create(self, spec=None):
        self.calls["create"] += 1
        self.specs.append(spec)
        return SandboxHandle(
            provider="fake", sandbox_id=f"sb-{self.calls['create']}", addr="127.0.0.1:1"
        )

    def resume(self, ckpt):
        self.calls["resume"] += 1
        self.calls[f"resume:{ckpt}"] += 1
        return SandboxHandle(
            provider="fake", sandbox_id=f"fork-{self.calls['resume']}", addr="127.0.0.1:2"
        )

    def connect(self, handle):
        self.calls["connect"] += 1
        assert self.addr is not None, "test wired no mock runtime"
        return shinken.connect(self.addr)

    def destroy(self, handle):
        self.calls["destroy"] += 1

    def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None):
        self.calls["checkpoint"] += 1
        return "ckpt-7"

    def check_alive(self, handle):
        if not self.alive:
            raise SandboxDied("gone", exit_code=137)


# ------------------------------------------------------------------------ protocol shape


def test_provider_satisfies_the_agentix_protocol_shape():
    for name in AGENTIX_PROVIDER_SURFACE:
        assert callable(getattr(ax.ShinkenAgentixProvider, name)), name
    # their create/delete/get are coroutine functions; session is an async context manager
    for name in ("create", "delete", "get"):
        assert inspect.iscoroutinefunction(getattr(ax.ShinkenAgentixProvider, name)), name
    prov = ax.ShinkenAgentixProvider(_FakeShinkenProvider())
    assert hasattr(prov.session(None), "__aenter__")


def test_handle_and_info_carry_their_field_shapes():
    sandbox = ax.ShinkenAgentixSandbox(sandbox_id="s", runtime_url="ws://x", status="running")
    for f in AGENTIX_SANDBOX_FIELDS:
        assert hasattr(sandbox, f), f
    info = ax.ShinkenSandboxInfo(sandbox_id="s", runtime_url="ws://x")
    for f in AGENTIX_INFO_FIELDS:
        assert hasattr(info, f), f
    assert info.status == "running"


def test_isinstance_against_real_agentix_protocol_when_installed():
    base = pytest.importorskip("agentix.provider.base")
    assert isinstance(ax.ShinkenAgentixProvider(_FakeShinkenProvider()), base.SandboxProvider)


# ------------------------------------------------------------------------ config mapping


def test_create_maps_config_fields_onto_sandbox_spec():
    fake = _FakeShinkenProvider()
    config = SimpleNamespace(
        image="shinken/sandbox-linux",
        bundle="/cache/sha256-abc",  # their runtime overlay: accepted, recorded, not mounted
        platform="linux/amd64",
        env={"FOO": "bar"},
        resource=SimpleNamespace(cpu=2.0, memory="2g", gpu=None),
    )
    sandbox = _run(ax.ShinkenAgentixProvider(fake).create(config))
    spec = fake.specs[0]
    assert spec.image == "shinken/sandbox-linux"
    assert spec.cpus == 2.0 and spec.memory == "2g"
    assert spec.metadata["agentix_ignored"] == {
        "bundle": "/cache/sha256-abc",
        "platform": "linux/amd64",
    }
    assert spec.metadata["agentix_requested_env"] == {"FOO": "bar"}
    assert sandbox.sandbox_id == "sb-1" and sandbox.runtime_url == "ws://127.0.0.1:1"
    assert sandbox.status == "running"


def test_create_accepts_dict_config_and_none():
    fake = _FakeShinkenProvider()
    prov = ax.ShinkenAgentixProvider(fake)
    _run(prov.create({"image": "img", "resource": {"memory": 512, "cpu": 1}}))
    assert fake.specs[0].image == "img"
    assert fake.specs[0].memory == "512b"  # their int memory form is bytes
    _run(prov.create(None))
    assert fake.specs[1].image is None  # provider default image


def test_gpu_request_raises_instead_of_silently_dropping():
    prov = ax.ShinkenAgentixProvider(_FakeShinkenProvider())
    with pytest.raises(ax.AgentixInteropError, match="gpu"):
        _run(prov.create({"resource": {"gpu": 1}}))


# ------------------------------------------------------------------------ lifecycle


def test_get_reports_status_and_unknown_id_is_keyerror():
    fake = _FakeShinkenProvider()
    prov = ax.ShinkenAgentixProvider(fake)

    async def scenario():
        sandbox = await prov.create(None)
        info = await prov.get(sandbox.sandbox_id)
        assert info.status == "running" and info.sandbox_id == sandbox.sandbox_id
        fake.alive = False
        assert (await prov.get(sandbox.sandbox_id)).status == "exited(137)"
        with pytest.raises(KeyError, match="nope"):
            await prov.get("nope")

    _run(scenario())


def test_delete_is_tolerant_and_destroys_once():
    fake = _FakeShinkenProvider()
    prov = ax.ShinkenAgentixProvider(fake)

    async def scenario():
        sandbox = await prov.create(None)
        await prov.delete(sandbox.sandbox_id)
        await prov.delete(sandbox.sandbox_id)  # already gone: no raise (their rm -f semantics)
        await prov.delete("never-existed")

    _run(scenario())
    assert fake.calls["destroy"] == 1


def test_session_scopes_create_and_delete_even_on_error(mock_shinkend):
    fake = _FakeShinkenProvider(mock_shinkend)
    prov = ax.ShinkenAgentixProvider(fake)

    async def scenario():
        async with prov.session(None, call_deadline=30.0) as sandbox:
            assert sandbox.call_deadline == 30.0
        with pytest.raises(RuntimeError, match="boom"):
            async with prov.session(None) as sandbox:
                raise RuntimeError("boom")

    _run(scenario())
    assert fake.calls["create"] == 2 and fake.calls["destroy"] == 2


def test_golden_checkpoint_create_resumes_instead_of_cold_boot():
    fake = _FakeShinkenProvider()
    prov = ax.ShinkenAgentixProvider(fake, golden="ckpt-7")

    async def scenario():
        a = await prov.create(None)
        b = await prov.create(None)
        return a, b

    a, b = _run(scenario())
    assert fake.calls["create"] == 0  # never cold-boots
    assert fake.calls["resume:ckpt-7"] == 2  # every sandbox forks the one golden checkpoint
    assert a.sandbox_id != b.sandbox_id


def test_checkpoint_extra_returns_id_usable_as_golden():
    fake = _FakeShinkenProvider()
    prov = ax.ShinkenAgentixProvider(fake)

    async def scenario():
        sandbox = await prov.create(None)
        return await prov.checkpoint(sandbox.sandbox_id, name="golden")

    assert _run(scenario()) == "ckpt-7"
    assert fake.calls["checkpoint"] == 1


# ------------------------------------------------------------------------ the ACI handle


def test_sandbox_aci_is_a_live_typed_session(mock_shinkend):
    fake = _FakeShinkenProvider(mock_shinkend)
    prov = ax.ShinkenAgentixProvider(fake)

    async def scenario():
        async with prov.session(None) as sandbox:
            aci = sandbox.aci()
            assert aci is sandbox.aci()  # lazily connected, cached (their handle pattern)
            shot = await asyncio.to_thread(aci.screenshot)
            assert shot["bytes"][:4] == b"\x89PNG"
            with pytest.raises(NotImplementedError, match="typed ACI"):
                await sandbox.remote(print, "hi")

    _run(scenario())
    assert fake.calls["connect"] == 1


# ------------------------------------------------------------------------ live (Docker)

requires_docker = pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCKER_TESTS") != "1",
    reason="live Docker test: set SHINKEN_DOCKER_TESTS=1 (needs the shinken/sandbox-linux image)",
)


@requires_docker
def test_live_agentix_session_over_docker():
    """Their create→use→delete lifecycle over the real DockerLocalProvider: session scopes
    the sandbox, get() reports running, the ACI observes a real desktop."""
    from shinken.providers.docker import DockerLocalProvider

    # generous startup timeout: the live smoke shares the daemon with whatever else runs
    prov = ax.ShinkenAgentixProvider(
        DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    )

    async def scenario():
        async with prov.session({"image": "shinken/sandbox-linux"}) as sandbox:
            assert sandbox.runtime_url.startswith("ws://127.0.0.1:")
            info = await prov.get(sandbox.sandbox_id)
            assert info.status == "running"
            shot = await asyncio.to_thread(sandbox.aci().screenshot)
            assert shot["bytes"][:4] == b"\x89PNG"
            health = await sandbox.health()
            assert health["status"] == "ok"
            sid = sandbox.sandbox_id
        with pytest.raises(KeyError):  # session() deleted it on exit
            await prov.get(sid)

    _run(scenario())
