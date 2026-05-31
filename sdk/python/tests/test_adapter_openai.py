"""OpenAI Computer Use adapter (#76) — fixture-only, no live API."""

from __future__ import annotations

import base64

import jsonschema
import pytest

import shinken
from shinken import protocol
from shinken.adapters import AdapterError, OpenAIComputerUseAdapter

OAI = OpenAIComputerUseAdapter()


def _valid_aci_action(action: dict) -> dict:
    sch = protocol.aci_schema()
    jsonschema.validate(action, {"$defs": sch["$defs"], "$ref": "#/$defs/Action"})
    return action


def test_single_action_normalizes_to_list():
    out = OAI.to_aci_actions({"action": {"type": "click", "x": 10, "y": 20}})
    assert out == [{"verb": "click", "target": {"kind": "point_px", "x": 10, "y": 20}}]


def test_batched_actions_preserve_order():
    call = {
        "actions": [
            {"type": "move", "x": 1, "y": 2},
            {"type": "click", "x": 1, "y": 2, "button": "left"},
            {"type": "type", "text": "hi"},
        ]
    }
    out = OAI.to_aci_actions(call)
    assert [a["verb"] for a in out] == ["move", "click", "type_text"]
    for a in out:
        _valid_aci_action(a)


@pytest.mark.parametrize(
    "action,expected",
    [
        ({"type": "click", "x": 3, "y": 4, "button": "right"},
         {"verb": "right_click", "target": {"kind": "point_px", "x": 3, "y": 4}}),
        ({"type": "double_click", "x": 5, "y": 6},
         {"verb": "double_click", "target": {"kind": "point_px", "x": 5, "y": 6}}),
        ({"type": "keypress", "keys": ["CTRL", "S"]}, {"verb": "key", "keys": "ctrl+s"}),
        ({"type": "type", "text": "abc"}, {"verb": "type_text", "text": "abc"}),
        ({"type": "screenshot"}, {"verb": "screenshot"}),
        ({"type": "wait", "ms": 500}, {"verb": "wait", "ms": 500}),
        ({"type": "scroll", "x": 0, "y": 0, "scroll_x": 2, "scroll_y": -3},
         {"verb": "scroll", "target": {"kind": "point_px", "x": 0, "y": 0}, "dx": 2, "dy": -3}),
    ],
)
def test_action_mappings(action, expected):
    out = OAI.to_aci_actions({"action": action})
    assert out == [expected]
    _valid_aci_action(out[0])


@pytest.mark.parametrize(
    "bad_call",
    [
        {"action": {"type": "drag", "path": [{"x": 0, "y": 0}]}},  # unsupported
        {"action": {"type": "click", "x": 1, "y": 2, "button": "wheel"}},  # button unsupported
        {"action": {"type": "click"}},  # missing coordinate
        {"action": {"type": "type"}},  # missing text
        {"action": {"type": "keypress"}},  # missing keys
        {"action": {"type": "frobnicate"}},  # unknown type
        {"action": {}},  # no type
        {},  # no action(s)
    ],
)
def test_malformed_or_unsupported_raise_adapter_error(bad_call):
    with pytest.raises(AdapterError):
        OAI.to_aci_actions(bad_call)


def test_safety_checks_map_to_permission_events():
    call = {
        "action": {"type": "click", "x": 1, "y": 1},
        "pending_safety_checks": [
            {"id": "sc_1", "code": "malicious_instructions", "message": "looks risky"}
        ],
    }
    events = OAI.safety_check_events(call)
    assert events[0]["decision"] == "ask" and events[0]["capability"] == "safety_check"
    assert events[0]["code"] == "malicious_instructions"


def test_screenshot_to_computer_call_output():
    png = b"\x89PNG\r\n\x1a\n openai-bytes"
    obs = {"type": "observation", "png": png, "image": {"w": 1024, "h": 768}}
    out = OAI.to_computer_call_output(
        obs, call_id="call_42", acknowledged_safety_checks=[{"id": "sc_1"}]
    )
    assert out["type"] == "computer_call_output" and out["call_id"] == "call_42"
    assert out["output"]["type"] == "computer_screenshot"
    uri = out["output"]["image_url"]
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == png  # screenshot survives the round-trip
    assert out["acknowledged_safety_checks"] == [{"id": "sc_1"}]


def test_run_metadata_records_provider_model_tool():
    meta = OpenAIComputerUseAdapter(model="computer-use-preview").run_metadata()
    assert meta["provider"] == "openai"
    assert meta["tool"] == "computer_use_preview"
    assert meta["model"] == "computer-use-preview"


def test_batch_to_aci_to_action_batch(mock_shinkend):
    # OpenAI batched actions -> canonical ACI batch -> ordered execution results.
    call = {"actions": [{"type": "move", "x": 7, "y": 8}, {"type": "click", "x": 7, "y": 8}]}
    with shinken.connect(mock_shinkend) as env:
        res = env.act_batch(OAI.to_aci_actions(call))
    assert [r["verb"] for r in res["results"]] == ["move", "click"]
    assert all(r["ok"] for r in res["results"])
