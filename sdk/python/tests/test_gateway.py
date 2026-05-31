"""Local Action Gateway capability shim (#84)."""

from __future__ import annotations

import pytest

import shinken
from shinken.gateway import check_action


def test_check_action_map():
    assert check_action("click", {"input_automation": True})[:2] == (True, "input_automation")
    allowed, cap, reason = check_action("type_text", {"input_automation": False})
    assert not allowed and cap == "input_automation" and "not granted" in reason
    assert check_action("wait", {})[0] is True  # no capability required
    # an unknown verb is deny-by-default (requires input_automation)
    allowed, cap, _ = check_action("mystery", {})
    assert not allowed and cap == "input_automation"


def test_gateway_denies_ungranted_capability(mock_shinkend):
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=True,
        sandbox_capabilities={"input_automation": False},
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=1, y=1)


def test_gateway_allows_permitted_input(mock_shinkend):
    with shinken.connect(mock_shinkend, enforce_capabilities=True) as env:
        env.click(x=5, y=5)  # input_automation granted by default → passes


def test_enforcement_is_opt_in(mock_shinkend):
    # a non-recording session does not enforce by default: a restricted envelope does
    # not block (back-compat for the plain connect() one-liner)
    with shinken.connect(mock_shinkend, sandbox_capabilities={"input_automation": False}) as env:
        env.click(x=1, y=1)  # must not raise


def test_enforcement_explicit_off_allows_plain_connect(mock_shinkend):
    with shinken.connect(
        mock_shinkend,
        enforce_capabilities=False,
        sandbox_capabilities={"input_automation": False},
    ) as env:
        env.click(x=1, y=1)  # must not raise — enforcement explicitly disabled
