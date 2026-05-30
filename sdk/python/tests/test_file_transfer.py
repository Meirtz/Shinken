"""SDK file transfer: put/get, replay refs, capability enforcement (#85)."""

from __future__ import annotations

import pytest

import shinken
from shinken.artifacts import HashMismatch
from shinken.gateway import CapabilityDenied
from shinken.skn import Replay


def test_put_get_roundtrip_records_refs_without_payloads(mock_shinkend, tmp_path):
    src = tmp_path / "report.csv"
    src.write_bytes(b"col\n1\n2\n")
    with shinken.connect(mock_shinkend, record=True, artifact_root=str(tmp_path / "sbx")) as env:
        put = env.put_file(str(src), "out/report.csv")
        out = tmp_path / "back.csv"
        got = env.get_file("out/report.csv", str(out), expect_sha256=put["sha256"])
        path = env.save_replay(str(tmp_path / "r.skn"))
    assert out.read_bytes() == b"col\n1\n2\n" and got["sha256"] == put["sha256"]

    rp = Replay.load(path)  # validates the bundle against schema/skn.schema.json on load
    transfers = [e for e in rp.events if e["kind"] == "file_transfer"]
    assert [e["src"] for e in transfers] == ["put", "get"]
    # the bundle records refs (hash/path/size/scope) but not the bytes by default
    assert transfers[0]["payload"]["sha256"] == put["sha256"]
    assert "stored" not in transfers[0]["payload"]
    assert rp.media_keys() == []  # nothing content-addressed when archive=False


def test_archive_content_addresses_bytes_for_replay(mock_shinkend, tmp_path):
    src = tmp_path / "blob.bin"
    src.write_bytes(b"\x00\x01\x02\x03reproducible")
    with shinken.connect(mock_shinkend, record=True, artifact_root=str(tmp_path / "sbx")) as env:
        put = env.put_file(str(src), "blob.bin", archive=True)
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    ev = next(e for e in rp.events if e["kind"] == "file_transfer")
    assert ev["payload"]["stored"] is True
    assert rp.media(put["sha256"]) == b"\x00\x01\x02\x03reproducible"  # replay can reproduce it


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
        record=True,
        enforce_capabilities=True,
        sandbox_capabilities=caps,
        artifact_root=str(tmp_path / "sbx"),
    ) as env:
        with pytest.raises(CapabilityDenied):
            env.put_file(str(src), "f")
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    denies = [e for e in rp.events if e["kind"] == "permission" and e["src"] == "deny"]
    assert any(e["payload"]["capability"] == "fs_scope" for e in denies)
