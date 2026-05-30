"""CDP browser observation backend (#79) — pure normalization on CDP fixtures (offline)."""

from __future__ import annotations

import jsonschema

import shinken
from shinken import protocol
from shinken.a11y import coverage_metrics, observe_structured, to_elements
from shinken.cdp import CdpSource, bounds_from_snapshot, parse_ax_tree

# A small but realistic Accessibility.getFullAXTree response: a login form whose fields
# sit under an `ignored` generic wrapper (node 2), plus a disabled button.
AX_NODES = [
    {
        "nodeId": "1",
        "ignored": False,
        "role": {"value": "RootWebArea"},
        "name": {"value": "Login"},
        "childIds": ["2"],
        "backendDOMNodeId": 1,
    },
    {
        "nodeId": "2",
        "ignored": True,  # presentational wrapper — should be hoisted away
        "role": {"value": "generic"},
        "childIds": ["3", "4", "5"],
        "backendDOMNodeId": 2,
    },
    {
        "nodeId": "3",
        "ignored": False,
        "role": {"value": "textbox"},
        "name": {"value": "Username"},
        "backendDOMNodeId": 3,
        "properties": [
            {"name": "focusable", "value": {"value": True}},
            {"name": "editable", "value": {"value": True}},
        ],
    },
    {
        "nodeId": "4",
        "ignored": False,
        "role": {"value": "button"},
        "name": {"value": "Sign in"},
        "backendDOMNodeId": 4,
        "properties": [
            {"name": "focusable", "value": {"value": True}},
            {"name": "disabled", "value": {"value": True}},
        ],
    },
    {
        "nodeId": "5",
        "ignored": False,
        "role": {"value": "link"},
        "name": {"value": "Forgot?"},
        "backendDOMNodeId": 5,
    },
]

BOUNDS = {1: (0, 0, 400, 300), 3: (10, 10, 200, 30), 4: (10, 50, 80, 30), 5: (10, 90, 60, 20)}


def test_parse_ax_tree_hoists_ignored_and_normalizes():
    root = parse_ax_tree(AX_NODES, BOUNDS)
    assert root.role == "RootWebArea" and root.name == "Login"
    # the ignored generic wrapper (node 2) is gone; its children are hoisted to the root
    assert [c.role for c in root.children] == ["textbox", "button", "link"]
    textbox = root.children[0]
    assert textbox.bbox == (10, 10, 200, 30)
    assert "focusable" in textbox.states and "editable" in textbox.states
    assert "enabled" in textbox.states
    # the disabled button reports no "enabled" state
    button = root.children[1]
    assert "enabled" not in button.states and "focusable" in button.states
    # AX/DOM ids are carried as provenance for re-resolution
    assert root.provenance == {"ax_node_id": "1", "backend_dom_node_id": 1}
    assert textbox.provenance["backend_dom_node_id"] == 3


def test_include_ignored_keeps_wrapper():
    root = parse_ax_tree(AX_NODES, BOUNDS, include_ignored=True)
    assert [c.role for c in root.children] == ["generic"]
    assert [c.role for c in root.children[0].children] == ["textbox", "button", "link"]


def test_to_elements_cdp_source_with_provenance_validates_against_aci():
    els = to_elements(parse_ax_tree(AX_NODES, BOUNDS), source="cdp")
    assert len(els) == 4  # root + 3 hoisted fields (ignored wrapper dropped)
    sch = protocol.aci_schema()
    base = {"$defs": sch["$defs"]}
    for e in els:
        assert e["source"] == "cdp" and "provenance" in e
        jsonschema.validate(e, {**base, "$ref": "#/$defs/Element"})  # conforms to ACI Element
    refs = [e["ref"] for e in els]
    assert refs == [f"e{i}" for i in range(4)]  # stable, document-order refs
    assert els[1]["bbox"] == [10, 10, 200, 30]  # textbox (e1)


def test_coverage_metrics_on_cdp_tree():
    m = coverage_metrics(parse_ax_tree(AX_NODES, BOUNDS))
    assert m["nodes"] == 4 and m["roled"] == 4 and m["named"] == 4
    assert m["actionable"] == 3  # textbox, button, link (RootWebArea is not actionable)
    assert m["with_bbox"] == 4 and m["addressable"] == 3


def test_bounds_from_snapshot():
    snapshot = {
        "documents": [
            {
                "nodes": {"backendNodeId": [1, 3, 4, 5]},
                "layout": {
                    "nodeIndex": [0, 1, 2, 3],
                    "bounds": [
                        [0, 0, 400, 300],
                        [10, 10, 200, 30],
                        [10, 50, 80, 30],
                        [10, 90, 60, 20],
                    ],
                },
            }
        ],
        "strings": [],
    }
    assert bounds_from_snapshot(snapshot) == BOUNDS


def test_forest_wraps_in_document_root():
    # two un-parented roots -> wrapped in a synthetic document node
    nodes = [
        {"nodeId": "a", "role": {"value": "button"}, "name": {"value": "A"}},
        {"nodeId": "b", "role": {"value": "link"}, "name": {"value": "B"}},
    ]
    root = parse_ax_tree(nodes)
    assert root.role == "document" and [c.role for c in root.children] == ["button", "link"]


def test_cdp_source_graceful_when_unavailable(monkeypatch):
    src = CdpSource(http_url="http://127.0.0.1:0")

    def boom():
        raise ConnectionRefusedError("no browser listening")

    monkeypatch.setattr(src, "discover_ws_url", boom)
    obs = observe_structured(src)  # must not raise — screenshot path stays usable
    assert obs["available"] is False and obs["elements"] == [] and obs["error"]
    assert src.source_name == "cdp"


class _CdpStub:
    """A CDP-labelled source returning a fixed parsed tree (no live browser)."""

    source_name = "cdp"

    def __init__(self, tree):
        self._tree = tree

    def tree(self):
        return self._tree


def test_sdk_observe_cdp_source_and_resolve(mock_shinkend):
    tree = parse_ax_tree(AX_NODES, BOUNDS)
    with shinken.connect(mock_shinkend) as env:
        obs = env.observe(structured=True, source=_CdpStub(tree))
        assert obs["available"] and obs["node_count"] == 4
        assert all(e["source"] == "cdp" for e in obs["elements"])
        p = env.resolve("e1")  # textbox centre of (10,10,200,30)
        assert (p["x"], p["y"]) == (110, 25)
        assert p["element"]["provenance"]["backend_dom_node_id"] == 3
