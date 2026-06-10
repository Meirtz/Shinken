"""Capability/permission decision recording (#83 / E4): the local gateway records every
grant, ask-resolution, and denial as a first-class, contract-shaped event."""

from __future__ import annotations

import jsonschema
import pytest

import shinken
from shinken.gateway import CAPABILITY_EVENT_SCHEMA, CapabilityDenied


def test_allowed_action_records_an_allow_event(mock_shinkend):
    with shinken.connect(mock_shinkend, enforce_capabilities=True) as env:
        env.act("click", {"kind": "point_px", "x": 1, "y": 2})  # input_automation granted
        events = env.capability_events
    assert len(events) == 1
    e = events[0]
    assert e["decision"] == "allow" and e["granted"] is True and e["subject"] == "click"
    jsonschema.validate(e, CAPABILITY_EVENT_SCHEMA)  # E10 contract shape


def test_denied_action_records_a_deny_event_and_raises(mock_shinkend):
    caps = {"screenshot": False}
    with shinken.connect(
        mock_shinkend, enforce_capabilities=True, sandbox_capabilities=caps
    ) as env:
        with pytest.raises(CapabilityDenied):
            env.screenshot()
        events = env.capability_events
    assert any(e["decision"] == "deny" and e["granted"] is False for e in events)
    for e in events:
        jsonschema.validate(e, CAPABILITY_EVENT_SCHEMA)


def test_ask_resolution_records_grant_and_denial(mock_shinkend):
    caps = {"clipboard": "ask"}
    # on_ask grants → an "ask" event with granted True
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities=caps,
        on_ask=lambda *_a: True,
    ) as env:
        env.gate_capability("clipboard", "read_clipboard")
        granted = env.capability_events
    assert granted[-1]["decision"] == "ask" and granted[-1]["granted"] is True

    # on_ask denies → an "ask" event with granted False, and a raise
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities=caps,
        on_ask=lambda *_a: False,
    ) as env:
        with pytest.raises(CapabilityDenied):
            env.gate_capability("clipboard", "read_clipboard")
        denied = env.capability_events
    assert denied[-1]["decision"] == "ask" and denied[-1]["granted"] is False
    assert denied[-1]["reason"]  # carries why


def test_no_events_when_enforcement_off(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:  # enforce_capabilities not set
        env.act("click", {"kind": "point_px", "x": 1, "y": 2})
        assert env.capability_events == []


def test_capability_events_is_a_copy(mock_shinkend):
    with shinken.connect(mock_shinkend, enforce_capabilities=True) as env:
        env.act("click", {"kind": "point_px", "x": 1, "y": 2})
        snap = env.capability_events
        snap.append({"bogus": True})
        assert len(env.capability_events) == 1  # external mutation doesn't leak in
