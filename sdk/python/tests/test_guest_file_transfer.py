"""Guest-boundary file transfer (#154): move files through the actual guest filesystem
via `docker cp`, not just the host-local reference store — gated by fs.scope."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import shinken
from shinken.artifacts import FileScopeError, HashMismatch, sha256_bytes
from shinken.providers import DockerLocalProvider
from shinken.providers.base import SandboxHandle
from shinken.providers.docker import DockerGuestTransport, _validate_guest_path


def test_validate_guest_path_requires_absolute_and_no_traversal():
    assert _validate_guest_path("/work/out.txt") == "/work/out.txt"
    with pytest.raises(FileScopeError):
        _validate_guest_path("relative/path")
    with pytest.raises(FileScopeError):
        _validate_guest_path("/work/../etc/passwd")


def test_docker_guest_transport_put_uses_docker_cp(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "shinken.providers.docker._run",
        lambda cmd, timeout=30.0: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    src = tmp_path / "f.txt"
    src.write_bytes(b"payload")
    t = DockerGuestTransport("cont123", docker_bin="docker")
    ref = t.put(src, "/work/f.txt", scope="session")

    assert calls[0] == ["docker", "cp", str(src), "cont123:/work/f.txt"]
    assert ref.path == "/work/f.txt"
    assert ref.direction == "put"
    assert ref.sha256 == sha256_bytes(b"payload")
    assert ref.size == len(b"payload")


def test_docker_guest_transport_get_fetches_and_verifies_hash(monkeypatch, tmp_path):
    def fake_run(cmd, timeout=30.0):
        # simulate `docker cp cont:guest local` by writing the fetched bytes locally
        if ":" in cmd[2]:
            Path(cmd[3]).write_bytes(b"result")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    dst = tmp_path / "got.txt"
    t = DockerGuestTransport("cont123")

    ref = t.get("/work/result.txt", dst, expect_sha256=sha256_bytes(b"result"))
    assert dst.read_bytes() == b"result"
    assert ref.direction == "get"
    assert ref.sha256 == sha256_bytes(b"result")

    with pytest.raises(HashMismatch):
        t.get("/work/result.txt", tmp_path / "x.txt", expect_sha256="deadbeef")


def test_sandbox_routes_file_transfer_through_attached_guest_transport(mock_shinkend, tmp_path):
    # A provider-attached guest transport is used instead of the host-local store, and the
    # transfer is still fs.scope-gated (#154).
    moved: list[tuple] = []

    class FakeTransport:
        def put(self, local_path, guest_path, scope="session"):
            from shinken.artifacts import ArtifactRef, sha256_file

            moved.append(("put", guest_path))
            return ArtifactRef(guest_path, sha256_file(local_path), 3, scope, "put")

        def get(self, guest_path, local_path, *, expect_sha256=None, scope="session"):
            moved.append(("get", guest_path))
            Path(local_path).write_bytes(b"abc")
            from shinken.artifacts import ArtifactRef

            return ArtifactRef(guest_path, sha256_bytes(b"abc"), 3, scope, "get")

    src = tmp_path / "in.txt"
    src.write_bytes(b"abc")
    with shinken.connect(mock_shinkend) as env:
        env._set_guest_transport(FakeTransport())
        ev = env.put_file(str(src), "/work/in.txt")
        assert ev["path"] == "/work/in.txt" and ev["direction"] == "put"
        env.get_file("/work/in.txt", str(tmp_path / "out.txt"))

    assert moved == [("put", "/work/in.txt"), ("get", "/work/in.txt")]


def test_docker_provider_connect_attaches_guest_transport(mock_shinkend):
    handle = SandboxHandle(
        provider="docker-local",
        sandbox_id="shinken-local-abc",
        addr=mock_shinkend,
        metadata={"container_id": "cid-xyz"},
    )
    env = DockerLocalProvider().connect(handle)
    try:
        transport = env._inner._guest_transport
        assert isinstance(transport, DockerGuestTransport)
        assert transport.container_id == "cid-xyz"
    finally:
        env.close()
