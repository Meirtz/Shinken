"""String-form XML tool calls -> canonical ACI actions (corpus-driven).

Many CU models emit their tool calls as XML *text*, not structured tool_use JSON. This
corpus covers the wild-type grammars surveyed from public agents (Qwen/Hermes JSON-in-
``<tool_call>``, invoke/parameter blocks, Seed/UI-TARS-2 ``<function=...>`` parameter
elements, attribute/element XML) — well-formed, fenced, malformed-but-recoverable,
multi-action, and junk-rejection samples — plus the property that every parsed wire
action validates against the packaged ACI schema."""

from __future__ import annotations

import jsonschema
import pytest

from shinken import protocol
from shinken.adapters import (
    AdapterError,
    AnthropicComputerUseAdapter,
    KimiVLAdapter,
    OpenAIComputerUseAdapter,
)
from shinken.dialect import DONE, DialectError, looks_like_xml, parse_actions, parse_xml_actions


def _px(x, y):
    return {"kind": "point_px", "x": x, "y": y}


def _norm(x, y):
    return {"kind": "point_norm", "x": x, "y": y}


CENTER = _norm(0.5, 0.5)

# --- the corpus: (id, model-output text, expected canonical ACI actions) -------------

CORPUS = [
    # (a) Qwen / Hermes JSON-in-XML — the OSWorld qwen3vl_agent computer_use contract
    (
        "qwen-click",
        "Action: Click the Firefox icon in the taskbar.\n"
        "<tool_call>\n"
        '{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [135, 742]}}\n'
        "</tool_call>",
        [{"verb": "click", "target": _px(135, 742)}],
    ),
    (
        "qwen-key-array",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "key", '
        '"keys": ["ctrl", "s"]}}</tool_call>',
        [{"verb": "key", "keys": "ctrl+s"}],
    ),
    (
        "qwen-type",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "type", '
        '"text": "Q3 report"}}</tool_call>',
        [{"verb": "type_text", "text": "Q3 report"}],
    ),
    (
        # pyautogui scroll semantics: positive = up; the ACI wire is +dy = down
        "qwen-scroll-pixels",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "scroll", '
        '"coordinate": [640, 400], "pixels": -500}}</tool_call>',
        [{"verb": "scroll", "target": _px(640, 400), "dy": 500}],
    ),
    (
        "qwen-wait-seconds",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "wait", '
        '"time": 1.5}}</tool_call>',
        [{"verb": "wait", "ms": 1500}],
    ),
    (
        "qwen-mouse-move",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "mouse_move", '
        '"coordinate": [10, 20]}}</tool_call>',
        [{"verb": "move", "target": _px(10, 20)}],
    ),
    (
        "qwen-terminate-success",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "terminate", '
        '"status": "success"}}</tool_call>',
        [DONE],
    ),
    (
        "qwen-terminate-failure",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "terminate", '
        '"status": "failure"}}</tool_call>',
        [{"verb": "done", "status": "fail"}],
    ),
    (
        "qwen-fenced",  # markdown fence around the call
        "```xml\n<tool_call>\n"
        '{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [100, 200]}}\n'
        "</tool_call>\n```",
        [{"verb": "click", "target": _px(100, 200)}],
    ),
    (
        "qwen-unclosed",  # truncated generation: no closing </tool_call>
        '<tool_call>\n{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [88, 99]}}',
        [{"verb": "click", "target": _px(88, 99)}],
    ),
    (
        "qwen-trailing-comma",  # malformed-but-recoverable JSON
        '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [5, 6],}}</tool_call>',
        [{"verb": "click", "target": _px(5, 6)}],
    ),
    (
        "qwen-truncated-json",  # missing closing braces
        '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [7, 8]',
        [{"verb": "click", "target": _px(7, 8)}],
    ),
    (
        "qwen-double-encoded-arguments",
        '<tool_call>{"name": "computer_use", "arguments": '
        '"{\\"action\\": \\"left_click\\", \\"coordinate\\": [1, 2]}"}</tool_call>',
        [{"verb": "click", "target": _px(1, 2)}],
    ),
    (
        "qwen-two-calls-ordered",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [1, 2]}}</tool_call>\n'
        '<tool_call>{"name": "computer_use", "arguments": {"action": "key", '
        '"keys": ["enter"]}}</tool_call>',
        [{"verb": "click", "target": _px(1, 2)}, {"verb": "key", "keys": "enter"}],
    ),
    (
        "json-bare-verb-name",  # no computer_use wrapper: the name IS the verb
        '<tool_call>{"name": "click", "arguments": {"x": 300, "y": 400}}</tool_call>',
        [{"verb": "click", "target": _px(300, 400)}],
    ),
    (
        "json-bare-action-object",  # {"action": ...} with no name/arguments envelope
        '<tool_call>{"action": "double_click", "coordinate": [30, 40]}</tool_call>',
        [{"verb": "double_click", "target": _px(30, 40)}],
    ),
    # (d) invoke/parameter blocks (Anthropic-style text-form function calls)
    (
        "invoke-direct-verb",
        "I'll click the save button now.\n\n"
        '<invoke name="click">\n<parameter name="x">100</parameter>\n'
        '<parameter name="y">200</parameter>\n</invoke>',
        [{"verb": "click", "target": _px(100, 200)}],
    ),
    (
        "invoke-computer-wrapper",
        '<invoke name="computer">\n<parameter name="action">left_click</parameter>\n'
        '<parameter name="coordinate">[640, 400]</parameter>\n</invoke>',
        [{"verb": "click", "target": _px(640, 400)}],
    ),
    (
        # Anthropic scroll vocabulary: wheel clicks x 100px, direction down = +dy
        "invoke-anthropic-scroll",
        '<invoke name="computer">\n<parameter name="action">scroll</parameter>\n'
        '<parameter name="coordinate">[640, 400]</parameter>\n'
        '<parameter name="scroll_direction">down</parameter>\n'
        '<parameter name="scroll_amount">3</parameter>\n</invoke>',
        [{"verb": "scroll", "target": _px(640, 400), "dy": 300}],
    ),
    (
        "invoke-anthropic-key-text",
        '<invoke name="computer"><parameter name="action">key</parameter>'
        '<parameter name="text">ctrl+shift+p</parameter></invoke>',
        [{"verb": "key", "keys": "ctrl+shift+p"}],
    ),
    (
        "invoke-type-wrapper",
        '<invoke name="computer"><parameter name="action">type</parameter>'
        '<parameter name="text">hello world</parameter></invoke>',
        [{"verb": "type_text", "text": "hello world"}],
    ),
    (
        "invoke-unclosed-screenshot",
        '<invoke name="screenshot">',
        [{"verb": "screenshot"}],
    ),
    # (b/c) Seed / UI-TARS-2 / qwen3.5-4b function-parameter element XML
    (
        "seed-function-eq-point",
        "<tool_call>\n<function=click>\n"
        "<parameter=point><point>400 300</point></parameter>\n</function>\n</tool_call>",
        [{"verb": "click", "target": _px(400, 300)}],
    ),
    (
        "seed-function-name-scroll",
        "<seed:tool_call>\n<function name='scroll'>\n"
        "<parameter name='direction'>up</parameter>\n"
        "<parameter name='point'>500 500</parameter>\n</function>\n</seed:tool_call>",
        [{"verb": "scroll", "target": _px(500, 500), "dy": -300}],
    ),
    (
        "seed-function-type-content",
        "<tool_call><function=type><parameter=content>shrimp and crab recipes"
        "</parameter></function></tool_call>",
        [{"verb": "type_text", "text": "shrimp and crab recipes"}],
    ),
    (
        "function-standalone-hotkey",
        '<function=hotkey><parameter=keys>["ctrl", "t"]</parameter></function>',
        [{"verb": "key", "keys": "ctrl+t"}],
    ),
    (
        "seed-think-noise",  # prose tags around the call are markup, not actions
        "<think>I should scroll up. A good point is <point>500 500</point>.</think>\n"
        "<seed:tool_call>\n<function name='click'>\n"
        "<parameter name='point'>500 500</parameter>\n</function>\n</seed:tool_call>",
        [{"verb": "click", "target": _px(500, 500)}],
    ),
    # (b) attribute / element XML
    (
        "action-element-param",
        '<action name="click"><param name="x">100</param><param name="y">200</param></action>',
        [{"verb": "click", "target": _px(100, 200)}],
    ),
    (
        "action-element-coordinate",
        '<action name="double_click"><parameter name="coordinate">[55, 66]</parameter></action>',
        [{"verb": "double_click", "target": _px(55, 66)}],
    ),
    (
        "action-element-self-closing",
        '<action name="screenshot"/>',
        [{"verb": "screenshot"}],
    ),
    (
        "bare-alias-tag",
        '<left_click x="100" y="200"/>',
        [{"verb": "click", "target": _px(100, 200)}],
    ),
    (
        "bare-unquoted-attrs",  # malformed XML, recoverable
        "<mouse_move x=640 y=360>",
        [{"verb": "move", "target": _px(640, 360)}],
    ),
    (
        "bare-normalized-floats",  # fractional [0,1] pair -> point_norm
        '<click x="0.5" y="0.25"/>',
        [{"verb": "click", "target": _norm(0.5, 0.25)}],
    ),
    (
        "bare-right-button",
        '<click x="9" y="9" button="right"/>',
        [{"verb": "right_click", "target": _px(9, 9)}],
    ),
    (
        "scroll-openai-deltas",  # OpenAI pixel-denominated scroll_x/scroll_y pass through
        '<action name="scroll"><param name="x">10</param><param name="y">20</param>'
        '<param name="scroll_y">120</param></action>',
        [{"verb": "scroll", "target": _px(10, 20), "dy": 120}],
    ),
    (
        "scroll-direction-default-target",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "scroll", '
        '"direction": "down"}}</tool_call>',
        [{"verb": "scroll", "target": CENTER, "dy": 300}],
    ),
    (
        "keypress-openai-style",
        '<action name="keypress"><param name="keys">["ctrl", "l"]</param></action>',
        [{"verb": "key", "keys": "ctrl+l"}],
    ),
    (
        "start-box-centre",  # UI-TARS-style box -> centre point
        '<invoke name="click"><parameter name="start_box">(100, 200, 300, 400)'
        "</parameter></invoke>",
        [{"verb": "click", "target": _px(200, 300)}],
    ),
    (
        "answer-block",
        "<think>Simple arithmetic.</think>\n<answer>\nThe answer is 2.\n</answer>",
        [{"verb": "done", "answer": "The answer is 2."}],
    ),
    (
        "multi-grammar-ordered",
        "I'll save the file.\n"
        '<invoke name="click"><parameter name="x">10</parameter>'
        '<parameter name="y">20</parameter></invoke>\n'
        'then press <key combo="ctrl+s"/>\n'
        '<tool_call>{"name": "computer_use", "arguments": {"action": "terminate", '
        '"status": "success"}}</tool_call>',
        [
            {"verb": "click", "target": _px(10, 20)},
            {"verb": "key", "keys": "ctrl+s"},
            DONE,
        ],
    ),
]

