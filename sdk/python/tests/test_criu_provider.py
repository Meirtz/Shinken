"""CRIU memory-tier provider (snapshot_kind="process") — offline contract tests.

These pin the tier's shape without Docker: the loud privileged posture, the
dump-(--leave-running)+commit checkpoint pairing, the idle-boot + parked-PID +
criu-restore fork path, and the donor-token inheritance. The live proof
(dump → restore → the in-memory marker survives → donor still live) is
``scripts/criu_smoke.py``; the measured numbers are ``benchmarks/bench_fork.py``
in memory mode.
"""

from __future__ import annotations

import subprocess

import pytest

import shinken
from shinken.providers import CriuDockerProvider, SandboxSpec
from shinken.providers.base import ProviderError, SandboxHandle
from shinken.providers.criu import (
    MemoryMarkerError,
    read_memory_marker,
    verify_memory_marker,
)


def _handle(cid: str = "donor1", token: str = "donor-token") -> SandboxHandle:
    return SandboxHandle(
        provider="docker-criu",
        sandbox_id=cid,
        addr="127.0.0.1:1",
        token=token,
        metadata={"container_id": cid, "image": "shinken/sandbox-linux-criu"},
    )


def _mock_docker(monkeypatch, *, ns_last_pid: str = "431") -> list[list[str]]:
    """Capture every docker invocation; exec replies with a parked-PID readout."""
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        out = "cid\n"
        if cmd[:2] == [
            "docker",
            "exec",
        ] and "criu dump" in " ".join(cmd):
            out = f"{ns_last_pid}\n"
        elif cmd[:2] == ["docker", "images"] or cmd[:2] == ["docker", "ps"]:
            # List queries (the v2 label sweeps in cleanup_snapshots/list/gc) see an
            # empty daemon — only the provider's own in-memory ledger exists here.
            out = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19020)
    monkeypatch.setattr(CriuDockerProvider, "_wait_ready", lambda _self, _handle: None)
    return calls


# --- tier selection + capability honesty ---------------------------------------------


def test_capabilities_advertise_process_tier_and_privilege_loudly():
    caps = CriuDockerProvider().capabilities
    assert caps.name == "docker-criu"
    assert caps.snapshot_kind == "process"
    assert caps.requires_privileged is True
    assert caps.supports_snapshot and caps.supports_fork
    assert caps.supports_checkpoint and caps.supports_resume
    assert caps.isolation == "container" and caps.display == "x11"
    notes = " ".join(caps.notes).lower()
    assert "privileged: true" in notes
    assert "not an isolation posture" in notes


def test_registry_and_top_level_export():
    assert shinken.CriuDockerProvider is CriuDockerProvider
    provider = shinken.providers.get("docker-criu")
    assert isinstance(provider, CriuDockerProvider)


def test_base_docker_tier_is_unchanged():
    # The opt-in tier must not leak into the default disk tier.
    caps = shinken.DockerLocalProvider().capabilities
    assert caps.snapshot_kind == "disk"
    assert caps.requires_privileged is False


def test_warm_pool_kwargs_are_rejected():
    with pytest.raises(ProviderError, match="files-only"):
        CriuDockerProvider(warm_pool_size=4)


# --- container shaping -----------------------------------------------------------------


def test_create_runs_privileged_init_with_images_volume(monkeypatch):
    calls = _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="ckpt-vol-test")
    provider.create(SandboxSpec())
    vol_create = next(c for c in calls if c[:3] == ["docker", "volume", "create"])
    assert "ckpt-vol-test" in vol_create
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--privileged" in run_cmd
    assert "--init" in run_cmd
    assert "ckpt-vol-test:/ckpt" in run_cmd
    assert any(a.startswith("SHINKEN_CRIU_PID_FLOOR=") for a in run_cmd)
    assert run_cmd[-1] == "shinken/sandbox-linux-criu"  # image CMD = supervised boot


# --- checkpoint: criu dump --leave-running + docker commit ------------------------------


def test_snapshot_dumps_leave_running_then_commits(monkeypatch):
    calls = _mock_docker(monkeypatch, ns_last_pid="431")
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="golden")
    assert snap == "shinken-memsnap:golden"
    exec_cmd = next(c for c in calls if c[:2] == ["docker", "exec"])
    script = exec_cmd[-1]
    assert "criu dump" in script
    assert "--leave-running" in script  # donor stays live — a true checkpoint
    assert "--tcp-close" in script
    assert "/tmp/shinken-tree.pid" in script
    assert "/ckpt/golden" in script
    commit = next(c for c in calls if c[:2] == ["docker", "commit"])
    assert commit[2] == "donor1" and commit[3] == snap
    # the parked restore floor sits ABOVE the donor's recorded PID range
    assert provider._mem[snap]["park"] == 431 + 128
    assert provider._mem[snap]["token"] == "donor-token"


# --- restore: idle boot + parked PIDs + criu restore ------------------------------------


