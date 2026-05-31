"""SDK file transfer: put/get, capability enforcement (#85)."""

from __future__ import annotations

import pytest

import shinken
from shinken.artifacts import HashMismatch
from shinken.gateway import CapabilityDenied


def test_put_get_roundtrip_returns_refs(mock_shinkend, tmp_path):
    src = tmp_path / "report.csv"
    src.write_bytes(b"col\n1\n2\n")
    with shinken.connect(mock_shinkend, artifact_root=str(tmp_path / "sbx")) as env:
        put = env.put_file(str(src), "out/report.csv")
        out = tmp_path / "back.csv"
        got = env.get_file("out/report.csv", str(out), expect_sha256=put["sha256"])
    assert out.read_bytes() == b"col\n1\n2\n" and got["sha256"] == put["sha256"]
    assert put["path"] == "out/report.csv"
    assert put["direction"] == "put"
    assert got["direction"] == "get"


def test_get_file_hash_mismatch_raises(mock_shinkend, tmp_path):
    src = tmp_path / "f"
    src.write_bytes(b"genuine")
    with shinken.connect(mock_shinkend, artifact_root=str(tmp_path / "sbx")) as env:
        env.put_file(str(src), "f")
        with pytest.raises(HashMismatch):
            env.get_file("f", str(tmp_path / "out"), expect_sha256="00" * 32)


def test_enforce_denies_transfer_without_fs_scope(mock_shinkend, tmp_path):
    src = tmp_path / "f"
    src.write_bytes(b"x")
    caps = {"fs_scope": "none"}  # filesystem access not granted
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities=caps,
        artifact_root=str(tmp_path / "sbx"),
    ) as env:
        with pytest.raises(CapabilityDenied):
            env.put_file(str(src), "f")
