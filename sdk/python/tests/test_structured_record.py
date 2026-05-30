"""Structured observation recording into .skn + a11y capability gating (#144, #145)."""

from __future__ import annotations

import pytest

import shinken
from shinken.a11y import A11yNode
from shinken.skn import Replay


class _Src:
    source_name = "atspi"

    def __init__(self, tree):
        self._t = tree

    def tree(self):
        return self._t


def _tree() -> A11yNode:
    return A11yNode(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[
            A11yNode("push button", "OK", (10, 10, 80, 30)),
            A11yNode("entry", "Search", (10, 50, 200, 30)),
        ],
    )


# --- #144: structured observe + observe_diff are recorded into .skn -----------------
def test_structured_observe_recorded_into_skn(mock_shinkend, tmp_path):
    with shinken.connect(mock_shinkend, record=True) as env:
        env.observe(structured=True, source=_Src(_tree()))
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    rp.validate()
    obs = [e for e in rp.events if e["kind"] == "observation"]
    assert obs and obs[-1]["payload"]["tree"] == "full"
    assert obs[-1]["payload"]["node_count"] == 3 and len(obs[-1]["payload"]["elements"]) == 3


def test_observe_diff_recorded_into_skn(mock_shinkend, tmp_path):
    with shinken.connect(mock_shinkend, record=True) as env:
        env.observe_diff(source=_Src(_tree()))
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    rp.validate()
    diffs = [
        e for e in rp.events if e["kind"] == "observation" and e["payload"].get("tree") == "diff"
    ]
    assert diffs and len(diffs[-1]["payload"]["added"]) == 3  # first diff: all added


# --- #145: structured observation is gated on the a11y capability -------------------
def test_a11y_denied_blocks_structured_observe(mock_shinkend, tmp_path):
    with shinken.connect(
        mock_shinkend, record=True, enforce_capabilities=True, sandbox_capabilities={"a11y": False}
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.observe(structured=True, source=_Src(_tree()))
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    denies = [e for e in rp.events if e["kind"] == "permission" and e["src"] == "deny"]
    assert any(e["payload"]["capability"] == "a11y" for e in denies)
    assert not any(e["kind"] == "observation" for e in rp.events)  # denied before capture


def test_a11y_ask_approves_structured_observe(mock_shinkend, tmp_path):
    with shinken.connect(
        mock_shinkend,
        record=True,
        enforce_capabilities=True,
        sandbox_capabilities={"a11y": "ask"},
        on_ask=lambda *a: True,
    ) as env:
        obs = env.observe(structured=True, source=_Src(_tree()))
        assert obs["available"] and obs["node_count"] == 3
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    ask = [e for e in rp.events if e["kind"] == "permission" and e["src"] == "ask"]
    assert ask and ask[0]["payload"]["resolution"] == "grant"
    assert ask[0]["payload"]["capability"] == "a11y"


def test_not_enforcing_allows_structured_observe(mock_shinkend):
    # a11y:false but enforcement off → no gate, capture proceeds
    with shinken.connect(mock_shinkend, sandbox_capabilities={"a11y": False}) as env:
        obs = env.observe(structured=True, source=_Src(_tree()))
        assert obs["available"] and obs["node_count"] == 3
