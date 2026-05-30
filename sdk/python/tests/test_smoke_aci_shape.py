"""Smoke harness emits canonical ACI action dicts + routes through act_batch (#163)."""

from __future__ import annotations

import jsonschema

import shinken
from shinken import protocol
from shinken.skn import Replay
from shinken.smoke import BENIGN_ACTION, _apply


def test_benign_action_is_aci_shaped():
    # canonical: a typed point_px target (not the old flat {verb, x, y}); validates as an Action
    assert BENIGN_ACTION["target"] == {"kind": "point_px", "x": 100, "y": 100}
    assert "x" not in BENIGN_ACTION and "y" not in BENIGN_ACTION
    sch = protocol.aci_schema()
    jsonschema.validate(BENIGN_ACTION, {"$defs": sch["$defs"], "$ref": "#/$defs/Action"})


def test_apply_routes_through_act_batch(mock_shinkend, tmp_path):
    with shinken.connect(mock_shinkend, record=True) as env:
        _apply(env, BENIGN_ACTION)
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    move = next(e for e in rp.events if e["kind"] == "action" and e["src"] == "move")
    assert move["payload"]["target"] == {"kind": "point_px", "x": 100, "y": 100}
    assert "batch_id" in move  # executed via the canonical ordered-batch path (#73)
