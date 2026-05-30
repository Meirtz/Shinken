"""a11y tree-diff — send only what changed between turns (#4 / D3 bandwidth)."""

from __future__ import annotations

import shinken
from shinken.a11y import A11yNode, diff_elements, diff_size, element_key


def _el(ref, role, name, bbox, prov=None):
    e = {"ref": ref, "role": role, "name": name, "bbox": bbox, "source": "cdp"}
    if prov:
        e["provenance"] = prov
    return e


def test_element_key_prefers_provenance_then_role_name():
    assert element_key(_el("e0", "b", "OK", [0, 0, 1, 1], {"backend_dom_node_id": 7})) == ("dom", 7)
    assert element_key(_el("e1", "b", "OK", [0, 0, 1, 1], {"ax_node_id": "3"})) == ("ax", "3")
    assert element_key(_el("e2", "button", "OK", [0, 0, 1, 1])) == ("rn", "button", "OK")


def test_diff_unchanged_changed_added_removed():
    a = _el("e0", "button", "OK", [0, 0, 80, 30], {"backend_dom_node_id": 1})
    b = _el("e1", "textbox", "User", [0, 40, 200, 30], {"backend_dom_node_id": 2})
    prev = [a, b]

    # identical capture → nothing in the diff
    same = diff_elements(prev, [dict(a), dict(b)])
    assert same["added"] == [] and same["removed"] == [] and same["changed"] == []
    assert same["unchanged"] == 2

    # b's bbox changed under the same backend id → changed (precise, stable identity)
    b2 = _el("e9", "textbox", "User", [0, 40, 200, 40], {"backend_dom_node_id": 2})
    moved = diff_elements(prev, [dict(a), b2])
    assert moved["changed"] == [b2] and moved["added"] == [] and moved["removed"] == []

    # add c, remove a
    c = _el("e3", "link", "More", [0, 80, 40, 20], {"backend_dom_node_id": 3})
    delta = diff_elements(prev, [dict(b), c])
    assert delta["added"] == [c] and delta["removed"] == [a] and delta["changed"] == []


def test_first_diff_is_all_added():
    curr = [_el("e0", "button", "OK", [0, 0, 1, 1], {"backend_dom_node_id": 1})]
    d = diff_elements([], curr)
    assert d["added"] == curr and d["unchanged"] == 0 and d["prev_count"] == 0


def test_diff_size_is_smaller_for_a_small_change():
    full = [
        _el(f"e{i}", "button", f"b{i}", [i, i, 10, 10], {"backend_dom_node_id": i})
        for i in range(20)
    ]
    changed = [dict(e) for e in full]
    changed[0] = _el("e0", "button", "b0", [0, 0, 99, 99], {"backend_dom_node_id": 0})
    sz = diff_size(diff_elements(full, changed), changed)
    assert sz["diff_bytes"] < sz["full_bytes"] and 0 < sz["ratio"] < 1  # bandwidth saved


class _Src:
    source_name = "atspi"

    def __init__(self, tree):
        self._t = tree

    def tree(self):
        return self._t


class _Fail:
    source_name = "atspi"

    def tree(self):
        raise RuntimeError("no AT-SPI bus")


def _tree(btn_bbox=(10, 10, 80, 30)) -> A11yNode:
    return A11yNode(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[
            A11yNode("push button", "OK", btn_bbox),
            A11yNode("entry", "Search", (10, 50, 200, 30)),
        ],
    )


def test_sdk_observe_diff_first_all_added_then_unchanged_then_changed(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        d1 = env.observe_diff(source=_Src(_tree()))
        # first capture: frame + button + entry, no baseline → all added
        assert d1["available"] and len(d1["added"]) == 3 and d1["unchanged"] == 0
        assert d1["size"]["full_bytes"] > 0

        d2 = env.observe_diff(source=_Src(_tree()))  # identical → all unchanged
        assert d2["added"] == [] and d2["changed"] == [] and d2["unchanged"] == 3

        d3 = env.observe_diff(source=_Src(_tree(btn_bbox=(10, 10, 80, 40))))  # button resized
        assert d3["unchanged"] == 2 and len(d3["changed"]) == 1
        assert d3["added"] == [] and d3["removed"] == []
        assert d3["size"]["diff_bytes"] < d3["size"]["full_bytes"]  # only the change is sent

        # element_ref map stays usable after a diff capture
        assert env.resolve("e1")["element"]["role"] == "push button"


def test_observe_diff_graceful_when_unavailable(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        d = env.observe_diff(source=_Fail())
        assert d["available"] is False and d["added"] == [] and d["error"] == "RuntimeError"
