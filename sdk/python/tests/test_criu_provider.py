"""CRIU memory-tier provider (snapshot_kind="process") — offline contract tests.

These pin the tier's shape without Docker: the loud privileged posture, the
dump-(--leave-stopped)+commit+resume consistency window, the idle-boot + parked-PID +
criu-restore fork path, and the donor-token inheritance. The live proof
(dump → restore → the in-memory marker survives → donor still live) is
``scripts/criu_smoke.py``; the measured numbers are ``benchmarks/bench_fork.py``
in memory mode.
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest

import shinken
from shinken.providers import CriuDockerProvider, DockerLocalProvider, SandboxSpec
from shinken.providers.base import ProviderError, SandboxHandle, UnsatisfiedSandboxSpec
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
        elif cmd[:2] == ["docker", "commit"]:
            commit_count = sum(c[:2] == ["docker", "commit"] for c in calls)
            out = f"sha256:criu-image-{commit_count}\n"
        elif cmd[:2] == ["docker", "exec"] and "/proc/$pid/environ" in " ".join(cmd):
            out = "donor-token\n"
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
    assert "shinken.criu_images_volume=ckpt-vol-test" in run_cmd
    assert any(a.startswith("SHINKEN_CRIU_PID_FLOOR=") for a in run_cmd)
    assert run_cmd[-1] == "shinken/sandbox-linux-criu"  # image CMD = supervised boot


def test_criu_accepts_process_memory_contract(monkeypatch):
    _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="v")
    handle = provider.create(SandboxSpec(state_fidelity="process_memory"))
    assert handle.metadata["sandbox_spec"]["state_fidelity"] == "process_memory"


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (SandboxSpec(os="macos"), "os"),
        (SandboxSpec(needs_gpu=True), "needs_gpu"),
        (SandboxSpec(fast_reset=True), "fast_reset"),
    ],
)
def test_criu_rejects_unsatisfied_spec_before_docker(monkeypatch, spec, field):
    calls: list[list[str]] = []
    monkeypatch.setattr("shinken.providers.docker._run", lambda cmd, **_kwargs: calls.append(cmd))
    with pytest.raises(UnsatisfiedSandboxSpec) as raised:
        CriuDockerProvider(images_volume="v").create(spec)
    assert raised.value.field == field
    assert calls == []


def test_list_recovers_live_process_token_and_volume(monkeypatch):
    rebuilt = SandboxHandle(
        provider="docker-criu",
        sandbox_id="restored",
        addr="127.0.0.1:1",
        token="temporary-container-token",
        metadata={"container_id": "cid", "image": "sha256:source"},
    )
    monkeypatch.setattr(DockerLocalProvider, "list", lambda _self: [rebuilt])

    def fake_run(cmd, timeout=30.0):
        if cmd[:3] == ["docker", "inspect", "-f"]:
            out = "persisted-volume\n"
        elif cmd[:2] == ["docker", "exec"]:
            out = "donor-token\n"
        else:  # pragma: no cover - guards the contract
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="different-default")
    assert provider.list() == [rebuilt]
    assert rebuilt.token == "donor-token"
    assert rebuilt.metadata["criu_images_volume"] == "persisted-volume"
    assert provider._image_volumes["sha256:source"] == "persisted-volume"


# --- checkpoint: one stopped memory+filesystem consistency window -----------------------


def test_snapshot_dumps_stopped_commits_then_resumes_donor(monkeypatch):
    calls = _mock_docker(monkeypatch, ns_last_pid="431")
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="golden")
    assert snap.startswith("shinken-memsnap:") and "golden" not in snap
    dump_i = next(i for i, c in enumerate(calls) if "criu dump" in " ".join(c))
    commit_i = next(i for i, c in enumerate(calls) if c[:2] == ["docker", "commit"])
    resume_i = next(i for i, c in enumerate(calls) if "kill -s CONT -- -1" in " ".join(c))
    assert dump_i < commit_i < resume_i
    assert "/proc/$tree_pid/status" in calls[resume_i][-1]
    script = calls[dump_i][-1]
    assert "criu dump" in script
    assert "--leave-stopped" in script
    assert "--tcp-close" in script
    assert "/tmp/shinken-tree.pid" in script
    assert provider._mem[snap]["images_dir"] in script
    commit = calls[commit_i]
    assert commit[-2:] == ["donor1", snap]
    assert "LABEL shinken.snapshot_name=golden" not in commit
    assert "LABEL shinken.snapshot_name=" in commit
    # the parked restore floor sits ABOVE the donor's recorded PID range
    assert provider._mem[snap]["park"] == 431 + 128
    assert "token" not in provider._mem[snap]


def test_criu_same_human_name_mints_distinct_ids(monkeypatch):
    _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="v")
    first = provider.snapshot(_handle(), name="same")
    second = provider.snapshot(_handle(), name="same")
    assert first != second


def test_criu_resumes_donor_when_commit_fails(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        if "criu dump" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 0, stdout="431\n", stderr="")
        if cmd[:2] == ["docker", "commit"]:
            raise ProviderError("commit failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="v")
    with pytest.raises(ProviderError, match="commit failed"):
        provider.snapshot(_handle())
    commit_i = next(i for i, c in enumerate(calls) if c[:2] == ["docker", "commit"])
    resume_i = next(i for i, c in enumerate(calls) if "kill -s CONT -- -1" in " ".join(c))
    cleanup_i = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "run", "--rm"])
    assert commit_i < resume_i < cleanup_i
    assert any(arg.startswith("/ckpt/") for arg in calls[cleanup_i])


def test_criu_resumes_donor_when_dump_fails(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        if "criu dump" in " ".join(cmd):
            raise ProviderError("dump failed")
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="v")
    with pytest.raises(ProviderError, match="dump failed"):
        provider.snapshot(_handle())
    dump_i = next(i for i, c in enumerate(calls) if "criu dump" in " ".join(c))
    resume_i = next(i for i, c in enumerate(calls) if "kill -s CONT -- -1" in " ".join(c))
    cleanup_i = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "run", "--rm"])
    assert dump_i < resume_i < cleanup_i


def test_criu_retries_and_verifies_donor_resume(monkeypatch):
    calls: list[list[str]] = []
    resume_attempts = 0

    def fake_run(cmd, timeout=30.0):
        nonlocal resume_attempts
        calls.append(cmd)
        joined = " ".join(cmd)
        if "criu dump" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="431\n", stderr="")
        if cmd[:2] == ["docker", "commit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:retry-proof\n", stderr="")
        if "kill -s CONT -- -1" in joined:
            resume_attempts += 1
            if resume_attempts == 1:
                raise ProviderError("transient resume failure")
            assert "/proc/$tree_pid/status" in cmd[-1]
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="v")
    snapshot = provider.snapshot(_handle())
    assert snapshot in provider._snapshots
    assert resume_attempts == provider._DONOR_RESUME_ATTEMPTS


def test_criu_resume_failure_does_not_mask_primary_snapshot_error(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "criu dump" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="431\n", stderr="")
        if cmd[:2] == ["docker", "commit"]:
            raise ProviderError("primary commit failure")
        if "kill -s CONT -- -1" in joined:
            raise ProviderError("resume command failure")
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="v")
    with pytest.raises(ProviderError, match="primary commit failure") as raised:
        provider.snapshot(_handle())
    assert isinstance(raised.value.__cause__, ProviderError)
    assert "failed to resume and verify CRIU donor" in str(raised.value.__cause__)
    resume_indices = [i for i, c in enumerate(calls) if "kill -s CONT -- -1" in " ".join(c)]
    assert len(resume_indices) == provider._DONOR_RESUME_ATTEMPTS
    cleanup_i = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "run", "--rm"])
    assert cleanup_i > max(resume_indices)


def test_criu_resume_failure_removes_committed_but_unpublished_snapshot(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "criu dump" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="431\n", stderr="")
        if cmd[:2] == ["docker", "commit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:unpublished\n", stderr="")
        if "kill -s CONT -- -1" in joined:
            raise ProviderError("resume command failure")
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="v")
    with pytest.raises(ProviderError, match="failed to resume and verify CRIU donor"):
        provider.snapshot(_handle())

    commit = next(c for c in calls if c[:2] == ["docker", "commit"])
    image_tag = commit[-1]
    rmi_i = next(i for i, c in enumerate(calls) if c == ["docker", "rmi", "-f", image_tag])
    dump_cleanup_i = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "run", "--rm"])
    resume_indices = [i for i, c in enumerate(calls) if "kill -s CONT -- -1" in " ".join(c)]
    assert max(resume_indices) < rmi_i < dump_cleanup_i
    assert provider._snapshots == {}
    assert provider._mem == {}


def test_criu_cleanup_failures_do_not_mask_resume_failure(monkeypatch, caplog):
    def fake_run(cmd, timeout=30.0):
        joined = " ".join(cmd)
        if "criu dump" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="431\n", stderr="")
        if cmd[:2] == ["docker", "commit"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:unpublished\n", stderr="")
        if "kill -s CONT -- -1" in joined:
            raise ProviderError("resume command failure")
        if cmd[:2] == ["docker", "rmi"]:
            raise ProviderError("image cleanup failure")
        if cmd[:3] == ["docker", "run", "--rm"]:
            raise ProviderError("dump cleanup failure")
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    provider = CriuDockerProvider(images_volume="v")
    with caplog.at_level("ERROR"):
        with pytest.raises(ProviderError, match="failed to resume and verify CRIU donor"):
            provider.snapshot(_handle())
    assert "failed to remove unpublished CRIU image" in caplog.text
    assert "failed to remove incomplete CRIU dump" in caplog.text


# --- restore: idle boot + parked PIDs + criu restore ------------------------------------


def test_restore_boots_idle_parks_pids_and_restores(monkeypatch):
    calls = _mock_docker(monkeypatch)
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="g2")
    calls.clear()
    child = provider.restore(snap)
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    # restore target boots IDLE off the committed image — nothing races the restore
    assert run_cmd[-3:] == ["sha256:criu-image-1", "sleep", "infinity"]
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
    assert child.metadata["image"] == "sha256:criu-image-1"
    assert child.metadata["snapshot_id"] == snap


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


def test_fresh_criu_provider_recovers_tier_metadata_without_label_token(monkeypatch):
    calls = _mock_docker(monkeypatch)
    original = CriuDockerProvider(images_volume="persisted-volume")
    checkpoint = original.checkpoint(
        _handle("donor1", token="never-in-label"),
        name="memory-golden",
        event_seq=9,
    )
    commit = next(c for c in calls if c[:2] == ["docker", "commit"])
    encoded = next(
        arg.split("=", 1)[1]
        for arg in commit
        if arg.startswith("LABEL shinken.snapshot_record.v1=")
    )
    raw = base64.urlsafe_b64decode(encoded).decode()
    record = json.loads(raw)
    assert "never-in-label" not in raw
    assert "token" not in json.dumps(record["tier_metadata"]).lower()

    fresh_calls: list[list[str]] = []

    def fresh_run(cmd, timeout=30.0):
        fresh_calls.append(cmd)
        joined = " ".join(cmd)
        if cmd[:2] == ["docker", "images"]:
            out = "sha256:persisted-criu\n"
        elif cmd[:3] == ["docker", "image", "inspect"]:
            out = json.dumps(
                [
                    {
                        "Id": "sha256:persisted-criu",
                        "Config": {"Labels": {CriuDockerProvider._SNAPSHOT_RECORD_LABEL: encoded}},
                    }
                ]
            )
        elif cmd[:2] == ["docker", "exec"] and "/proc/$pid/environ" in joined:
            out = "never-in-label\n"
        elif cmd[:2] == ["docker", "run"]:
            out = "restored-container\n"
        else:
            out = "cid\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.criu._run", fresh_run)
    monkeypatch.setattr("shinken.providers.docker._run", fresh_run)
    fresh = CriuDockerProvider(images_volume="different-default")
    restored = fresh.restore(checkpoint)
    snapshot_id = record["snapshot_id"]
    assert fresh._mem[snapshot_id] == {
        "images_dir": record["tier_metadata"]["images_dir"],
        "park": record["tier_metadata"]["park"],
        "images_volume": "persisted-volume",
    }
    run_cmd = next(c for c in fresh_calls if c[:2] == ["docker", "run"])
    assert "persisted-volume:/ckpt" in run_cmd
    assert run_cmd[-3:] == ["sha256:persisted-criu", "sleep", "infinity"]
    assert restored.token == "never-in-label"
    assert restored.metadata["checkpoint_id"] == checkpoint
    assert restored.metadata["event_seq"] == 9


def test_delete_snapshot_reclaims_image_and_images_dir(monkeypatch):
    calls = _mock_docker(monkeypatch)
    removed: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):
        removed.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="g3")
    images_dir = provider._mem[snap]["images_dir"]
    provider.delete_snapshot(snap)
    assert snap not in provider._mem
    rmi = next(c for c in removed if c[:2] == ["docker", "rmi"])
    assert "sha256:criu-image-1" in rmi
    rm_dir = next(c for c in calls if "rm" in c and images_dir in c)
    assert "--rm" in rm_dir and "v:/ckpt" in rm_dir


def test_delete_unlabeled_legacy_snapshot_reclaims_image_and_images_dir(monkeypatch):
    snapshot_id = "shinken-memsnap:legacy"
    images_dir = "/ckpt/" + "a" * 32
    calls: list[list[str]] = []

    def legacy_run(cmd, timeout=30.0):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps([{"Id": "sha256:legacy-criu", "Config": {"Labels": {}}}]),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    removed = []
    monkeypatch.setattr("shinken.providers.criu._run", legacy_run)
    monkeypatch.setattr("shinken.providers.docker._run", legacy_run)
    monkeypatch.setattr(
        "shinken.providers.docker.subprocess.run",
        lambda cmd, **_kwargs: (
            removed.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        ),
    )
    provider = CriuDockerProvider(images_volume="v")
    provider._mem[snapshot_id] = {
        "images_dir": images_dir,
        "park": 1,
        "images_volume": "v",
    }

    provider.delete_snapshot(snapshot_id)
    assert snapshot_id not in provider._mem
    assert any(cmd[:2] == ["docker", "run"] and images_dir in cmd for cmd in calls)
    assert removed == [["docker", "rmi", "-f", "sha256:legacy-criu"]]


def test_cleanup_snapshots_removes_owned_dir_not_shared_volume(monkeypatch):
    calls = _mock_docker(monkeypatch)
    removed: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):
        removed.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)
    provider = CriuDockerProvider(images_volume="v")
    snap = provider.snapshot(_handle("donor1"), name="g4")
    images_dir = provider._mem[snap]["images_dir"]
    count = provider.cleanup_snapshots()
    assert count == 1
    assert ["docker", "volume", "rm", "-f", "v"] not in removed + calls
    assert any(images_dir in " ".join(c) for c in calls)


def test_gc_snapshot_removes_criu_dump_before_image(monkeypatch):
    provider = CriuDockerProvider(images_volume="v")
    snapshot_id = "shinken-memsnap:" + "a" * 32
    images_dir = "/ckpt/" + "a" * 32
    record = provider._snapshot_record(
        snapshot_id=snapshot_id,
        spec=SandboxSpec(state_fidelity="process_memory"),
        name=None,
        checkpoint_id=None,
        event_seq=None,
        agent_state_ref=None,
        tier_metadata={"images_dir": images_dir, "park": 559, "images_volume": "v"},
    )
    encoded = provider._encode_snapshot_record(record)
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        if cmd[:2] == ["docker", "ps"]:
            out = ""
        elif cmd[:3] == ["docker", "images", "-q"]:
            out = "sha256:dead-criu\n"
        elif cmd[:4] == ["docker", "image", "inspect", "-f"]:
            out = "sha256:dead-criu\t99999999\t1.0\n"
        elif cmd[:3] == ["docker", "image", "inspect"]:
            out = json.dumps(
                [
                    {
                        "Id": "sha256:dead-criu",
                        "Config": {"Labels": {CriuDockerProvider._SNAPSHOT_RECORD_LABEL: encoded}},
                    }
                ]
            )
        else:
            out = "cleanup-ok\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    removed: list[list[str]] = []
    monkeypatch.setattr("shinken.providers.criu._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr(
        "shinken.providers.docker.subprocess.run",
        lambda cmd, **_kwargs: removed.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )
    report = provider.gc(snapshots=True, force=True)
    assert report.images == 1
    images_query = next(c for c in calls if c[:3] == ["docker", "images", "-q"])
    assert "label=shinken.provider=docker-criu" in images_query
    cleanup_i = next(i for i, c in enumerate(calls) if images_dir in c)
    inspect_i = next(
        i for i, c in enumerate(calls) if c[:3] == ["docker", "image", "inspect"] and "-f" not in c
    )
    assert inspect_i < cleanup_i
    assert ["docker", "rmi", "-f", "sha256:dead-criu"] in removed


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
