"""element_ref resolution + semantic action routing (#78)."""

from __future__ import annotations

import pytest

import shinken
from shinken.a11y import A11yNode


class _Src:
    def __init__(self, tree):
        self._tree = tree

    def tree(self):
        return self._tree


def _tree() -> A11yNode:
    return A11yNode(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[
            A11yNode("push button", "OK", (10, 10, 80, 30)),  # e1 -> centre (50, 25)
            A11yNode("panel", "", None),  # e2 -> zero bbox (unresolvable)
        ],
    )


def test_resolve_element_ref_to_bbox_centre(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        obs = env.observe(structured=True, source=_Src(_tree()))
        assert obs["node_count"] == 3
        p = env.resolve("e1")
        assert (p["x"], p["y"]) == (50, 25)  # centre of (10,10,80,30)
        assert p["element"]["role"] == "push button"
        with pytest.raises(KeyError):
            env.resolve("e99")  # unknown ref
        with pytest.raises(ValueError):
            env.resolve("e2")  # bbox-less panel


def test_act_on_routes_element_ref_to_typed_point(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.observe(structured=True, source=_Src(_tree()))
        ack = env.act_on("e1", "click")
    assert ack["type"] == "ack" and ack["ok"] is True
