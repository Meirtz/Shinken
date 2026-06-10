"""Ordered action batch execution (#73)."""

from __future__ import annotations

import jsonschema

import shinken
from shinken import protocol

_PT = {"kind": "point_px", "x": 10, "y": 20}
BATCH = [
    {"verb": "click", "target": _PT},
    {"verb": "type_text", "text": "hi"},
    {"verb": "key", "keys": "ctrl+s"},
]


def test_action_batch_schema_validates_a_sample():
    sch = protocol.aci_schema()
    jsonschema.validate(
        {"batch_id": "b1", "stop_on_error": True, "actions": BATCH},
        {"$defs": sch["$defs"], "$ref": "#/$defs/ActionBatch"},
    )


def test_batch_executes_in_order_with_shared_batch_id(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        res = env.act_batch(BATCH)
    assert res["completed"] is True
    assert [r["verb"] for r in res["results"]] == ["click", "type_text", "key"]
    assert all(r["ok"] for r in res["results"])
    assert res["batch_id"]


def test_stop_on_error_returns_partial_state(mock_shinkend):
    # screenshot capability denied mid-batch -> the gateway stops the batch at index 1
    caps = {"screenshot": False}
    batch = [{"verb": "click", "target": _PT}, {"verb": "screenshot"}, {"verb": "key", "keys": "a"}]
    with shinken.connect(
        mock_shinkend, enforce_capabilities=True, sandbox_capabilities=caps
    ) as env:
        res = env.act_batch(batch)
    assert res["completed"] is False and res["stopped_at"] == 1
    assert res["results"][0]["ok"] is True and res["results"][0]["status"] == "ok"
    assert res["results"][1]["ok"] is False and res["results"][1]["error"]
    # the failing action carries a typed status; failure_kind mirrors it (#56)
    assert res["results"][1]["status"] in ("error", "timeout", "sandbox_died")
    assert res["failure_kind"] == res["results"][1]["status"]
    # the third action never ran but is accounted for as a skipped row
    assert len(res["results"]) == 3
    assert res["results"][2]["status"] == "skipped" and res["results"][2]["ok"] is False


def test_continue_on_error_runs_remaining(mock_shinkend):
    caps = {"screenshot": False}
    batch = [{"verb": "click", "target": _PT}, {"verb": "screenshot"}, {"verb": "key", "keys": "a"}]
    with shinken.connect(
        mock_shinkend, enforce_capabilities=True, sandbox_capabilities=caps
    ) as env:
        res = env.act_batch(batch, stop_on_error=False)
    assert res["completed"] is True and len(res["results"]) == 3
    assert [r["ok"] for r in res["results"]] == [True, False, True]
