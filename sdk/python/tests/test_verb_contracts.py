"""Verb-specific ACI action contracts (#72) — per-verb required params + result shapes.

Fixes the #23 finding that `Action` only required `verb`, so missing required parameters
were schema-valid. Now each verb declares its required params via JSON-Schema conditionals
and these contract tests assert valid/invalid cases (and the per-verb result shape)."""

from __future__ import annotations

import jsonschema
import pytest

import shinken
from shinken import protocol
from shinken.skn import Replay

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
    ],
)
def test_verbs_without_required_params_are_valid(action):
    _valid(action)


def test_unknown_param_is_rejected():
    _invalid({"verb": "click", "target": PT, "bogus": 1})  # additionalProperties: false


def test_unknown_verb_is_rejected():
    _invalid({"verb": "exec_shell", "text": "rm -rf /"})  # not in the Verb enum


def test_result_shapes_per_verb(mock_shinkend, tmp_path):
    # contract on results: screenshot → observation, action verbs → a recorded action event
    with shinken.connect(mock_shinkend, record=True) as env:
        obs = env.observe()
        assert obs["type"] == "observation" and obs["png"][:8] == b"\x89PNG\r\n\x1a\n"
        env.click(x=1, y=2)
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    assert any(e["kind"] == "action" and e["src"] == "click" for e in rp.events)
    assert any(e["kind"] == "observation" for e in rp.events)
