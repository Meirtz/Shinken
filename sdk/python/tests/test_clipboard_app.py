"""Desktop verbs (G2+G3): clipboard get/set, launch_app, activate_window.

SDK-facade + gateway-classification tests against the mock runtime — the live X11
paths are covered by the Rust unit tests and `scripts/clipboard_app_smoke.py`.
"""

from __future__ import annotations

import pytest

import shinken
from shinken.gateway import VERB_CAPABILITY, check_action

# ---- facade: wire shapes + reply handling ----


def test_clipboard_set_then_get_roundtrips(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        ack = env.clipboard_set("copy me ✂️")
        assert ack["ok"] is True
        assert env.clipboard_get() == "copy me ✂️"


def test_clipboard_get_before_set_raises_typed_empty(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(RuntimeError, match="clipboard is empty"):
            env.clipboard_get()


def test_launch_app_ships_app_and_args(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        ack = env.launch_app("xclock", ["-geometry", "200x200"])
        assert ack["ok"] is True
        launches = env.query("state")["launches"]
        assert launches == [{"app": "xclock", "args": ["-geometry", "200x200"]}]
        # args=None is omitted from the wire (schema: absent, not null)
        env.launch_app("xterm")
        launches = env.query("state")["launches"]
        assert launches[-1] == {"app": "xterm", "args": None}


def test_activate_window_by_id_and_by_app(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        env.activate_window(42)
        env.activate_window(app="xclock")
        acts = env.query("state")["activations"]
        assert acts == [
            {"window_id": 42, "app": None},
            {"window_id": None, "app": "xclock"},
        ]


def test_activate_window_requires_a_selector(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(ValueError, match="window_id or app"):
            env.activate_window()


def test_desktop_writes_admit_observe(mock_shinkend):
    # act-returns-observation on the G2/G3 writes: the observation comes back in the
    # same exchange (mock answers like the runtime: ack then cause-correlated frame).
    with shinken.connect(mock_shinkend) as env:
        obs = env.clipboard_set("x", observe={"format": "jpeg"})
        assert obs["format"] == "jpeg" and obs["bytes"]
        obs = env.launch_app("xclock", observe=True)
        assert obs["bytes"]
        obs = env.activate_window(42, observe=True)
        assert obs["bytes"]


# ---- gateway classification (#84): clipboard is boundary-ish, default-off ----


def test_gateway_classification_of_desktop_verbs():
    assert VERB_CAPABILITY["clipboard_get"] == "clipboard"
    assert VERB_CAPABILITY["clipboard_set"] == "clipboard"
    assert VERB_CAPABILITY["launch_app"] == "app_launch"
    assert VERB_CAPABILITY["activate_window"] == "input_automation"
    # the default envelope denies the clipboard (a data channel) and grants launch
    caps = dict(shinken.client.DEFAULT_SANDBOX_CAPABILITIES)
    assert check_action("clipboard_get", caps)[0] is False
    assert check_action("clipboard_set", caps)[0] is False
    assert check_action("launch_app", caps)[0] is True
    assert check_action("activate_window", caps)[0] is True


def test_enforcing_session_denies_clipboard_by_default(mock_shinkend):
    with shinken.connect(mock_shinkend, enforce_capabilities=True) as env:
        with pytest.raises(shinken.CapabilityDenied, match="clipboard"):
            env.clipboard_get()
        with pytest.raises(shinken.CapabilityDenied, match="clipboard"):
            env.clipboard_set("secret")
        # nothing reached the runtime
        assert env.query("state")["clipboard"] is None
        # the denials are first-class capability events
        denied = [e for e in env.capability_events if not e["granted"]]
        assert {e["capability"] for e in denied} == {"clipboard"}


def test_enforcing_session_grants_clipboard_when_envelope_does(mock_shinkend):
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities={"clipboard": True},
    ) as env:
        env.clipboard_set("ok now")
        assert env.clipboard_get() == "ok now"


def test_enforcing_session_can_deny_app_launch(mock_shinkend):
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities={"app_launch": False},
    ) as env:
        with pytest.raises(shinken.CapabilityDenied, match="app_launch"):
            env.launch_app("xterm")
        assert env.query("state")["launches"] == []
        env.activate_window(42)  # plain GUI input stays granted


def test_clipboard_ask_tier_pauses_for_approval(mock_shinkend):
    asked: list[tuple[str, str]] = []

    def approve(subject: str, cap: str, reason: str) -> bool:
        asked.append((subject, cap))
        return True

    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities={"clipboard": "ask"},
        on_ask=approve,
    ) as env:
        env.clipboard_set("approved")
        assert env.clipboard_get() == "approved"
    assert asked == [("clipboard_set", "clipboard"), ("clipboard_get", "clipboard")]
