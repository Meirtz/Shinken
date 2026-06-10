"""v0.0.1 contract & release-gate tests (#89).

One consolidated suite that fails on schema/runtime drift across the ACI wire
vocabulary and verifier receipts — plus packaged-vs-repo schema parity.
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


def _act(verb, **kw):
    return {"type": "action", "call_id": "c", "action": {"verb": verb, **kw}}


# --- ACI wire contract: the implemented vocabulary must validate (fails on drift) ---


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "hello", "v": 0, "client": {"name": "x", "version": "0"}},
        # the authenticated handshake the Rust runtime requires and the client sends must
        # validate against the schema (the schema previously forbade the `token` field)
        {"type": "hello", "v": 0, "client": {"name": "x", "version": "0"}, "token": "shk_abc"},
        _act("start_screencast", fps=10, max_long_edge=640),
        _act("start_screencast", fps=10, resume_stream="sc-old"),
        _act("start_screencast", fps=10, format="jpeg", quality=80),
        _act("stop_screencast"),
        _act("screenshot", scope="active_window"),
        _act("screenshot", scope="window:0x1f"),
        _act("screenshot", format="jpeg", quality=50),
        _act("screenshot", format="png"),
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8, "scope": "screen"},
        },
        {
            "type": "observation",
            "obs_id": "o",
            "stream": "s",
            "seq": 0,
            "image": {"ref": "x", "w": 8, "h": 8, "scope": "screen", "format": "jpeg"},
        },
    ],
)
def test_aci_wire_vocab_validates(msg):
    protocol.validate(msg)


@pytest.mark.parametrize(
    "msg",
    [
        _act("teleport"),
        _act("screenshot", scope="window:bad"),
        _act("start_screencast", resume_stream=7),  # must be a stream id string
        # codec contract: enum is exactly png|jpeg; quality bounded 1-100 (the runtime
        # REJECTS out-of-range rather than clamping — schema and runtime must agree)
        _act("screenshot", format="webp"),
        _act("screenshot", format="jpg"),
        _act("screenshot", format="jpeg", quality=0),
        _act("screenshot", format="jpeg", quality=101),
    ],
)
def test_aci_invalid_rejected(msg):
    with pytest.raises(jsonschema.ValidationError):
        protocol.validate(msg)


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
    name = "aci.schema.json"
    assert json.loads((repo / name).read_text()) == json.loads((pkg / name).read_text()), (
        f"{name}: packaged copy drifted from repo source-of-truth"
    )