def test_restore_boots_idle_parks_pids_and_restores(monkeypatch):
    calls = _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="g2")
    calls.clear()
    child = provider.restore(snap)
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    # restore target boots IDLE off the committed image — nothing races the restore
    assert run_cmd[-3:] == [snap, "sleep", "infinity"]
    assert "--privileged" in run_cmd and "--init" in run_cmd
    restore_exec = next(
        c for c in calls if c[:2] == ["docker", "exec"] and "criu restore" in " ".join(c)
    )
    script = restore_exec[-1]
    assert f"echo {431 + 128} > /proc/sys/kernel/ns_last_pid" in script
    assert "--restore-detached" in script and "--tcp-close" in script
    # the replica handle carries the DONOR's token (the restored shinkend keeps it)
    assert child.token == "donor-token"
    assert child.metadata["memory_restore"] is True
    assert child.metadata["image"] == snap


def test_restore_unknown_snapshot_raises():
    provider = CriuDockerProvider(images_volume="v")
    with pytest.raises(ProviderError, match="unknown memory snapshot"):
        provider.restore("shinken-memsnap:never-taken")


def test_fork_is_dump_commit_restore(monkeypatch):
    calls = _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="v")
    child = provider.fork(_handle("donor1"))
    joined = [" ".join(c) for c in calls]
    dump_i = next(i for i, c in enumerate(joined) if "criu dump" in c)
    commit_i = next(i for i, c in enumerate(joined) if c.startswith("docker commit"))
    restore_i = next(i for i, c in enumerate(joined) if "criu restore" in c)
    assert dump_i < commit_i < restore_i
    assert child.metadata["memory_restore"] is True


def test_checkpoint_resume_roundtrip_uses_memory_path(monkeypatch):
    _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="v")
    ckpt = provider.checkpoint(_handle("donor1"), event_seq=7)
    assert ckpt.startswith("ckpt-")
    resumed = provider.resume(ckpt)
    assert resumed.metadata["memory_restore"] is True


def test_delete_snapshot_reclaims_image_and_images_dir(monkeypatch):
    _mock_docker(monkeypatch)
    removed: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):
        removed.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.criu.subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="g3")
    provider.delete_snapshot(snap)
    assert snap not in provider._mem
    rmi = next(c for c in removed if c[:2] == ["docker", "rmi"])
    assert snap in rmi
    rm_dir = next(c for c in removed if "rm" in c and "/ckpt/g3" in c)
    assert "--rm" in rm_dir and "v:/ckpt" in rm_dir


def test_cleanup_snapshots_removes_volume(monkeypatch):
    _mock_docker(monkeypatch)
    removed: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):
        removed.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.criu.subprocess.run", fake_subprocess_run)
    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)
    provider = CriuDockerProvider(images_volume="v")
    provider.snapshot(_handle("donor1"), name="g4")
    count = provider.cleanup_snapshots()
    assert count == 1
    assert ["docker", "volume", "rm", "-f", "v"] in removed
    # the per-snapshot rm container is skipped — the volume goes away wholesale
    assert not any("/ckpt/g4" in " ".join(c) for c in removed)


# --- the process-memory marker (the state files-only tiers cannot carry) ----------------


class _FakeEnv:
    """Answers exec() like a guest whose marker process is (or is not) alive."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.launched: list[tuple] = []

    def launch_app(self, app, args):
        self.launched.append((app, args))

    def exec(self, argv, timeout=None):
        return self.replies.pop(0)


def test_read_memory_marker_parses_live_answer():
    env = _FakeEnv([{"exit_code": 0, "stdout": "abc123:4200:317\n", "stderr": ""}])
    reading = read_memory_marker(env)
    assert reading == {"nonce": "abc123", "beats": 4200, "pid": 317}


def test_read_memory_marker_raises_on_dead_process():
    # exactly what a files-only restore produces: the pidfile exists, the process doesn't
    env = _FakeEnv([{"exit_code": 4, "stdout": "no-such-process:317\n", "stderr": ""}])
    with pytest.raises(MemoryMarkerError, match="no-such-process"):
        read_memory_marker(env)


def test_verify_memory_marker_requires_same_pid_nonce_and_progress():
    baseline = {"nonce": "n1", "beats": 100, "pid": 317}
    live = _FakeEnv([{"exit_code": 0, "stdout": "n1:150:317", "stderr": ""}])
    assert verify_memory_marker(live, baseline)["ok"] is True
    restarted = _FakeEnv([{"exit_code": 0, "stdout": "n1:3:317", "stderr": ""}])
    assert verify_memory_marker(restarted, baseline)["ok"] is False  # counter reset
    other_pid = _FakeEnv([{"exit_code": 0, "stdout": "n1:150:999", "stderr": ""}])
    assert verify_memory_marker(other_pid, baseline)["ok"] is False
    dead = _FakeEnv([{"exit_code": 4, "stdout": "no-such-process:317", "stderr": ""}])
    out = verify_memory_marker(dead, baseline)
    assert out["ok"] is False and "no-such-process" in out["error"]
