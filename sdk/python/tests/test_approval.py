"""Capability approval — risky ('ask') steps pause for approval and are recorded (#7)."""

from __future__ import annotations

import pytest

import shinken
from shinken.gateway import decide_action
from shinken.skn import Replay

_ASK_CAPS = {"input_automation": "ask"}  # make GUI input a risky verb that must be approved


def test_decide_action_three_states():
    assert decide_action("click", {"input_automation": True})[0] == "allow"
    assert decide_action("click", {"input_automation": False})[0] == "deny"
    assert decide_action("click", {"input_automation": "ask"})[0] == "ask"
    assert decide_action("wait", {})[0] == "allow"  # no capability required
    assert decide_action("mystery", {})[0] == "deny"  # unknown verb → deny by default


def test_ask_granted_records_approval_then_acts(mock_shinkend, tmp_path):
    asked: list[tuple] = []

    def approve(verb, cap, reason):
        asked.append((verb, cap))
        return True

    with shinken.connect(
        mock_shinkend,
        record=True,
        enforce_capabilities=True,
        sandbox_capabilities=_ASK_CAPS,
        on_ask=approve,
    ) as env:
        env.click(x=10, y=20)
        path = env.save_replay(str(tmp_path / "r.skn"))
    assert asked == [("click", "input_automation")]  # the handler was consulted

    rp = Replay.load(path)
    ask = next(e for e in rp.events if e["kind"] == "permission" and e["src"] == "ask")
    assert ask["payload"]["resolution"] == "grant" and ask["payload"]["verb"] == "click"
    click = next(e for e in rp.events if e["kind"] == "action" and e["src"] == "click")
    assert ask["seq"] < click["seq"]  # approval recorded before the action runs


def test_ask_denied_blocks_and_records(mock_shinkend, tmp_path):
    with shinken.connect(
        mock_shinkend,
        record=True,
        enforce_capabilities=True,
        sandbox_capabilities=_ASK_CAPS,
        on_ask=lambda *a: False,
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=10, y=20)
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    ask = next(e for e in rp.events if e["kind"] == "permission" and e["src"] == "ask")
    assert ask["payload"]["resolution"] == "deny"
    assert not any(e["kind"] == "action" and e["src"] == "click" for e in rp.events)


def test_ask_defaults_to_deny_without_handler(mock_shinkend, tmp_path):
    with shinken.connect(
        mock_shinkend,
        record=True,
        enforce_capabilities=True,
        sandbox_capabilities=_ASK_CAPS,  # no on_ask handler
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=10, y=20)
        path = env.save_replay(str(tmp_path / "r.skn"))
    rp = Replay.load(path)
    ask = next(e for e in rp.events if e["kind"] == "permission" and e["src"] == "ask")
    assert ask["payload"]["resolution"] == "deny"  # conservative default: no approver → denied
