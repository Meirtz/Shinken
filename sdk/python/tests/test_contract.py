"""v0.0.1 contract & release-gate tests (#89).

One consolidated suite that fails on schema/runtime drift across the ACI wire
vocabulary, the `.skn` replay bundle (manifest + every event kind), capability /
permission events, and verifier receipts — plus packaged-vs-repo schema parity.
The human-facing gate is `docs/release-gate.md`. CI runs this as the named
`contract` job.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from shinken import protocol
from shinken.eval import RECEIPT_SCHEMA, VerifierReceipt, check
from shinken.skn import Recorder, _validate_bundle, skn_schema


def _act(verb, **kw):
    return {"type": "action", "call_id": "c", "action": {"verb": verb, **kw}}


def _ev(seq, kind, src, payload=None, action_id=None):
    e = {"seq": seq, "dt": seq / 10, "kind": kind, "src": src, "payload": payload or {}}
    if action_id is not None:
        e["action_id"] = action_id
    return e


# --- ACI wire contract: the implemented vocabulary must validate (fails on drift) ---


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "hello", "v": 0, "client": {"name": "x", "version": "0"}},
        _act("start_screencast", fps=10, max_long_edge=640),
        _act("stop_screencast"),
        _act("screenshot", scope="active_window"),
        _act("screenshot", scope="window:0x1f"),
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8, "scope": "screen"},
        },
    ],
)
def test_aci_wire_vocab_validates(msg):
    protocol.validate(msg)


@pytest.mark.parametrize(
    "msg",
    [_act("teleport"), _act("screenshot", scope="window:bad")],
)
def test_aci_invalid_rejected(msg):
    with pytest.raises(jsonschema.ValidationError):
        protocol.validate(msg)


# --- .skn contract: manifest (capabilities + redaction) + every event kind ---


def test_skn_manifest_and_events_validate():
    sch = skn_schema()
    base = {"$defs": sch["$defs"]}
    manifest = {
        "skn_version": 0,
        "session_id": "s",
        "run_id": "r",
        "t0_wall": "2026-05-31T00:00:00+00:00",
        "platform": "linux",
        "capabilities": {"input_automation": True, "egress": False},
        "redaction": {"media": True, "text": False},
        "channels": ["meta", "action"],
    }
    jsonschema.validate(manifest, {**base, "$ref": "#/$defs/Manifest"})
    for ev in [
        _ev(0, "meta", "capability_envelope"),
        _ev(1, "action", "click", action_id="c1", payload={"verb": "click"}),
        _ev(2, "observation", "image", action_id="c1"),
        _ev(3, "permission", "deny", payload={"capability": "egress"}),
        # per-kind payload contracts (#150): well-defined kinds bind strictly
        _ev(4, "file_transfer", "put", payload={
            "direction": "put", "sha256": "ab", "size": 3, "scope": "session"}),
        _ev(5, "verifier_receipt", "pass", payload={
            "passed": True, "checks": [{"name": "did_x", "ok": True}]}),
    ]:
        jsonschema.validate(ev, {**base, "$ref": "#/$defs/Event"})


def test_recorder_bundle_is_self_consistent():
    # a real recorder bundle validates (manifest + events) — runtime↔schema parity
    rec = Recorder(platform="linux", capabilities={"egress": False})
    rec.capability_envelope()
    rec.action("click", {"verb": "click"}, "c1")
    rec.observation(
        {"image": {"w": 1, "h": 1, "scope": "screen"}}, png=b"\x89PNG\r\n\x1a\n", action_id="c1"
    )
    _validate_bundle(rec.manifest(), rec.events)  # raises on drift


# --- verifier receipt contract ---


def test_verifier_receipt_contract():
    r = VerifierReceipt.from_checks([check("c", True, {"e": 1})])
    jsonschema.validate(r.to_dict(), RECEIPT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"passed": "no", "checks": []}, RECEIPT_SCHEMA)


# --- packaged schemas == repo source-of-truth (no wheel/repo drift) ---


def test_packaged_schemas_match_repo():
    repo = Path(__file__).resolve().parents[3] / "schema"
    if not repo.exists():  # running from a wheel install — repo source not present
        pytest.skip("repo schema/ not present")
    pkg = Path(__file__).resolve().parents[1] / "src" / "shinken" / "schemas"
    for name in ("aci.schema.json", "skn.schema.json"):
        assert json.loads((repo / name).read_text()) == json.loads((pkg / name).read_text()), (
            f"{name}: packaged copy drifted from repo source-of-truth"
        )
