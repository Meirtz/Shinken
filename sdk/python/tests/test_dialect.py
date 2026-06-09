"""Agent-native action dialect parser (#74): model output -> canonical ACI actions.

Covers >=20 valid/invalid fixtures, the <actions> wrapper, coordinate normalization,
and that malformed output raises a teaching error instead of being executed."""

from __future__ import annotations

import pytest

from shinken.dialect import DONE, DialectError, parse_actions

# --- valid fixtures: (dialect snippet, expected canonical action) ------------------

VALID = [
    (
        '<click x="640" y="420"/>',
        {"verb": "click", "target": {"kind": "point_px", "x": 640, "y": 420}},
    ),
    (
        '<click x="1" y="2" button="left"/>',
        {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}},
    ),
    (
        '<double_click x="310" y="88"/>',
        {"verb": "double_click", "target": {"kind": "point_px", "x": 310, "y": 88}},
    ),
    (
        '<right_click x="900" y="512"/>',
        {"verb": "right_click", "target": {"kind": "point_px", "x": 900, "y": 512}},
    ),
    ('<move x="5" y="6"/>', {"verb": "move", "target": {"kind": "point_px", "x": 5, "y": 6}}),
    (
        '<click nx="0.5" ny="0.25"/>',
        {"verb": "click", "target": {"kind": "point_norm", "x": 0.5, "y": 0.25}},
    ),
    # a targetless scroll defaults to the screen centre (the wire requires a target)
    (
        '<scroll dy="-480"/>',
        {"verb": "scroll", "dy": -480, "target": {"kind": "point_norm", "x": 0.5, "y": 0.5}},
    ),
    # button='right' on a click maps to the right_click verb (never silently dropped)
    (
        '<click x="1" y="2" button="right"/>',
        {"verb": "right_click", "target": {"kind": "point_px", "x": 1, "y": 2}},
    ),
    (
        '<scroll x="900" y="600" dy="-480" dx="10"/>',
        {
            "verb": "scroll",
            "dy": -480,
            "dx": 10,
            "target": {"kind": "point_px", "x": 900, "y": 600},
        },
    ),
    ('<type_text text="hello world"/>', {"verb": "type_text", "text": "hello world"}),
    ("<type_text text='single quotes'/>", {"verb": "type_text", "text": "single quotes"}),
    ('<key combo="ctrl+s"/>', {"verb": "key", "keys": "ctrl+s"}),
    ("<screenshot/>", {"verb": "screenshot", "scope": "screen"}),
    ('<screenshot scope="active_window"/>', {"verb": "screenshot", "scope": "active_window"}),
    ("<wait/>", {"verb": "wait"}),
    ('<wait ms="500"/>', {"verb": "wait", "ms": 500}),
    ("<done/>", DONE),
    ("<stop/>", DONE),
    (
        '<click x="1.5" y="2.5"/>',
        {"verb": "click", "target": {"kind": "point_px", "x": 1.5, "y": 2.5}},
    ),
]


@pytest.mark.parametrize(("snippet", "expected"), VALID)
def test_valid_dialect_parses_to_typed_aci_action(snippet, expected):
    assert parse_actions(snippet) == [expected]


def test_actions_block_with_multiple_tags_in_order():
    text = """
    <actions>
      <click x="640" y="400"/>
      <type_text text="Q3 report"/>
      <key combo="ctrl+s"/>
      <done/>
    </actions>
    """
    actions = parse_actions(text)
    assert [a["verb"] for a in actions] == ["click", "type_text", "key", "done"]


def test_actions_block_ignores_surrounding_model_prose():
    text = 'I will click the button.\n<actions><click x="3" y="4"/></actions>\nDone.'
    assert parse_actions(text) == [
        {"verb": "click", "target": {"kind": "point_px", "x": 3, "y": 4}}
    ]


# --- invalid fixtures: each must raise DialectError (a teaching error) --------------

INVALID = [
    "<teleport x='1' y='2'/>",  # unknown verb
    "<click x='1' y='2' z='3'/>",  # unknown attribute
    "<click x='1'/>",  # missing y
    "<click y='2'/>",  # missing x
    "<click x='a' y='b'/>",  # non-numeric coords
    "<scroll/>",  # missing required dy
    "<type_text/>",  # missing required text
    "<key/>",  # missing required combo
    "<click x='1' y='2' nx='0.5' ny='0.5'/>",  # px and norm both
    "<click nx='1.5' ny='0.5'/>",  # normalized out of [0,1]
    "<click x='1' y='2' button='diagonal'/>",  # bad button
    "<click x='1' y='2' button='middle'/>",  # middle has no ACI wire verb
    "<click/>",  # pointing verb with no coordinate
    "<scroll dy='nope'/>",  # non-numeric dy
    "<click x=1 y=2/>",  # malformed: unquoted attribute values (silently dropped before)
    "<Click x='1' y='2'/>",  # malformed: uppercase tag name
    "just prose, no tags at all",  # no actions
    "",  # empty
]


@pytest.mark.parametrize("snippet", INVALID)
def test_invalid_dialect_raises_teaching_error(snippet):
    with pytest.raises(DialectError):
        parse_actions(snippet)


def test_non_string_input_rejected():
    with pytest.raises(DialectError):
        parse_actions(None)  # type: ignore[arg-type]


def test_parsed_actions_match_aci_act_batch_shape():
    # The parser's output dicts are exactly what the ACI action path consumes (#74):
    # a `verb` plus verb-appropriate fields — no extra keys, no code.
    (action,) = parse_actions('<click x="10" y="20"/>')
    assert set(action) == {"verb", "target"}
    assert action["target"]["kind"] == "point_px"