# --- junk / unsupported: every sample must raise a typed teaching error ---------------

REJECTS = [
    ("not-json", "<tool_call>this is not json</tool_call>"),
    ("unknown-verb", '<tool_call>{"name": "teleport", "arguments": {"x": 1, "y": 2}}</tool_call>'),
    ("no-name", '<tool_call>{"no_name": true}</tool_call>'),
    (
        "middle-click-unsupported",
        '<invoke name="computer"><parameter name="action">middle_click</parameter>'
        '<parameter name="coordinate">[1, 2]</parameter></invoke>',
    ),
    (
        "drag-unsupported",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click_drag", '
        '"coordinate": [1, 2]}}</tool_call>',
    ),
    ("action-shaped-unknown-tag", '<warp x="1" y="2"/>'),
    ("pointing-verb-without-target", "<click/>"),
    (
        "missing-y",
        '<invoke name="click"><parameter name="x">100</parameter></invoke>',
    ),
    ("plain-prose", "Sure, I will click that button for you."),
    ("empty", ""),
    ("fenced-junk", "```\nhello\n```"),
    (
        "non-numeric-coordinate",
        '<action name="click"><param name="x">abc</param><param name="y">2</param></action>',
    ),
    (
        "unknown-argument",
        '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [1, 2], "zoom": 3}}</tool_call>',
    ),
    ("scroll-without-magnitude", '<scroll x="1" y="2"/>'),
    (
        "wrapper-without-action",
        '<tool_call>{"name": "computer_use", "arguments": {"coordinate": [1, 2]}}</tool_call>',
    ),
    ("malformed-container", "<tool_call id='1'>{}</tool_call>"),
]


