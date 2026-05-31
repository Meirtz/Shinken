"""Capability approval — risky ('ask') steps pause for approval (#7)."""

from __future__ import annotations

import pytest

import shinken
from shinken.gateway import decide_action

_ASK_CAPS = {"input_automation": "ask"}  # make GUI input a risky verb that must be approved


def test_decide_action_three_states():
    assert decide_action("click", {"input_automation": True})[0] == "allow"
    assert decide_action("click", {"input_automation": False})[0] == "deny"
    assert decide_action("click", {"input_automation": "ask"})[0] == "ask"
    assert decide_action("wait", {})[0] == "allow"  # no capability required
    assert decide_action("mystery", {})[0] == "deny"  # unknown verb → deny by default


def test_ask_granted_calls_approval_then_acts(mock_shinkend):
    asked: list[tuple] = []

    def approve(verb, cap, reason):
        asked.append((verb, cap))
        return True

    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities=_ASK_CAPS,
        on_ask=approve,
    ) as env:
        ack = env.click(x=10, y=20)
    assert asked == [("click", "input_automation")]  # the handler was consulted
    assert ack["type"] == "ack" and ack["ok"] is True


def test_ask_denied_blocks(mock_shinkend):
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities=_ASK_CAPS,
        on_ask=lambda *a: False,
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=10, y=20)


def test_ask_defaults_to_deny_without_handler(mock_shinkend):
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities=_ASK_CAPS,  # no on_ask handler
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=10, y=20)
