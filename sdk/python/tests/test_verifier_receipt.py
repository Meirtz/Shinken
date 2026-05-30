"""First-class verifier_receipt event in .skn (#149)."""

from __future__ import annotations

import jsonschema

import shinken
from shinken.eval import VerifierReceipt, check, click_then_type_task, run_eval
from shinken.skn import Recorder, Replay, skn_schema


def _vr_ref() -> dict:
    return {"$defs": skn_schema()["$defs"], "$ref": "#/$defs/VerifierReceipt"}


def test_recorder_emits_pass_receipt(tmp_path):
    rec = Recorder()
    receipt = VerifierReceipt.from_checks([check("did_thing", True, evidence=1)]).to_dict()
    rec.verifier_receipt(receipt)
    rp = Replay.load(rec.save(str(tmp_path / "r.skn")))
    ev = next(e for e in rp.events if e["kind"] == "verifier_receipt")
    assert ev["src"] == "pass" and ev["payload"]["passed"] is True
    jsonschema.validate(ev["payload"], _vr_ref())  # payload conforms to the schema $def


def test_recorder_emits_fail_receipt(tmp_path):
    rec = Recorder()
    rec.verifier_receipt(VerifierReceipt.from_checks([check("did_thing", False)]).to_dict())
    rp = Replay.load(rec.save(str(tmp_path / "r.skn")))
    ev = next(e for e in rp.events if e["kind"] == "verifier_receipt")
    assert ev["src"] == "fail" and ev["payload"]["passed"] is False
    jsonschema.validate(ev["payload"], _vr_ref())


def test_run_eval_persists_receipt_into_skn(mock_shinkend, tmp_path):
    summary = run_eval(
        click_then_type_task(10, 20, "hi"),
        lambda: shinken.connect(mock_shinkend, record=True),
        n=1,
        out_dir=str(tmp_path),
    )
    rp = Replay.load(summary.results[0].bundle)
    rp.validate()  # the .skn (now incl. the receipt) is schema-valid + pairing-consistent
    vr = [e for e in rp.events if e["kind"] == "verifier_receipt"]
    assert vr and vr[0]["payload"]["passed"] == summary.results[0].passed


def test_record_verifier_receipt_noop_when_not_recording(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:  # not recording
        assert env.record_verifier_receipt({"passed": True, "checks": []}) is False
