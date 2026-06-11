"""Verb-specific ACI action contracts (#72) — per-verb required params + result shapes.

Fixes the #23 finding that `Action` only required `verb`, so missing required parameters
were schema-valid. Now each verb declares its required params via JSON-Schema conditionals
and these contract tests assert valid/invalid cases (and the per-verb result shape)."""

from __future__ import annotations

import jsonschema
import pytest

import shinken
from shinken import protocol

PT = {"kind": "point_px", "x": 1, "y": 2}


def _action_ref() -> dict:
    sch = protocol.aci_schema()
    return {"$defs": sch["$defs"], "$ref": "#/$defs/Action"}


def _valid(action: dict) -> None:
    jsonschema.validate(action, _action_ref())


def _invalid(action: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(action, _action_ref())


@pytest.mark.parametrize("verb", ["click", "double_click", "right_click", "move", "scroll"])
def test_pointer_verbs_require_target(verb):
    _valid({"verb": verb, "target": PT})
    _invalid({"verb": verb})  # missing target is now a contract violation


PT2 = {"kind": "point_px", "x": 300, "y": 200}


def test_drag_requires_target_and_to():
    _valid({"verb": "drag", "target": PT, "to": PT2})
    _valid({"verb": "drag", "target": PT, "to": PT2, "duration_ms": 250, "button": "right"})
    _invalid({"verb": "drag", "target": PT})  # missing `to`
    _invalid({"verb": "drag", "to": PT2})  # missing `target`
    _invalid({"verb": "drag", "target": PT, "to": PT2, "duration_ms": -1})
    _invalid({"verb": "drag", "target": PT, "to": PT2, "button": "wheel"})


def test_mouse_down_up_target_is_optional():
    # the decomposed gesture halves act at the current pointer position without a target
    for verb in ("mouse_down", "mouse_up"):
        _valid({"verb": verb})
        _valid({"verb": verb, "target": PT, "button": "middle"})
        _invalid({"verb": verb, "button": "Left"})  # button names are lowercase, exactly


def test_observe_is_gated_to_mutating_verbs():
    _valid({"verb": "click", "target": PT, "observe": {}})
    _valid(
        {
            "verb": "key",
            "keys": "ctrl+s",
            "observe": {"format": "jpeg", "quality": 80, "max_long_edge": 640, "scope": "screen"},
        }
    )
    _invalid({"verb": "screenshot", "observe": {}})  # non-mutating
    _invalid({"verb": "wait", "ms": 10, "observe": {}})
    _invalid({"verb": "click", "target": PT, "observe": {"fps": 30}})  # unknown observe key


def test_type_text_requires_text():
    _valid({"verb": "type_text", "text": "hi"})
    _invalid({"verb": "type_text"})


def test_key_requires_keys():
    _valid({"verb": "key", "keys": "ctrl+s"})
    _invalid({"verb": "key"})


@pytest.mark.parametrize(
    "action",
    [
        {"verb": "screenshot"},
        {"verb": "screenshot", "scope": "active_window"},
        {"verb": "start_screencast", "fps": 5},
        {"verb": "stop_screencast"},
        {"verb": "wait", "ms": 100},
        {"verb": "observe"},
        {"verb": "observe", "structured": True, "diff": True, "settle_ms": 100},
    ],
)
def test_verbs_without_required_params_are_valid(action):
    _valid(action)


EL = {"kind": "element_ref", "ref": "e2"}


def test_element_verbs_require_target_and_set_value_requires_text():
    _valid({"verb": "invoke_action", "target": EL})
    _valid({"verb": "invoke_action", "target": EL, "text": "click"})
    _valid({"verb": "set_value", "target": EL, "text": "hello"})
    _invalid({"verb": "invoke_action"})  # missing target
    _invalid({"verb": "set_value", "target": EL})  # missing text
    _invalid({"verb": "set_value", "text": "x"})  # missing target


def test_unknown_param_is_rejected():
    _invalid({"verb": "click", "target": PT, "bogus": 1})  # additionalProperties: false


def test_unknown_verb_is_rejected():
    _invalid({"verb": "exec_shell", "text": "rm -rf /"})  # not in the Verb enum


def test_result_shapes_per_verb(mock_shinkend):
    # contract on results: screenshot → observation, action verbs → ack-shaped result
    with shinken.connect(mock_shinkend) as env:
        obs = env.observe()
        assert obs["type"] == "observation" and obs["png"][:8] == b"\x89PNG\r\n\x1a\n"
        ack = env.click(x=1, y=2)
        assert ack["type"] == "ack" and ack["ok"] is True
