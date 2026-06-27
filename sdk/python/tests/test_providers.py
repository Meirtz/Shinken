"""Sandbox provider contracts and local implementations."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tarfile

import pytest

import shinken
from shinken.providers import (
    DockerLocalProvider,
    ExternalProvider,
    SandboxSpec,
    StateFidelity,
    UnsatisfiedSandboxSpec,
    UnsupportedProviderOperation,
)
from shinken.providers.base import ProviderError, SandboxHandle
from shinken.providers.docker import _parse_mem_usage, _redact_cmd, _run


def test_external_provider_connects_to_existing_runtime(mock_shinkend):
    provider = ExternalProvider(addr=mock_shinkend)
    handle = provider.create(SandboxSpec(metadata={"sandbox_id": "external-test"}))

    health = provider.health(handle)
    assert health.ready is True
    assert health.screenshot_bytes is not None and health.screenshot_bytes > 0

    with provider.connect(handle) as env:
        assert env.platform == "linux"

    same = provider.reset(handle)
    assert same is handle
    assert same.metadata["reset_strategy"] == "provider_managed"
    provider.destroy(handle)
    assert handle.metadata["destroyed"] is True


def test_external_provider_does_not_advertise_snapshot_or_fork(mock_shinkend):
    provider = ExternalProvider(addr=mock_shinkend)
    handle = provider.create()

    with pytest.raises(UnsupportedProviderOperation):
        provider.snapshot(handle)
    with pytest.raises(UnsupportedProviderOperation):
        provider.fork(handle)


def test_docker_provider_builds_run_command(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 30.0):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19001)
    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", lambda _self, _handle: None)

    provider = DockerLocalProvider(image="shinken/test", name_prefix="test-sandbox")
    handle = provider.create(
        SandboxSpec(
            memory="512m",
            cpus=1.5,
            pids_limit=128,
            shm_size="128m",
            screen_geometry="1024x768x24",
        )
    )

    cmd = commands[0]
    assert cmd[:4] == ["docker", "run", "-d", "--rm"]
    assert "--label" in cmd
    assert "shinken.provider=docker-local" in cmd
    assert "shinken.name_prefix=test-sandbox" in cmd
    assert "-p" in cmd and "127.0.0.1:19001:8765" in cmd
    assert "--memory" in cmd and "512m" in cmd
    assert "--cpus" in cmd and "1.5" in cmd
    assert "--pids-limit" in cmd and "128" in cmd
    assert "--shm-size" in cmd and "128m" in cmd
    assert "SHINKEND_ENABLE_EXEC=1" in cmd
    assert "shinken/test" == cmd[-1]
    assert handle.addr == "127.0.0.1:19001"
    assert handle.metadata["container_id"] == "container-id"


def test_docker_reset_replays_complete_recorded_spec(monkeypatch):
    provider = DockerLocalProvider()
    original = SandboxSpec(
        image="custom-image",
        os="linux",
        needs_gui=False,
        needs_gpu=True,
        fast_reset=True,
        state_fidelity="process_memory",
        memory="3g",
        cpus=2.5,
        pids_limit=77,
        shm_size="256m",
        screen_geometry="1440x900x24",
        extra_env={"VISIBLE_SETTING": "yes"},
        metadata={"suite": "full-reset-contract"},
    )
    handle = SandboxHandle(
        provider="docker-local",
        sandbox_id="old",
        addr="127.0.0.1:1",
        metadata={"sandbox_spec": provider._spec_to_record(original)},
    )
    replacement = SandboxHandle("docker-local", "new", "127.0.0.1:2")
    destroyed: list[SandboxHandle] = []
    created: list[SandboxSpec] = []
    monkeypatch.setattr(provider, "destroy", lambda value: destroyed.append(value))

    def capture_create(spec):
        created.append(spec)
        return replacement

    monkeypatch.setattr(provider, "create", capture_create)
    assert provider.reset(handle) is replacement
    assert destroyed == [handle]
    assert created == [original]


def test_docker_destroy_failure_is_reported_and_not_marked_destroyed(monkeypatch):
    handle = SandboxHandle("docker-local", "still-running", "127.0.0.1:1")

    def fail_rm(cmd, **kwargs):
        assert cmd == ["docker", "rm", "-f", "still-running"]
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(1, cmd, stderr="docker daemon unavailable")

    monkeypatch.setattr(subprocess, "run", fail_rm)
    with pytest.raises(ProviderError, match="docker daemon unavailable"):
        DockerLocalProvider().destroy(handle)
    assert "destroyed" not in handle.metadata


def test_docker_destroy_is_idempotent_after_success(monkeypatch):
    calls: list[list[str]] = []

    def ok_rm(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", ok_rm)
    handle = SandboxHandle("docker-local", "one-shot", "127.0.0.1:1")
    provider = DockerLocalProvider()
    provider.destroy(handle)
    provider.destroy(handle)
    assert calls == [["docker", "rm", "-f", "one-shot"]]


def test_docker_destroy_retries_fork_snapshot_cleanup(monkeypatch):
    rm_calls: list[list[str]] = []

    def remove_container(cmd, timeout=30.0):
        rm_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", remove_container)
    provider = DockerLocalProvider()
    delete_calls = []

    def flaky_delete(snapshot):
        delete_calls.append(snapshot)
        if len(delete_calls) == 1:
            raise ProviderError("transient rmi failure")

    monkeypatch.setattr(provider, "delete_snapshot", flaky_delete)
    handle = SandboxHandle(
        "docker-local",
        "fork-child",
        "127.0.0.1:1",
        metadata={"fork_snapshot": "shinken-snap:parent"},
    )
    with pytest.raises(ProviderError, match="transient rmi failure"):
        provider.destroy(handle)
    assert handle.metadata["destroyed"] is True
    assert handle.metadata["fork_snapshot"] == "shinken-snap:parent"

    provider.destroy(handle)
    provider.destroy(handle)
    assert rm_calls == [["docker", "rm", "-f", "fork-child"]]
    assert delete_calls == ["shinken-snap:parent", "shinken-snap:parent"]
    assert "fork_snapshot" not in handle.metadata


def _docker_create(monkeypatch, **kwargs):
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 30.0):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19009)
    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", lambda _self, _handle: None)
    handle = DockerLocalProvider(**kwargs).create()
    return commands[0], handle


def test_docker_extra_env_passes_through_but_never_reserved_keys(monkeypatch):
    """SandboxSpec.extra_env reaches `docker run -e` (e.g. SHINKEND_DAMAGE=off for the
    damage-vs-poll A/B), while provider-reserved names can't be overridden — docker's
    last -e wins, so a trusted reserved key appended after ours would win silently."""
    from shinken.providers.base import SandboxSpec

    commands: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 30.0):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19009)
    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", lambda _self, _handle: None)
    DockerLocalProvider().create(
        SandboxSpec(
            extra_env={
                "SHINKEND_DAMAGE": "off",
                "SHINKEND_TOKEN": "evil",
                "SHINKEND_ENABLE_EXEC": "0",
                "SCREEN_GEOMETRY": "1x1",
            }
        )
    )
    cmd = commands[0]
    assert "SHINKEND_DAMAGE=off" in cmd
    assert "SHINKEND_TOKEN=evil" not in cmd, "reserved env must not be overridable"
    assert "SHINKEND_ENABLE_EXEC=0" not in cmd
    assert cmd.count("SHINKEND_ENABLE_EXEC=1") == 1
    assert "SCREEN_GEOMETRY=1x1" not in cmd
    assert sum(1 for a in cmd if a.startswith("SCREEN_GEOMETRY=")) == 1


def test_docker_default_network_mode_is_bridge_and_recorded(monkeypatch):
    # #152: default is bridge (guest has egress), the port is published, and the actual
    # mode + egress posture are recorded so callers aren't misled.
    cmd, handle = _docker_create(monkeypatch)
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "bridge"
    assert "-p" in cmd and "127.0.0.1:19009:8765" in cmd
    assert handle.metadata["network_mode"] == "bridge"
    assert handle.metadata["guest_egress"] is True


def test_docker_network_none_fails_before_docker_run(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shinken.providers.docker._run",
        lambda cmd, **_kwargs: calls.append(cmd),
    )
    with pytest.raises(ProviderError, match="before docker run"):
        DockerLocalProvider(network_mode="none").create()
    assert calls == []


def test_docker_invalid_network_mode_rejected():
    with pytest.raises(ProviderError):
        DockerLocalProvider(network_mode="airplane")


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        (SandboxSpec(os="windows"), "os"),
        (SandboxSpec(needs_gpu=True), "needs_gpu"),
        (SandboxSpec(fast_reset=True), "fast_reset"),
        (SandboxSpec(state_fidelity="process_memory"), "state_fidelity"),
    ],
)
def test_docker_unsatisfied_spec_is_typed_and_preflighted(monkeypatch, spec, field):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shinken.providers.docker._run",
        lambda cmd, **_kwargs: calls.append(cmd),
    )
    with pytest.raises(UnsatisfiedSandboxSpec) as raised:
        DockerLocalProvider().create(spec)
    assert raised.value.field == field
    assert calls == []


def test_unproven_warm_pool_is_rejected_explicitly():
    with pytest.raises(ProviderError, match="cannot yet be proven equivalent"):
        DockerLocalProvider(warm_pool_size=1)


def test_docker_provider_cleans_up_if_readiness_fails(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 30.0):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19002)

    def fail_wait(_self, _handle):
        raise RuntimeError("not ready")

    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", fail_wait)

    provider = DockerLocalProvider(image="shinken/test", name_prefix="test-sandbox")
    with pytest.raises(RuntimeError, match="not ready"):
        provider.create()
    assert any(cmd[:3] == ["docker", "rm", "-f"] for cmd in commands)


def test_docker_provider_destroy_all_uses_labels(monkeypatch):
    # destroy_all() is the RENAMED blunt sweep cleanup_orphans() used to be: every
    # labeled container goes, including live siblings (cleanup_orphans is now
    # owner-aware — see test_api_v2.py).
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 30.0):
        commands.append(cmd)
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="a\nb\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    provider = DockerLocalProvider(name_prefix="test-sandbox")

    assert provider.destroy_all() == 2
    assert "label=shinken.provider=docker-local" in commands[0]
    assert "label=shinken.name_prefix=test-sandbox" in commands[0]
    # cleanup selects by label only — the fragile substring `name=^prefix-` filter is
    # gone, so it can't silently miss containers and leave orphans (#157)
    assert not any(arg.startswith("name=") for arg in commands[0])
    assert commands[1] == ["docker", "rm", "-f", "a", "b"]


def test_docker_memory_parser():
    assert _parse_mem_usage("12.5MiB / 1GiB") == 13_107_200
    assert _parse_mem_usage("1.25GB / 2GB") == 1_250_000_000
    assert _parse_mem_usage("") is None


def test_redact_cmd_masks_secret_env():
    # #153: every Docker -e/--env value is masked, including names without a conventional
    # secret suffix. Inline long/short flag forms follow the same rule.
    masked = _redact_cmd(
        [
            "docker",
            "run",
            "-e",
            "SHINKEND_TOKEN=deadbeef",
            "--env",
            "DATABASE_URL=postgres://user:pass@db/prod",
            "--env=AUTHORIZATION=Bearer sensitive-auth",
            "-e=SESSION_COOKIE=sensitive-cookie",
            "-e",
            "PASSWD=hunter2",
            "-e",
            "PUBLIC_SETTING=not-secret-but-still-private",
            "img",
        ]
    )
    for value in (
        "deadbeef",
        "postgres://user:pass@db/prod",
        "Bearer sensitive-auth",
        "sensitive-cookie",
        "hunter2",
        "not-secret-but-still-private",
    ):
        assert value not in masked
    for key in (
        "SHINKEND_TOKEN",
        "DATABASE_URL",
        "AUTHORIZATION",
        "SESSION_COOKIE",
        "PASSWD",
        "PUBLIC_SETTING",
    ):
        assert f"{key}=***" in masked
    # non-secret args are preserved verbatim
    assert _redact_cmd(["SCREEN_GEOMETRY=1280x800x24"]) == "SCREEN_GEOMETRY=1280x800x24"


@pytest.mark.parametrize(
    "assignment",
    [
        "SHINKEND_TOKEN=supersecret",
        "api_token=supersecret",
        "DATABASE_URL=supersecret",
        "AUTHORIZATION=supersecret",
        "SESSION_COOKIE=supersecret",
        "PASSWD=supersecret",
    ],
)
def test_run_error_does_not_leak_env_value(monkeypatch, assignment):
    # The engine may echo the full assignment in stderr; neither the rendered command nor
    # captured process output may leak the value into ProviderError/logs.
    def boom(cmd, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            cmd,
            output="",
            stderr=f"docker rejected {assignment}; value={assignment.split('=', 1)[1]}",
        )

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ProviderError) as exc:
        _run(["docker", "run", "-e", assignment, "img"])
    msg = str(exc.value)
    assert "supersecret" not in msg
    assert f"{assignment.split('=', 1)[0]}=***" in msg


def test_top_level_exports_provider_types():
    assert shinken.DockerLocalProvider is DockerLocalProvider
    assert shinken.ExternalProvider is ExternalProvider
    assert shinken.SandboxSpec is SandboxSpec
    assert shinken.providers.UnsatisfiedSandboxSpec is UnsatisfiedSandboxSpec
    assert shinken.providers.StateFidelity is StateFidelity


# --- runtime-state primitives, disk tier (#206) -----------------------------------


def _handle(cid: str = "c1") -> SandboxHandle:
    return SandboxHandle(
        provider="docker-local", sandbox_id=cid, addr="127.0.0.1:1", metadata={"container_id": cid}
    )


def _mock_docker(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        if cmd[:2] == ["docker", "commit"]:
            commit_count = sum(c[:2] == ["docker", "commit"] for c in calls)
            out = f"sha256:immutable-{commit_count}\n"
        else:
            out = "cid\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19010)
    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", lambda _self, _handle: None)
    return calls


def test_docker_advertises_disk_runtime_state():
    caps = DockerLocalProvider().capabilities
    assert caps.supports_snapshot and caps.supports_fork
    assert caps.supports_checkpoint and caps.supports_resume
    assert caps.snapshot_kind == "disk"


def test_docker_snapshot_uses_commit(monkeypatch):
    calls = _mock_docker(monkeypatch)
    snap = DockerLocalProvider().snapshot(_handle("c1"), name="base")
    assert calls[0][:2] == ["docker", "commit"]
    assert "c1" in calls[0]
    assert snap.startswith("shinken-snap:") and "base" not in snap
    assert "LABEL shinken.snapshot_name=base" not in calls[0]
    assert "LABEL shinken.checkpoint_id=" in calls[0]
    assert "LABEL shinken.snapshot_name=" in calls[0]


def test_docker_same_human_name_mints_distinct_snapshot_ids(monkeypatch):
    _mock_docker(monkeypatch)
    provider = DockerLocalProvider()
    first = provider.snapshot(_handle("c1"), name="golden")
    second = provider.snapshot(_handle("c1"), name="golden")
    assert first != second
    assert first.startswith("shinken-snap:") and second.startswith("shinken-snap:")


def test_docker_delete_snapshot_retains_ownership_on_rmi_failure(monkeypatch):
    _mock_docker(monkeypatch)
    provider = DockerLocalProvider()
    snapshot = provider.snapshot(_handle("c1"), name="golden")

    def fail_rmi(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="docker daemon unavailable")

    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fail_rmi)
    with pytest.raises(ProviderError, match="docker daemon unavailable"):
        provider.delete_snapshot(snapshot)
    assert snapshot in provider._snapshots
    assert snapshot in provider._snapshot_records
    assert snapshot in provider._snapshot_images

    monkeypatch.setattr(
        "shinken.providers.docker.subprocess.run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    provider.delete_snapshot(snapshot)
    assert snapshot not in provider._snapshots
    assert snapshot not in provider._snapshot_records
    assert snapshot not in provider._snapshot_images


def test_docker_delete_snapshot_accepts_already_absent_image(monkeypatch):
    _mock_docker(monkeypatch)
    provider = DockerLocalProvider()
    snapshot = provider.snapshot(_handle("c1"), name="golden")
    monkeypatch.setattr(
        "shinken.providers.docker.subprocess.run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Error response from daemon: No such image"
        ),
    )
    provider.delete_snapshot(snapshot)
    assert snapshot not in provider._snapshots


def test_docker_delete_snapshot_propagates_discovery_failure(monkeypatch):
    def fail_discovery(cmd, timeout=30.0):
        raise ProviderError("docker daemon unavailable")

    monkeypatch.setattr("shinken.providers.docker._run", fail_discovery)
    with pytest.raises(ProviderError, match="docker daemon unavailable"):
        DockerLocalProvider().delete_snapshot("shinken-snap:unknown")


def test_docker_delete_snapshot_is_idempotent_after_proven_absence(monkeypatch):
    calls = []

    def empty_discovery(cmd, timeout=30.0):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            raise ProviderError("Error response from daemon: No such image")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", empty_discovery)
    DockerLocalProvider().delete_snapshot("shinken-snap:already-gone")
    assert calls and calls[0][:2] == ["docker", "images"]


def test_docker_delete_snapshot_removes_unlabeled_legacy_image(monkeypatch):
    calls = []

    def legacy_discovery(cmd, timeout=30.0):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps([{"Id": "sha256:legacy", "Config": {"Labels": {}}}]),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    removed = []
    monkeypatch.setattr("shinken.providers.docker._run", legacy_discovery)
    monkeypatch.setattr(
        "shinken.providers.docker.subprocess.run",
        lambda cmd, **_kwargs: (
            removed.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        ),
    )
    DockerLocalProvider().delete_snapshot("shinken-snap:legacy")
    assert any(call[:3] == ["docker", "image", "inspect"] for call in calls)
    assert removed == [["docker", "rmi", "-f", "sha256:legacy"]]


def test_docker_rejects_cross_tier_persisted_record():
    provider = DockerLocalProvider()
    record = provider._snapshot_record(
        snapshot_id="shinken-memsnap:foreign",
        spec=SandboxSpec(),
        name=None,
        checkpoint_id=None,
        event_seq=None,
        agent_state_ref=None,
    )
    record["provider"] = "docker-criu"
    record["snapshot_kind"] = "process"
    with pytest.raises(ProviderError, match="belongs to provider"):
        provider._remember_snapshot_record(record, image_ref="sha256:foreign")


def test_docker_fork_snapshots_then_launches_from_image(monkeypatch):
    calls = _mock_docker(monkeypatch)
    child = DockerLocalProvider().fork(_handle("c1"))
    assert any(c[:2] == ["docker", "commit"] for c in calls)
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    assert run_cmd[-1] == "sha256:immutable-1"
    assert child.metadata["image"] == "sha256:immutable-1"
    assert child.metadata["snapshot_id"].startswith("shinken-snap:")


def test_docker_fork_restore_failure_reclaims_intermediate_snapshot(monkeypatch):
    provider = DockerLocalProvider()
    deleted: list[str] = []
    monkeypatch.setattr(provider, "snapshot", lambda _handle: "shinken-snap:intermediate")

    def fail_restore(_snapshot_id):
        raise ProviderError("synthetic restore failure")

    monkeypatch.setattr(provider, "restore", fail_restore)
    monkeypatch.setattr(provider, "delete_snapshot", deleted.append)

    with pytest.raises(ProviderError, match="synthetic restore failure"):
        provider.fork(_handle("c1"))
    assert deleted == ["shinken-snap:intermediate"]


def test_docker_fork_cleanup_failure_does_not_replace_restore_error(monkeypatch, caplog):
    provider = DockerLocalProvider()
    monkeypatch.setattr(provider, "snapshot", lambda _handle: "shinken-snap:intermediate")

    def fail_restore(_snapshot_id):
        raise ProviderError("primary restore failure")

    def fail_delete(_snapshot_id):
        raise ProviderError("secondary cleanup failure")

    monkeypatch.setattr(provider, "restore", fail_restore)
    monkeypatch.setattr(provider, "delete_snapshot", fail_delete)

    with pytest.raises(ProviderError, match="primary restore failure"):
        provider.fork(_handle("c1"))
    assert "failed to delete intermediate snapshot" in caplog.text


def test_docker_checkpoint_binds_offset_and_resume_restores_it(monkeypatch):
    _mock_docker(monkeypatch)
    p = DockerLocalProvider()
    ckpt = p.checkpoint(_handle("c1"), event_seq=42, agent_state_ref="agent://x")
    assert ckpt.startswith("ckpt-")
    rec = p._checkpoints[ckpt]
    assert rec["event_seq"] == 42 and rec["agent_state_ref"] == "agent://x"
    resumed = p.resume(ckpt)  # resume a checkpoint id -> restore its snapshot
    assert resumed.metadata["image"] == p._snapshot_images[rec["snapshot_id"]]
    assert resumed.metadata["snapshot_id"] == rec["snapshot_id"]
    assert resumed.metadata["restore_path"] == "cold"


def test_fresh_provider_rebuilds_checkpoint_from_image_labels(monkeypatch):
    calls = _mock_docker(monkeypatch)
    original = DockerLocalProvider()
    donor = original.create(
        SandboxSpec(
            os="linux",
            memory="768m",
            cpus=1.25,
            screen_geometry="900x700x24",
        )
    )
    checkpoint = original.checkpoint(
        donor,
        name="human-label",
        event_seq=73,
        agent_state_ref="agent://state/73",
    )
    commit = next(cmd for cmd in calls if cmd[:2] == ["docker", "commit"])
    encoded = next(
        arg.split("=", 1)[1]
        for arg in commit
        if arg.startswith("LABEL shinken.snapshot_record.v1=")
    )
    raw_record = base64.urlsafe_b64decode(encoded).decode()
    record = json.loads(raw_record)
    assert record["name"] == "human-label"
    assert record["spec"]["extra_env"] == {}
    assert record["spec"]["redacted_extra_env_keys"] == []

    fresh_calls: list[list[str]] = []

    def fresh_run(cmd, timeout=30.0):
        fresh_calls.append(cmd)
        if cmd[:2] == ["docker", "images"]:
            out = "sha256:persisted-image\n"
        elif cmd[:3] == ["docker", "image", "inspect"]:
            out = json.dumps(
                [
                    {
                        "Id": "sha256:persisted-image",
                        "Config": {"Labels": {DockerLocalProvider._SNAPSHOT_RECORD_LABEL: encoded}},
                    }
                ]
            )
        elif cmd[:2] == ["docker", "run"]:
            out = "fresh-container\n"
        else:
            out = ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fresh_run)
    fresh = DockerLocalProvider()
    rebuilt = fresh.snapshot_spec(checkpoint)
    assert rebuilt is not None
    assert rebuilt.memory == "768m" and rebuilt.cpus == 1.25
    assert rebuilt.screen_geometry == "900x700x24"
    assert rebuilt.extra_env == {}
    assert rebuilt.metadata == {}

    restored = fresh.restore(checkpoint)
    run_cmd = next(cmd for cmd in fresh_calls if cmd[:2] == ["docker", "run"])
    assert run_cmd[-1] == "sha256:persisted-image"
    assert restored.metadata["checkpoint_id"] == checkpoint
    assert restored.metadata["event_seq"] == 73
    assert restored.metadata["agent_state_ref"] == "agent://state/73"
    assert restored.metadata["restore_path"] == "cold"
    assert restored.metadata["pool_status"] == "disabled"


def test_snapshot_persistence_strips_all_caller_env_and_metadata(monkeypatch):
    calls = _mock_docker(monkeypatch)
    original = DockerLocalProvider()
    sentinel_values = {
        "PUBLIC_SETTING": "sentinel-public",
        "API_TOKEN": "sentinel-token",
        "AUTHORIZATION": "Bearer sentinel-auth",
        "SESSION_COOKIE": "sentinel-cookie",
        "DATABASE_URL": "postgres://sentinel-db",
        "PASSWD": "sentinel-passwd",
        "SSH_PRIVATE_KEY_PEM": "sentinel-private-key",
    }
    donor = original.create(
        SandboxSpec(
            extra_env=sentinel_values,
            metadata={
                "purpose": "sentinel-purpose",
                "headers": {
                    "Authorization": "Bearer sentinel-meta-auth",
                    "Cookie": "sentinel-meta-cookie",
                },
                "database_url": "postgres://sentinel-meta-db",
            },
        )
    )
    checkpoint = original.checkpoint(donor)
    commit = next(cmd for cmd in calls if cmd[:2] == ["docker", "commit"])
    assert "ENV SHINKEND_TOKEN=" in commit
    for key in sentinel_values:
        assert f"ENV {key}=" in commit
    encoded = next(
        arg.split("=", 1)[1]
        for arg in commit
        if arg.startswith("LABEL shinken.snapshot_record.v1=")
    )
    raw_record = base64.urlsafe_b64decode(encoded).decode()
    for sentinel in (
        *sentinel_values.values(),
        "sentinel-purpose",
        "sentinel-meta-auth",
        "sentinel-meta-cookie",
        "sentinel-meta-db",
    ):
        assert sentinel not in raw_record
    record = json.loads(raw_record)
    assert record["spec"]["extra_env"] == {}
    assert record["spec"]["redacted_extra_env_keys"] == sorted(sentinel_values)
    assert record["spec"]["metadata"] == {}
    assert record["spec"]["redacted_metadata_paths"] == ["<root>"]

    restored = original.restore(checkpoint)
    same_process_restore = [cmd for cmd in calls if cmd[:2] == ["docker", "run"]][-1]
    for key, value in sentinel_values.items():
        assert f"{key}={value}" in same_process_restore
    assert restored.metadata["purpose"] == "sentinel-purpose"

    fresh_calls: list[list[str]] = []

    def fresh_run(cmd, timeout=30.0):
        fresh_calls.append(cmd)
        if cmd[:2] == ["docker", "images"]:
            out = "sha256:secret-scrubbed\n"
        elif cmd[:3] == ["docker", "image", "inspect"]:
            out = json.dumps(
                [
                    {
                        "Id": "sha256:secret-scrubbed",
                        "Config": {"Labels": {DockerLocalProvider._SNAPSHOT_RECORD_LABEL: encoded}},
                    }
                ]
            )
        else:
            out = "unexpected\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fresh_run)
    fresh = DockerLocalProvider()
    rebuilt = fresh.snapshot_spec(checkpoint)
    assert rebuilt is not None
    assert rebuilt.extra_env == {}
    assert rebuilt.metadata["_shinken_unresolved_secret_env"] == sorted(sentinel_values)
    assert rebuilt.metadata["_shinken_unresolved_metadata"] == ["<root>"]
    with pytest.raises(UnsatisfiedSandboxSpec) as raised:
        fresh.restore(checkpoint)
    assert raised.value.field == "metadata"
    assert raised.value.requested == ["<root>"]
    assert not any(cmd[:2] == ["docker", "run"] for cmd in fresh_calls)


def test_redacted_or_opaque_metadata_cannot_silently_replay():
    provider = DockerLocalProvider()
    persisted = provider._spec_to_record(
        SandboxSpec(metadata={"api_key": "metadata-secret", "opaque": object()}),
        for_label=True,
    )
    raw = json.dumps(persisted)
    assert "metadata-secret" not in raw
    assert persisted["metadata"] == {}
    assert persisted["redacted_metadata_paths"] == ["<root>"]
    reconstructed = provider._spec_from_record(persisted)
    with pytest.raises(UnsatisfiedSandboxSpec) as raised:
        provider._validate_replay_metadata(reconstructed)
    assert raised.value.field == "metadata"


def test_docker_resume_requires_id_not_live_handle():
    with pytest.raises(ProviderError):
        DockerLocalProvider().resume(_handle("c1"))


# --- push-based readiness (S8): one connection, guest-side ready query --------------


class _FakeEnv:
    """Stands in for a connected Sandbox during _wait_ready."""

    def __init__(self, ready_after: int = 1, unknown_query: bool = False):
        self.ready_after = ready_after
        self.unknown_query = unknown_query
        self.query_calls = 0
        self.screenshot_calls = 0
        self.closed = False

    def query(self, q):
        assert q == "ready"
        if self.unknown_query:
            raise RuntimeError("unknown query: ready")
        self.query_calls += 1
        ready = self.query_calls >= self.ready_after
        return {"ready": ready, "x11_up": ready, "root_nonblack": ready}

    def screenshot(self):
        self.screenshot_calls += 1
        return {"png": b"fake"}

    def close(self):
        self.closed = True


def test_wait_ready_polls_guest_query_on_one_persistent_connection(monkeypatch):
    # The readiness hot path must be: ONE connect, then cheap guest-side `ready` polls —
    # never a fresh WS + full screenshot + `docker stats` per poll (the legacy loop).
    env = _FakeEnv(ready_after=3)
    connects = []
    monkeypatch.setattr(DockerLocalProvider, "connect", lambda _self, _h: connects.append(1) or env)

    def no_stats(_self, _h):
        raise AssertionError("docker stats must not run on the readiness hot path")

    monkeypatch.setattr(DockerLocalProvider, "_container_rss", no_stats)
    provider = DockerLocalProvider(startup_timeout=5.0)
    provider._wait_ready(_handle())
    assert len(connects) == 1
    assert env.query_calls == 3  # polled until the guest said ready
    assert env.screenshot_calls == 0  # no PNG pulls on the new path
    assert env.closed


def test_wait_ready_retries_connect_then_times_out(monkeypatch):
    def refuse(_self, _h):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(DockerLocalProvider, "connect", refuse)
    provider = DockerLocalProvider(startup_timeout=0.1)
    with pytest.raises(ProviderError, match="timed out"):
        provider._wait_ready(_handle())


def test_wait_ready_falls_back_to_screenshot_probe_for_old_runtime(monkeypatch):
    # A runtime predating the `ready` query answers "unknown query: ready" — readiness
    # then probes a screenshot on the SAME session (no per-poll reconnect).
    env = _FakeEnv(unknown_query=True)
    monkeypatch.setattr(DockerLocalProvider, "connect", lambda _self, _h: env)
    monkeypatch.setattr("shinken.providers.docker._png_has_non_black_pixel", lambda _data: True)
    provider = DockerLocalProvider(startup_timeout=5.0)
    provider._wait_ready(_handle())
    assert env.screenshot_calls == 1
    assert env.closed


# --- warm-pool state graft (opt-in) --------------------------------------------------


def test_pool_disabled_by_default_no_thread_no_graft_path():
    provider = DockerLocalProvider()
    assert provider._pool is None
    assert provider._pool_thread is None
    # restore() must not consult the pool path at all when disabled
    assert provider._restore_from_pool("shinken-snap:x", SandboxSpec()) is None


def test_graft_excludes_live_runtime_state():
    for path in (
        "/tmp/.X11-unix",
        "/tmp/.X11-unix/X0",
        "/tmp/.X0-lock",
        "/run/dbus/pid",
        "/var/run/something",
        "/dev/shm/x",
        "/proc/1/fd",
        "/sys/fs/cgroup",
    ):
        assert DockerLocalProvider._graft_excluded(path), path
    for path in ("/tmp/shinken_bench_golden.txt", "/home/shinken/.bashrc", "/etc/hosts"):
        assert not DockerLocalProvider._graft_excluded(path), path


def test_pool_compatible_requires_matching_spec():
    provider = DockerLocalProvider(image="img-a")
    assert provider._pool_compatible(SandboxSpec())  # defaults match defaults
    assert not provider._pool_compatible(None)
    assert not provider._pool_compatible(SandboxSpec(screen_geometry="640x480x24"))
    assert not provider._pool_compatible(SandboxSpec(image="shinken-snap:chained"))
    assert not provider._pool_compatible(SandboxSpec(memory="512m"))


def test_capture_delta_comes_from_immutable_image_layer(monkeypatch, tmp_path):
    layer_bytes = io.BytesIO()
    with tarfile.open(fileobj=layer_bytes, mode="w") as layer:
        for name, payload in (
            ("tmp/shinken_bench_golden.txt", b"golden"),
            ("home/shinken/.wh.gone.txt", b""),
            ("home/cache/.wh..wh..opq", b""),
            ("run/dbus/pid", b"excluded-runtime"),
            ("run/dbus/.wh.old", b""),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            layer.addfile(member, io.BytesIO(payload))
    manifest = json.dumps([{"Layers": ["layer.tar"]}]).encode()
    image_bytes = io.BytesIO()
    with tarfile.open(fileobj=image_bytes, mode="w") as image:
        for name, payload in (("manifest.json", manifest), ("layer.tar", layer_bytes.getvalue())):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            image.addfile(member, io.BytesIO(payload))

    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        assert cmd[:3] == ["docker", "image", "save"]
        output_path = cmd[cmd.index("-o") + 1]
        with open(output_path, "wb") as saved:
            saved.write(image_bytes.getvalue())
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    provider = DockerLocalProvider()
    provider._delta_dir = tmp_path
    delta = provider._capture_delta("live-donor-must-not-be-read", "sha256:t1")
    assert calls == [["docker", "image", "save", "-o", str(tmp_path / "t1.image.tar"), "sha256:t1"]]
    assert delta["source"] == "committed_image_layer"
    assert delta["image_ref"] == "sha256:t1"
    assert delta["deletions"] == ["/home/shinken/gone.txt", "/home/cache"]
    assert delta["tar"]
    with tarfile.open(delta["tar"]) as graft:
        assert graft.getnames() == ["tmp/shinken_bench_golden.txt"]
