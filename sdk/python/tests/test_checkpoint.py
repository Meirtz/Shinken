"""sandbox.checkpoint()/fork()/resume provider wiring (#206)."""

from __future__ import annotations

import subprocess

import pytest

import shinken
from shinken.providers import DockerLocalProvider
from shinken.providers.base import SandboxHandle


class _FakeProvider:
    """Minimal provider context for runtime-state paths."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def checkpoint(self, handle, *, event_seq=None, agent_state_ref=None) -> str:
        self.calls.append(("checkpoint", handle, event_seq, agent_state_ref))
        return "ckpt-abc"

    def fork(self, handle):
        self.calls.append(("fork", handle))
        return "forked-handle"

    def resume(self, handle_or_checkpoint):
        self.calls.append(("resume", handle_or_checkpoint))
        return "resumed-handle"


def test_sandbox_checkpoint_calls_provider(mock_shinkend):
    fake = _FakeProvider()
    handle = object()
    with shinken.connect(mock_shinkend) as env:
        env._set_provider_context(fake, handle)  # mimic provider.connect() injection
        ckpt = env.checkpoint("task-1", agent_state_ref="agent://s")
        assert ckpt == "ckpt-abc"

    assert fake.calls == [("checkpoint", handle, None, "agent://s")]


def test_sandbox_fork_and_resume_call_provider(mock_shinkend):
    fake = _FakeProvider()
    with shinken.connect(mock_shinkend) as env:
        env._set_provider_context(fake, "handle-1")
        assert env.fork() == "forked-handle"
        assert env.resume("ckpt-1") == "resumed-handle"

    assert fake.calls == [("fork", "handle-1"), ("resume", "ckpt-1")]


def test_checkpoint_requires_a_provider_managed_session(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(RuntimeError, match="provider-managed"):
            env.checkpoint("no-provider")


def test_docker_connect_injects_context_and_checkpoint(mock_shinkend, monkeypatch):
    # End-to-end through the real DockerLocalProvider (docker commit mocked): connect()
    # attaches the provider context, and checkpoint() runs provider.checkpoint.
    monkeypatch.setattr(
        "shinken.providers.docker._run",
        lambda cmd, timeout=30.0: subprocess.CompletedProcess(cmd, 0, stdout="img\n", stderr=""),
    )
    handle = SandboxHandle(
        provider="docker-local",
        sandbox_id="shk",
        addr=mock_shinkend,
        metadata={"container_id": "shk"},
    )
    env = DockerLocalProvider().connect(handle)
    try:
        env.click(x=3, y=4)
        ckpt = env.checkpoint("milestone")
        assert ckpt.startswith("ckpt-")  # DockerLocalProvider.checkpoint id
    finally:
        env.close()