@pytest.mark.parametrize(("sample_id", "text", "expected"), CORPUS, ids=[c[0] for c in CORPUS])
def test_corpus_parses_to_canonical_aci_actions(sample_id, text, expected):
    assert parse_xml_actions(text) == expected


@pytest.mark.parametrize(("sample_id", "text"), REJECTS, ids=[r[0] for r in REJECTS])
def test_junk_raises_typed_teaching_error(sample_id, text):
    with pytest.raises(DialectError):
        parse_xml_actions(text)


def test_unknown_verb_error_carries_offending_snippet():
    with pytest.raises(DialectError, match="teleport"):
        parse_xml_actions('<tool_call>{"name": "teleport", "arguments": {"x": 1}}</tool_call>')


def test_unknown_bare_tag_never_silently_dropped_in_partial_plan():
    # the well-formed sibling must NOT execute when an action-shaped tag is unknown
    with pytest.raises(DialectError, match="warp"):
        parse_xml_actions('<left_click x="1" y="2"/>\n<warp x="3" y="4"/>')


# --- property: every parsed wire action validates against the packaged ACI schema -----


def _action_ref() -> dict:
    sch = protocol.aci_schema()
    return {"$defs": sch["$defs"], "$ref": "#/$defs/Action"}


@pytest.mark.parametrize(("sample_id", "text", "expected"), CORPUS, ids=[c[0] for c in CORPUS])
def test_every_parsed_action_validates_against_packaged_schema(sample_id, text, expected):
    for action in parse_xml_actions(text):
        if action.get("verb") == "done":
            continue  # DONE is an Operator-loop control action, not an ACI wire verb
        jsonschema.validate(action, _action_ref())


