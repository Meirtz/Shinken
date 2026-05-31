"""sandbox.checkpoint() ↔ .skn snapshot_ref linkage (#206 / B2).

A checkpoint snapshots substrate state via the provider and records a `snapshot_ref`
event anchored at the current replay offset — the link between replay (evidence) and
runtime state (the restorable thing)."""

from __future__ import annotations

import subprocess

import pytest

import shinken
from shinken.providers import DockerLocalProvider
from shinken.providers.base import SandboxHandle
from shinken.skn import Recorder, Replay


def test_recorder_snapshot_ref_event():
    rec = Recorder(platform="linux", capabilities={})
    rec.marker("start")
    ev = rec.snapshot_ref("ckpt-1", name="task-start", agent_state_ref="agent://s")
    assert ev["kind"] == "snapshot_ref"
    assert ev["src"] == "checkpoint"
    assert ev["snapshot_ref"] == "ckpt-1"
    assert ev["payload"] == {"name": "task-start", "agent_state_ref": "agent://s"}
    assert ev["seq"] == 1  # after the marker at seq 0


class _FakeProvider:
    """Minimal provider context for the checkpoint path."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def checkpoint(self, handle, *, event_seq=None, agent_state_ref=None) -> str:
        self.calls.append((event_seq, agent_state_ref))
        return "ckpt-abc"


def test_sandbox_checkpoint_records_snapshot_ref_at_replay_offset(mock_shinkend, tmp_path):
    fake = _FakeProvider()
    path = tmp_path / "c.skn"
    with shinken.connect(mock_shinkend, record=True) as env:
        env.click(x=1, y=2)  # advance the replay offset first
        env._set_provider_context(fake, object())  # mimic provider.connect() injection
        ckpt = env.checkpoint("task-1", agent_state_ref="agent://s")
        assert ckpt == "ckpt-abc"
        env.save_replay(str(path))

    # provider.checkpoint called once, with the current replay offset + agent ref
    assert len(fake.calls) == 1
    event_seq, agent_ref = fake.calls[0]
    assert agent_ref == "agent://s"

    rp = Replay.load(str(path))
    snap = next(e for e in rp.events if e["kind"] == "snapshot_ref")
    assert snap["snapshot_ref"] == "ckpt-abc"
    assert snap["payload"]["name"] == "task-1"
    # the offset handed to the provider == the snapshot_ref event's own seq (the link)
    assert snap["seq"] == event_seq


def test_checkpoint_requires_a_provider_managed_session(mock_shinkend):
    with shinken.connect(mock_shinkend, record=True) as env:
        with pytest.raises(RuntimeError, match="provider-managed"):
            env.checkpoint("no-provider")


def test_docker_connect_injects_context_and_checkpoint_links_replay(
    mock_shinkend, tmp_path, monkeypatch
):
    # End-to-end through the real DockerLocalProvider (docker commit mocked): connect()
    # attaches the provider context, and checkpoint() runs provider.checkpoint + records
    # the snapshot_ref event.
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
    path = tmp_path / "d.skn"
    env = DockerLocalProvider().connect(handle, record=True)
    try:
        env.click(x=3, y=4)
        ckpt = env.checkpoint("milestone")
        assert ckpt.startswith("ckpt-")  # DockerLocalProvider.checkpoint id
        env.save_replay(str(path))
    finally:
        env.close()

    rp = Replay.load(str(path))
    snap = next(e for e in rp.events if e["kind"] == "snapshot_ref")
    assert snap["snapshot_ref"] == ckpt
    assert snap["payload"]["name"] == "milestone"
