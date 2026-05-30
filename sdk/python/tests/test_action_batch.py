"""Ordered action batch execution with per-action replay events (#73)."""

from __future__ import annotations

import jsonschema

import shinken
from shinken import protocol
from shinken.skn import Replay

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


def test_batch_executes_in_order_with_shared_batch_id(mock_shinkend, tmp_path):
    with shinken.connect(mock_shinkend, record=True) as env:
        res = env.act_batch(BATCH)
        path = env.save_replay(str(tmp_path / "r.skn"))
    assert res["completed"] is True
    assert [r["verb"] for r in res["results"]] == ["click", "type_text", "key"]
    assert all(r["ok"] for r in res["results"])

    rp = Replay.load(path)
    rp.validate()  # schema-valid (incl. batch_id) + observation pairing intact
    actions = [e for e in rp.events if e["kind"] == "action"]
    assert [e["src"] for e in actions] == ["click", "type_text", "key"]  # order preserved
    assert {e["batch_id"] for e in actions} == {res["batch_id"]}  # one shared batch id
    ids = [e["action_id"] for e in actions]
    assert len(set(ids)) == 3  # distinct per-action ids (pairing preserved)


def test_stop_on_error_returns_partial_state(mock_shinkend, tmp_path):
    # screenshot capability denied mid-batch -> the gateway stops the batch at index 1
    caps = {"screenshot": False}
    batch = [{"verb": "click", "target": _PT}, {"verb": "screenshot"}, {"verb": "key", "keys": "a"}]
    with shinken.connect(
        mock_shinkend, record=True, enforce_capabilities=True, sandbox_capabilities=caps
    ) as env:
        res = env.act_batch(batch)
        path = env.save_replay(str(tmp_path / "r.skn"))
    assert res["completed"] is False and res["stopped_at"] == 1
    assert res["results"][0]["ok"] is True
    assert res["results"][1]["ok"] is False and res["results"][1]["error"]
    assert len(res["results"]) == 2  # the third action never ran

    rp = Replay.load(path)
    action_verbs = [e["src"] for e in rp.events if e["kind"] == "action"]
    assert action_verbs == ["click"]  # only the successful action was recorded
    denies = [e for e in rp.events if e["kind"] == "permission" and e["src"] == "deny"]
    assert any(e["payload"]["verb"] == "screenshot" for e in denies)


def test_continue_on_error_runs_remaining(mock_shinkend, tmp_path):
    caps = {"screenshot": False}
    batch = [{"verb": "click", "target": _PT}, {"verb": "screenshot"}, {"verb": "key", "keys": "a"}]
    with shinken.connect(
        mock_shinkend, record=True, enforce_capabilities=True, sandbox_capabilities=caps
    ) as env:
        res = env.act_batch(batch, stop_on_error=False)
        path = env.save_replay(str(tmp_path / "r.skn"))
    assert res["completed"] is True and len(res["results"]) == 3
    assert [r["ok"] for r in res["results"]] == [True, False, True]
    rp = Replay.load(path)
    assert [e["src"] for e in rp.events if e["kind"] == "action"] == ["click", "key"]
