"""Guest-side structured observation (M1b): ``observe`` verb preference, element_ref
action routing, the element verbs, and the pre-engine fallback."""

from __future__ import annotations

import pytest

import shinken
from shinken.a11y import A11yNode


class _Src:
    def __init__(self, tree):
        self._tree = tree

    def tree(self):
        return self._tree


def _local_tree() -> A11yNode:
    return A11yNode(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[A11yNode("push button", "OK", (10, 10, 80, 30))],
    )


def test_observe_prefers_the_guest_engine(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.capabilities.structured_observation is True
        obs = env.observe(structured=True, settle_ms=50)
        # The guest reply shape: tree_text for the model + the raw structured array.
        assert obs["type"] == "observation"
        assert obs["tree"] == "full"
        assert obs["revision"] == 1
        assert obs["focus"] == "e3"
        assert obs["node_count"] == 3
        assert "e2 push button" in obs["tree_text"]
        assert obs["elements"][1]["ref"] == "e2"
        assert obs["available"] is True
        # Re-observe: stable ids, bumped revision.
        obs2 = env.observe(structured=True)
        assert obs2["revision"] == 2
        assert obs2["elements"][1]["ref"] == "e2"


def test_observe_diff_uses_the_guest_diff_rendering(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.observe(structured=True)
        diff = env.observe_diff()
        assert diff["tree"] == "diff"
        assert diff["diff_of"] == 1
        assert "~   e3 entry" in diff["tree_text"]
        # the structured array stays the FULL live list (refs keep resolving)
        assert len(diff["elements"]) == 3


def test_act_on_dispatches_element_ref_to_the_guest(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.observe(structured=True)
        ack = env.act_on("e2", "click")
        assert ack["ok"] is True
        # The wire target was the element_ref itself — resolved guest-side.
        clicks = env.query("state")["clicks"]
        assert clicks == [{"verb": "click", "kind": "element_ref", "x": None, "y": None}]


def test_element_verbs_route_ref_and_payload(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.observe(structured=True)
        assert env.invoke_action("e2", "click")["ok"] is True
        assert env.invoke_action("e2")["ok"] is True  # default action
        assert env.set_value("e3", "hello")["ok"] is True
        calls = env.query("state")["element_calls"]
        assert calls == [
            {"verb": "invoke_action", "ref": "e2", "text": "click"},
            {"verb": "invoke_action", "ref": "e2", "text": None},
            {"verb": "set_value", "ref": "e3", "text": "hello"},
        ]


def test_explicit_source_keeps_the_local_path(mock_shinkend):
    # A caller-provided source (tests, CDP adapters) must bypass the guest engine.
    with shinken.connect(mock_shinkend) as env:
        obs = env.observe(structured=True, source=_Src(_local_tree()))
        assert obs["available"] is True
        assert obs["node_count"] == 2  # the local tree, not the mock's 3-node tree
        # …and element actions resolve locally (typed pixel action, no element_ref).
        ack = env.act_on("e1", "click")
        assert ack["ok"] is True
        clicks = env.query("state")["clicks"]
        assert clicks[-1]["kind"] == "point_px"
        assert (clicks[-1]["x"], clicks[-1]["y"]) == (50, 25)


def test_pre_engine_runtime_falls_back_to_local(mock_shinkend_no_structured):
    with shinken.connect(mock_shinkend_no_structured) as env:
        assert env.capabilities.structured_observation is False
        obs = env.observe(structured=True, source=_Src(_local_tree()))
        assert obs["available"] is True and obs["node_count"] == 2
        # observe_diff on the fallback keeps the legacy diff shape
        diff = env.observe_diff(source=_Src(_local_tree()))
        assert set(diff) >= {"added", "removed", "changed", "unchanged", "available"}
        # and the guest-only element verbs surface the runtime's unknown-verb error
        with pytest.raises(RuntimeError, match="unknown verb"):
            env.set_value("e1", "x")
