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
from shinken.providers.docker import _parse_mem_usage


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
    assert commands[1] == ["docker", "rm", "-f", "a", "b"]


def test_docker_memory_parser():
    assert _parse_mem_usage("12.5MiB / 1GiB") == 13_107_200
    assert _parse_mem_usage("1.25GB / 2GB") == 1_250_000_000
    assert _parse_mem_usage("") is None


def test_top_level_exports_provider_types():
    assert shinken.DockerLocalProvider is DockerLocalProvider
    assert shinken.ExternalProvider is ExternalProvider
    assert shinken.SandboxSpec is SandboxSpec
