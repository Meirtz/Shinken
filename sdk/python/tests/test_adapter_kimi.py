"""Kimi-VL adapter — fixture-only, no live API. Parses the Aguvis pyautogui text DSL with
normalized [0, 1] coordinates into canonical ACI actions."""

from __future__ import annotations

import jsonschema
import pytest

from shinken import protocol
from shinken.adapters import AdapterError, KimiVLAdapter

K = KimiVLAdapter()


def _valid_aci_action(action: dict) -> dict:
    sch = protocol.aci_schema()
    jsonschema.validate(action, {"$defs": sch["$defs"], "$ref": "#/$defs/Action"})
    return action


@pytest.mark.parametrize(
    "call,expected",
    [
        (
            "click(x=0.365, y=0.317)",
            {"verb": "click", "target": {"kind": "point_norm", "x": 0.365, "y": 0.317}},
        ),
        (
            "doubleClick(0.337, 0.648)",
            {"verb": "double_click", "target": {"kind": "point_norm", "x": 0.337, "y": 0.648}},
        ),
        (
            "rightClick(x=0.1, y=0.2)",
            {"verb": "right_click", "target": {"kind": "point_norm", "x": 0.1, "y": 0.2}},
        ),
        (
            "moveTo(0.5, 0.5)",
            {"verb": "move", "target": {"kind": "point_norm", "x": 0.5, "y": 0.5}},
        ),
        ("write('hello world')", {"verb": "type_text", "text": "hello world"}),
        ("pyautogui.write(text='kimi')", {"verb": "type_text", "text": "kimi"}),
        ("press('enter')", {"verb": "key", "keys": "enter"}),
        ("hotkey('ctrl', 'c')", {"verb": "key", "keys": "ctrl+c"}),
    ],
)
def test_supported_actions_map_to_aci(call, expected):
    action = K.to_aci_action(call)
    assert action == expected
    _valid_aci_action(action)  # produced action conforms to the ACI Action schema


def test_pyautogui_prefix_is_stripped():
    assert K.to_aci_action("pyautogui.click(x=0.2, y=0.8)") == {
        "verb": "click",
        "target": {"kind": "point_norm", "x": 0.2, "y": 0.8},
    }


def test_parses_full_thought_action_toolcall_block():
    # Kimi-VL emits Thought/Action/Toolcall; only the Toolcall line is the parseable action,
    # and the Action line is natural language that must NOT be mistaken for the call.
    output = (
        "Thought: The OK button is near the centre.\n"
        "Action: Click the OK button.\n"
        "Toolcall: click(x=0.365, y=0.317)"
    )
    assert K.to_aci_action(output) == {
        "verb": "click",
        "target": {"kind": "point_norm", "x": 0.365, "y": 0.317},
    }


def test_aguvis_action_pyautogui_line():
    # Aguvis form: the parseable line is `Action: pyautogui.<fn>(...)` (no Toolcall line).
    assert K.to_aci_action("Action: pyautogui.click(x=0.6756, y=0.4)") == {
        "verb": "click",
        "target": {"kind": "point_norm", "x": 0.6756, "y": 0.4},
    }


@pytest.mark.parametrize(
    "call,dy",
    [("scroll(-5)", 5), ("scroll(5)", -5), ("scroll(200)", -200), ("vscroll(-3)", 3)],
)
def test_scroll_sign_negated(call, dy):
    # pyautogui: +amount = up; ACI: +dy = down → negated. (No target: Kimi emits no scroll
    # coordinate; a dispatcher supplies one if its backend requires it — #56.)
    assert K.to_aci_action(call) == {"verb": "scroll", "dy": dy}


def test_terminate_and_answer_map_to_done_control_action():
    assert K.to_aci_action("terminate(status='success')") == {"verb": "done"}
    assert K.to_aci_action("terminate()") == {"verb": "done"}
    assert K.to_aci_action("terminate(status='failure')") == {"verb": "done", "status": "fail"}
    assert K.to_aci_action("answer('42')") == {"verb": "done", "answer": "42"}


def test_wait_seconds_to_ms():
    assert K.to_aci_action("wait(2)") == {"verb": "wait", "ms": 2000}
    assert K.to_aci_action("mobile.wait(0.5)") == {"verb": "wait", "ms": 500}


def test_rejects_pixel_or_thousand_scale_coordinates():
    # The single most important correctness guard: Kimi-VL is normalized [0, 1], NOT
    # UI-TARS's [0, 1000] and NOT pixels. A model (mis)emitting those must error loudly
    # rather than silently clamp every click into the top-left corner.
    for bad in ("click(x=365, y=317)", "click(x=1280, y=720)", "click(x=1.5, y=0.2)"):
        with pytest.raises(AdapterError):
            K.to_aci_action(bad)


@pytest.mark.parametrize(
    "bad",
    [
        "dragTo(0.5, 0.5)",  # no drag verb in ACI v0
        "mouseDown(0.1, 0.1)",
        "keyDown('shift')",
        "browser.select_option(0.1, 0.2, 'A')",
        "click()",  # no coordinate
        "press()",  # no key string
        "totally_not_an_action()",
        "",  # empty
        "just some prose with no call",
    ],
)
def test_unsupported_or_malformed_raise_structured(bad):
    with pytest.raises(AdapterError) as ei:
        K.to_aci_action(bad)
    assert ei.value.action and ei.value.reason  # structured, not a bare panic


def test_run_metadata_records_normalized_coordinate_space():
    assert K.run_metadata() == {
        "adapter": "kimi-vl",
        "provider": "moonshot",
        "model": "moonshotai/Kimi-VL-A3B-Thinking",
        "coordinate_space": "point_norm",
    }