# --- parse_actions(format=...) integration --------------------------------------------


def test_auto_detection_routes_xml_markers_to_xml_parser():
    text = (
        'Action: click it.\n<tool_call>{"name": "computer_use", "arguments": '
        '{"action": "left_click", "coordinate": [3, 4]}}</tool_call>'
    )
    assert looks_like_xml(text)
    assert parse_actions(text) == parse_xml_actions(text)


def test_auto_detection_keeps_native_dialect_unchanged():
    assert parse_actions('<click x="3" y="4"/>') == [{"verb": "click", "target": _px(3, 4)}]
    # fractional px coords keep the dialect semantics (point_px) under auto
    assert parse_actions('<click x="0.5" y="0.25"/>') == [
        {"verb": "click", "target": _px(0.5, 0.25)}
    ]


def test_auto_detection_routes_alias_bare_tags_to_xml():
    assert parse_actions('<left_click x="3" y="4"/>') == [{"verb": "click", "target": _px(3, 4)}]


def test_format_dialect_forces_native_grammar():
    with pytest.raises(DialectError):
        parse_actions(
            '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click", '
            '"coordinate": [1, 2]}}</tool_call>',
            format="dialect",
        )


def test_format_xml_forces_xml_grammar():
    assert parse_actions('<left_click x="1" y="2"/>', format="xml") == [
        {"verb": "click", "target": _px(1, 2)}
    ]


def test_unknown_format_rejected():
    with pytest.raises(DialectError, match="format"):
        parse_actions('<click x="1" y="2"/>', format="bogus")


def test_non_string_input_rejected():
    with pytest.raises(DialectError):
        parse_xml_actions(None)  # type: ignore[arg-type]


# --- adapter .from_text integration ----------------------------------------------------


def test_anthropic_adapter_from_text_invoke_block():
    actions = AnthropicComputerUseAdapter().from_text(
        '<invoke name="computer"><parameter name="action">left_click</parameter>'
        '<parameter name="coordinate">[640, 400]</parameter></invoke>'
    )
    assert actions == [{"verb": "click", "target": _px(640, 400)}]


def test_openai_adapter_from_text_action_element():
    actions = OpenAIComputerUseAdapter().from_text(
        '<action name="keypress"><param name="keys">["ctrl", "l"]</param></action>'
    )
    assert actions == [{"verb": "key", "keys": "ctrl+l"}]


def test_kimi_adapter_from_text_keeps_dsl_path():
    # plain-text Kimi-VL/Aguvis DSL is NOT re-parsed as XML — no duplication
    actions = KimiVLAdapter().from_text("Toolcall: click(x=0.365, y=0.317)")
    assert actions == [{"verb": "click", "target": _norm(0.365, 0.317)}]


def test_kimi_adapter_from_text_routes_xml():
    actions = KimiVLAdapter().from_text(
        '<tool_call>{"name": "computer_use", "arguments": {"action": "left_click", '
        '"coordinate": [1, 2]}}</tool_call>'
    )
    assert actions == [{"verb": "click", "target": _px(1, 2)}]


def test_from_text_junk_raises_adapter_error():
    for adapter in (AnthropicComputerUseAdapter(), OpenAIComputerUseAdapter()):
        with pytest.raises(AdapterError):
            adapter.from_text('<tool_call>{"name": "teleport", "arguments": {}}</tool_call>')
