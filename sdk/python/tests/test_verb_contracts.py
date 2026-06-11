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


def test_clipboard_verbs_contract():
    _valid({"verb": "clipboard_get"})
    _valid({"verb": "clipboard_set", "text": "copy me"})
    _valid({"verb": "clipboard_set", "text": "", "observe": {}})  # mutating → observe ok
    _invalid({"verb": "clipboard_set"})  # missing text
    _invalid({"verb": "clipboard_get", "observe": {}})  # the read is non-mutating


def test_launch_app_contract():
    _valid({"verb": "launch_app", "app": "xclock"})
    _valid({"verb": "launch_app", "app": "/usr/bin/xterm", "args": ["-geometry", "80x24"]})
    _valid({"verb": "launch_app", "app": "xterm", "observe": {"format": "jpeg"}})
    _invalid({"verb": "launch_app"})  # missing app
    _invalid({"verb": "launch_app", "app": ""})  # empty app
    _invalid({"verb": "launch_app", "app": "xclock", "args": "-flag"})  # args is a list
    _invalid({"verb": "click", "target": PT, "app": "xclock"})  # app gated to G3 verbs
    _invalid({"verb": "clipboard_set", "text": "x", "args": ["-x"]})  # args gated to launch


def test_activate_window_contract():
    _valid({"verb": "activate_window", "window_id": 42})
    _valid({"verb": "activate_window", "app": "xclock"})
    _valid({"verb": "activate_window", "window_id": 42, "observe": {}})
    _invalid({"verb": "activate_window"})  # needs window_id or app
    _invalid({"verb": "activate_window", "window_id": -1})  # ids are non-negative
    _invalid({"verb": "launch_app", "app": "x", "window_id": 42})  # gated to activate


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
