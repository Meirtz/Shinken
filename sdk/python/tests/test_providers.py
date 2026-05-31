"""Sandbox provider contracts and local implementations."""

from __future__ import annotations

import subprocess

import pytest

import shinken
from shinken.providers import (
    DockerLocalProvider,
    ExternalProvider,
    SandboxSpec,
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
    assert "shinken/test" == cmd[-1]
    assert handle.addr == "127.0.0.1:19001"
    assert handle.metadata["container_id"] == "container-id"


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


def test_docker_default_network_mode_is_bridge_and_recorded(monkeypatch):
    # #152: default is bridge (guest has egress), the port is published, and the actual
    # mode + egress posture are recorded so callers aren't misled.
    cmd, handle = _docker_create(monkeypatch)
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "bridge"
    assert "-p" in cmd and "127.0.0.1:19009:8765" in cmd
    assert handle.metadata["network_mode"] == "bridge"
    assert handle.metadata["guest_egress"] is True


def test_docker_network_none_omits_port_and_records_no_egress(monkeypatch):
    # #152: network_mode="none" gives the guest no network (no egress) and omits -p
    # (incompatible with --network none); metadata records the no-egress posture.
    cmd, handle = _docker_create(monkeypatch, network_mode="none")
    assert "--network" in cmd and cmd[cmd.index("--network") + 1] == "none"
    assert "-p" not in cmd
    assert handle.metadata["network_mode"] == "none"
    assert handle.metadata["guest_egress"] is False


def test_docker_invalid_network_mode_rejected():
    with pytest.raises(ProviderError):
        DockerLocalProvider(network_mode="airplane")


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
    removed: list[str] = []

    def fake_subprocess_run(cmd, **_kwargs):
        removed.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    provider = DockerLocalProvider(image="shinken/test", name_prefix="test-sandbox")
    with pytest.raises(RuntimeError, match="not ready"):
        provider.create()
    assert removed[:3] == ["docker", "rm", "-f"]


def test_docker_provider_cleanup_uses_labels(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: float = 30.0):
        commands.append(cmd)
        if cmd[:3] == ["docker", "ps", "-aq"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="a\nb\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    provider = DockerLocalProvider(name_prefix="test-sandbox")

    assert provider.cleanup_orphans() == 2
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
    # #153: secret env values are masked when a command is rendered for an error/log
    masked = _redact_cmd(["docker", "run", "-e", "SHINKEND_TOKEN=deadbeef", "img"])
    assert "deadbeef" not in masked
    assert "SHINKEND_TOKEN=***" in masked
    # non-secret args are preserved verbatim
    assert _redact_cmd(["SCREEN_GEOMETRY=1280x800x24"]) == "SCREEN_GEOMETRY=1280x800x24"


def test_run_error_does_not_leak_token(monkeypatch):
    # #153: a failing docker invocation must not echo the runtime token into the error
    def boom(cmd, **_kwargs):
        raise subprocess.CalledProcessError(1, cmd, output="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ProviderError) as exc:
        _run(["docker", "run", "-e", "SHINKEND_TOKEN=supersecret", "img"])
    msg = str(exc.value)
    assert "supersecret" not in msg
    assert "SHINKEND_TOKEN=***" in msg


def test_top_level_exports_provider_types():
    assert shinken.DockerLocalProvider is DockerLocalProvider
    assert shinken.ExternalProvider is ExternalProvider
    assert shinken.SandboxSpec is SandboxSpec


# --- runtime-state primitives, disk tier (#206) -----------------------------------


def _handle(cid: str = "c1") -> SandboxHandle:
    return SandboxHandle(
        provider="docker-local", sandbox_id=cid, addr="127.0.0.1:1", metadata={"container_id": cid}
    )


def _mock_docker(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

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
    assert snap == "shinken-snap:base"


def test_docker_fork_snapshots_then_launches_from_image(monkeypatch):
    calls = _mock_docker(monkeypatch)
    child = DockerLocalProvider().fork(_handle("c1"))
    assert any(c[:2] == ["docker", "commit"] for c in calls)
    run_cmd = next(c for c in calls if c[:2] == ["docker", "run"])
    assert run_cmd[-1].startswith("shinken-snap:")  # launched from the snapshot image
    assert child.metadata["image"].startswith("shinken-snap:")


def test_docker_checkpoint_binds_offset_and_resume_restores_it(monkeypatch):
    _mock_docker(monkeypatch)
    p = DockerLocalProvider()
    ckpt = p.checkpoint(_handle("c1"), event_seq=42, agent_state_ref="agent://x")
    assert ckpt.startswith("ckpt-")
    rec = p._checkpoints[ckpt]
    assert rec["event_seq"] == 42 and rec["agent_state_ref"] == "agent://x"
    resumed = p.resume(ckpt)  # resume a checkpoint id -> restore its snapshot
    assert resumed.metadata["image"] == rec["snapshot_id"]


def test_docker_resume_requires_id_not_live_handle():
    with pytest.raises(ProviderError):
        DockerLocalProvider().resume(_handle("c1"))
