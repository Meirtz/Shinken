"""a11y coverage metrics (#80 / Spike A #2) — pure metrics on synthetic trees (offline)."""

from __future__ import annotations

import jsonschema

import shinken
from shinken import protocol
from shinken.a11y import (
    A11yNode,
    aggregate,
    coverage_metrics,
    iter_nodes,
    observe_structured,
    to_elements,
)


class _StubSource:
    """An A11ySource that returns a fixed tree (or raises) — no real AT-SPI needed."""

    def __init__(self, tree=None, fail=False):
        self._tree = tree
        self._fail = fail

    def tree(self):
        if self._fail:
            raise RuntimeError("no AT-SPI bus")
        return self._tree


def _tree() -> A11yNode:
    return A11yNode(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[
            A11yNode("push button", "OK", (10, 10, 80, 30)),
            A11yNode("entry", "Search", (10, 50, 200, 30)),
            A11yNode("label", "Status", (10, 90, 200, 20)),  # named, not actionable
            A11yNode("push button", "", (10, 120, 80, 30)),  # actionable, unnamed
            A11yNode(
                "panel",
                "",
                None,  # no bbox
                children=[A11yNode("menu item", "File", (0, 0, 40, 20))],
            ),
        ],
    )


def test_iter_nodes_counts_whole_tree():
    assert len(list(iter_nodes(_tree()))) == 7


def test_coverage_metrics():
    m = coverage_metrics(_tree())
    assert m["nodes"] == 7
    assert m["roled"] == 7  # all have a real role
    assert m["named"] == 5  # Win, OK, Search, Status, File
    assert m["actionable"] == 4  # OK, Search(entry), unnamed button, File(menu item)
    assert m["with_bbox"] == 6  # all but the panel
    assert m["addressable"] == 3  # OK, Search, File (unnamed button excluded — no name)
    assert m["max_depth"] == 3  # frame -> panel -> menu item
    assert 0.0 <= m["pct_addressable"] <= 1.0


def test_empty_and_leaf_trees():
    leaf = coverage_metrics(A11yNode("button", "Go", (0, 0, 10, 10)))
    assert leaf["nodes"] == 1 and leaf["pct_addressable"] == 1.0 and leaf["max_depth"] == 1
    bare = coverage_metrics(A11yNode("unknown"))
    assert bare["roled"] == 0 and bare["pct_named"] == 0.0


def test_aggregate_means_across_apps():
    a = coverage_metrics(_tree())
    b = coverage_metrics(A11yNode("button", "Go", (0, 0, 10, 10)))
    agg = aggregate({"app_a": a, "app_b": b, "empty": {"nodes": 0}})
    assert agg["apps_measured"] == 2  # the empty app is excluded
    assert agg["total_nodes"] == a["nodes"] + b["nodes"]
    assert 0.0 <= agg["mean_pct_addressable"] <= 1.0


def test_to_elements_normalizes_and_validates_against_aci():
    els = to_elements(_tree())
    assert len(els) == 7
    sch = protocol.aci_schema()
    base = {"$defs": sch["$defs"]}
    for e in els:
        assert {"ref", "role", "bbox", "source"} <= set(e)
        assert e["source"] == "atspi" and len(e["bbox"]) == 4
        jsonschema.validate(e, {**base, "$ref": "#/$defs/Element"})  # conforms to ACI Element
    refs = [e["ref"] for e in els]
    assert refs == [f"e{i}" for i in range(7)] and len(set(refs)) == 7  # stable, unique
    assert any(e["bbox"] == [0, 0, 0, 0] for e in els)  # bbox-less panel → zero bbox
    assert any(e.get("name") == "OK" for e in els)  # names preserved


def test_observe_structured_available_and_wire_conformant():
    obs = observe_structured(_StubSource(tree=_tree()))
    assert obs["available"] is True and obs["node_count"] == 7 and obs["tree"] == "full"
    assert "capture_ms" in obs
    # the elements form a valid ACI observation message
    protocol.validate(
        {"type": "observation", "obs_id": "o", "tree": "full", "elements": obs["elements"]}
    )


def test_observe_structured_graceful_when_unavailable():
    obs = observe_structured(_StubSource(fail=True))
    assert obs["available"] is False and obs["elements"] == [] and obs["node_count"] == 0
    assert obs["error"] == "RuntimeError"  # reported, not raised


def test_sdk_observe_structured_and_pixels(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        obs = env.observe(structured=True, source=_StubSource(tree=_tree()))
        assert obs["available"] and obs["node_count"] == 7
        pix = env.observe(structured=False)
        assert pix["png"][:8] == b"\x89PNG\r\n\x1a\n" and pix["image"]["scope"] == "screen"
