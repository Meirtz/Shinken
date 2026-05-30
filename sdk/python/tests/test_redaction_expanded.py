"""Expanded capture-time redaction beyond action.text + media bytes (#151)."""

from __future__ import annotations

from shinken.skn import Recorder, Replay


def _events(rec: Recorder, tmp_path) -> list[dict]:
    return Replay.load(rec.save(str(tmp_path / "r.skn"))).events


def test_redact_text_covers_keys_and_text(tmp_path):
    rec = Recorder(redact_text=True)
    rec.action("key", {"verb": "key", "keys": "ctrl+shift+secret"}, "c1")
    rec.action("type_text", {"verb": "type_text", "text": "hunter2"}, "c2")
    evs = _events(rec, tmp_path)
    assert next(e for e in evs if e["src"] == "key")["payload"]["keys"] == "[redacted]"
    assert next(e for e in evs if e["src"] == "type_text")["payload"]["text"] == "[redacted]"


def test_redact_text_covers_structured_element_text(tmp_path):
    rec = Recorder(redact_text=True)
    el_in = {
        "ref": "e0",
        "role": "entry",
        "name": "Password",
        "value": "s3cr3t",
        "bbox": [0, 0, 1, 1],
    }
    rec.observation({"tree": "full", "node_count": 1, "elements": [el_in]})
    obs = next(e for e in _events(rec, tmp_path) if e["kind"] == "observation")
    el = obs["payload"]["elements"][0]
    assert el["name"] == "[redacted]" and el["value"] == "[redacted]"
    assert el["role"] == "entry" and el["bbox"] == [0, 0, 1, 1]  # structural fields kept


def test_redact_text_covers_diff_elements(tmp_path):
    rec = Recorder(redact_text=True)
    added = [{"ref": "e0", "role": "label", "name": "SSN 000"}]
    rec.observation({"tree": "diff", "added": added, "removed": [], "changed": []})
    ev = next(e for e in _events(rec, tmp_path) if e["kind"] == "observation")
    assert ev["payload"]["added"][0]["name"] == "[redacted]"


def test_redact_text_covers_file_path(tmp_path):
    rec = Recorder(redact_text=True)
    ref = {
        "direction": "put",
        "path": "/home/alice/secret.txt",
        "sha256": "abc",
        "size": 3,
        "scope": "session",
    }
    rec.file_transfer(ref)
    ev = next(e for e in _events(rec, tmp_path) if e["kind"] == "file_transfer")
    assert ev["payload"]["path"] == "[redacted]" and ev["payload"]["sha256"] == "abc"


def test_redact_text_covers_permission_reason_and_verifier_evidence(tmp_path):
    rec = Recorder(redact_text=True)
    rec.permission({"decision": "deny", "capability": "fs_scope", "reason": "path /home/alice"})
    rec.verifier_receipt(
        {"passed": False, "checks": [{"name": "x", "ok": False, "evidence": "/home/alice/x"}]}
    )
    evs = _events(rec, tmp_path)
    perm = next(e for e in evs if e["kind"] == "permission")
    vr = next(e for e in evs if e["kind"] == "verifier_receipt")
    assert perm["payload"]["reason"] == "[redacted]" and perm["payload"]["capability"] == "fs_scope"
    assert vr["payload"]["checks"][0]["evidence"] == "[redacted]"
    assert vr["payload"]["checks"][0]["name"] == "x"  # check name kept


def test_no_redaction_preserves_fields(tmp_path):
    rec = Recorder()  # redact_text=False
    rec.action("key", {"verb": "key", "keys": "ctrl+s"}, "c1")
    rec.file_transfer({"direction": "put", "path": "/p", "sha256": "a", "size": 1, "scope": "s"})
    evs = _events(rec, tmp_path)
    assert next(e for e in evs if e["src"] == "key")["payload"]["keys"] == "ctrl+s"
    assert next(e for e in evs if e["kind"] == "file_transfer")["payload"]["path"] == "/p"
