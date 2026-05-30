"""File/artifact transfer store, hashing, refs (#85) — offline, no sandbox."""

from __future__ import annotations

import pytest

from shinken.artifacts import (
    ArtifactRef,
    FileScopeError,
    HashMismatch,
    LocalArtifactStore,
    sha256_bytes,
    sha256_file,
)


def _write(path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


def test_put_get_roundtrip_restores_bytes(tmp_path):
    store = LocalArtifactStore(tmp_path / "sbx")
    payload = b"hello artifact \x00\x01"
    src = _write(tmp_path / "in.bin", payload)
    put = store.put(src, "docs/in.bin")
    assert put.direction == "put" and put.size == len(payload)
    assert put.sha256 == sha256_bytes(payload)

    out = tmp_path / "out" / "in.bin"
    got = store.get("docs/in.bin", out, expect_sha256=put.sha256)
    assert out.read_bytes() == b"hello artifact \x00\x01"
    assert got.direction == "get" and got.sha256 == put.sha256


def test_hash_mismatch_is_detected_and_file_not_written(tmp_path):
    store = LocalArtifactStore(tmp_path / "sbx")
    store.put(_write(tmp_path / "a.txt", b"real"), "a.txt")
    out = tmp_path / "a.copy"
    with pytest.raises(HashMismatch):
        store.get("a.txt", out, expect_sha256="deadbeef" * 8)
    assert not out.exists()  # mismatched fetch must not leave a corrupt local file


def test_content_addressing_same_bytes_same_hash(tmp_path):
    store = LocalArtifactStore(tmp_path / "sbx")
    a = store.put(_write(tmp_path / "x", b"same"), "x")
    b = store.put(_write(tmp_path / "y", b"same"), "nested/y")
    assert a.sha256 == b.sha256  # content-addressed, path-independent
    assert sha256_file(tmp_path / "x") == a.sha256


@pytest.mark.parametrize("bad", ["/etc/passwd", "../escape", "a/../../b"])
def test_scope_escape_rejected(tmp_path, bad):
    store = LocalArtifactStore(tmp_path / "sbx")
    with pytest.raises(FileScopeError):
        store.put(_write(tmp_path / "f", b"x"), bad)
    with pytest.raises(FileScopeError):
        store.get(bad, tmp_path / "out")


def test_missing_artifact_raises(tmp_path):
    store = LocalArtifactStore(tmp_path / "sbx")
    with pytest.raises(FileNotFoundError):
        store.get("nope.txt", tmp_path / "out")


def test_artifact_ref_event_carries_no_bytes():
    ref = ArtifactRef("p/f.bin", "abc123", 10, "session", "put")
    ev = ref.to_event()
    assert ev == {
        "direction": "put",
        "path": "p/f.bin",
        "sha256": "abc123",
        "size": 10,
        "scope": "session",
    }
    assert all(not isinstance(v, bytes | bytearray) for v in ev.values())  # refs, never payloads
