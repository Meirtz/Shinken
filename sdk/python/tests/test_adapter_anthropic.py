"""Anthropic Computer Use adapter (#75) — fixture-only, no live API."""

from __future__ import annotations

import base64

import jsonschema
import pytest

import shinken
from shinken import protocol
from shinken.adapters import AdapterError, AnthropicComputerUseAdapter

A = AnthropicComputerUseAdapter()


def _valid_aci_action(action: dict) -> dict:
    sch = protocol.aci_schema()
    jsonschema.validate(action, {"$defs": sch["$defs"], "$ref": "#/$defs/Action"})
    return action


@pytest.mark.parametrize(
    "tool_input,expected",
    [
        ({"action": "left_click", "coordinate": [10, 20]},
         {"verb": "click", "target": {"kind": "point_px", "x": 10, "y": 20}}),
        ({"action": "right_click", "coordinate": [1, 2]},
         {"verb": "right_click", "target": {"kind": "point_px", "x": 1, "y": 2}}),
        ({"action": "double_click", "coordinate": [3, 4]},
         {"verb": "double_click", "target": {"kind": "point_px", "x": 3, "y": 4}}),
        ({"action": "mouse_move", "coordinate": [5, 6]},
         {"verb": "move", "target": {"kind": "point_px", "x": 5, "y": 6}}),
        ({"action": "type", "text": "hello"}, {"verb": "type_text", "text": "hello"}),
        ({"action": "key", "text": "ctrl+s"}, {"verb": "key", "keys": "ctrl+s"}),
        ({"action": "screenshot"}, {"verb": "screenshot"}),
        ({"action": "wait", "duration": 1.5}, {"verb": "wait", "ms": 1500}),
    ],
)
def test_supported_actions_map_to_aci(tool_input, expected):
    action = A.to_aci_action(tool_input)
    assert action == expected
    _valid_aci_action(action)  # the produced action conforms to the ACI Action schema


@pytest.mark.parametrize(
    "direction,key,val",
    [("down", "dy", 3), ("up", "dy", -3), ("right", "dx", 3), ("left", "dx", -3)],
)
def test_scroll_directions(direction, key, val):
    action = A.to_aci_action(
        {
            "action": "scroll",
            "coordinate": [0, 0],
            "scroll_direction": direction,
            "scroll_amount": 3,
        }
    )
    assert action["verb"] == "scroll" and action[key] == val
    assert action["target"] == {"kind": "point_px", "x": 0, "y": 0}
    _valid_aci_action(action)


@pytest.mark.parametrize(
    "action", ["left_click_drag", "middle_click", "triple_click", "cursor_position", "hold_key"]
)
def test_unsupported_actions_raise_structured(action):
    with pytest.raises(AdapterError) as ei:
        A.to_aci_action({"action": action, "coordinate": [0, 0]})
    assert ei.value.action == action and ei.value.reason  # structured, not a bare panic


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "left_click"},  # missing coordinate
        {"action": "left_click", "coordinate": [1]},  # malformed coordinate
        {"action": "left_click", "coordinate": [1, "x"]},  # non-numeric
        {"action": "key"},  # missing text
        {"action": "scroll", "coordinate": [0, 0], "scroll_direction": "sideways"},
        {"action": "wait", "duration": -1},  # negative duration
        {},  # no action at all
    ],
)
def test_malformed_inputs_raise(bad):
    with pytest.raises(AdapterError):
        A.to_aci_action(bad)


def test_screenshot_to_tool_result_roundtrips_png():
    png = b"\x89PNG\r\n\x1a\n fake-screenshot-bytes"
    obs = {"type": "observation", "png": png, "image": {"w": 1280, "h": 720, "scope": "screen"}}
    res = A.to_tool_result(obs)
    block = res["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == png  # screenshot survives the round-trip
    assert res["metadata"] == {
        "coordinate_space": "point_px",
        "image_size": {"w": 1280, "h": 720},
        "scope": "screen",
    }


def test_to_tool_result_without_png_raises():
    with pytest.raises(AdapterError):
        A.to_tool_result({"type": "observation", "image": {"w": 1, "h": 1}})


def test_adapter_version_metadata():
    assert A.run_metadata() == {
        "adapter": "anthropic-computer-use",
        "tool_version": A.tool_version,
        "coordinate_space": "point_px",
    }


def test_tool_use_to_aci_action_executes(mock_shinkend):
    # the agent-native path: Anthropic tool_use -> canonical ACI action -> ack.
    with shinken.connect(mock_shinkend) as env:
        action = A.to_aci_action({"action": "left_click", "coordinate": [42, 24]})
        rest = {k: v for k, v in action.items() if k not in ("verb", "target")}
        ack = env.act(action["verb"], target=action.get("target"), **rest)
    assert ack["type"] == "ack" and ack["ok"] is True
