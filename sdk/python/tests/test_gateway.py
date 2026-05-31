"""Local Action Gateway capability shim (#84)."""

from __future__ import annotations

import pytest

import shinken
from shinken.gateway import check_action
from shinken.skn import Replay


def test_check_action_map():
    assert check_action("click", {"input_automation": True})[:2] == (True, "input_automation")
    allowed, cap, reason = check_action("type_text", {"input_automation": False})
    assert not allowed and cap == "input_automation" and "not granted" in reason
    assert check_action("wait", {})[0] is True  # no capability required
    # an unknown verb is deny-by-default (requires input_automation)
    allowed, cap, _ = check_action("mystery", {})
    assert not allowed and cap == "input_automation"


def test_gateway_denies_ungranted_capability(mock_shinkend, tmp_path):
    path = str(tmp_path / "denied.skn")
    with shinken.connect(
        mock_shinkend,
        record=True,
        enforce_capabilities=True,
        sandbox_capabilities={"input_automation": False},
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=1, y=1)
        env.save_replay(path)
    rp = Replay.load(path)
    # the denied action never reached the runtime (no click action recorded)…
    assert not any(e["kind"] == "action" and e["src"] == "click" for e in rp.events)
    # …and a deny decision is recorded with capability + reason
    deny = next(e for e in rp.events if e["kind"] == "permission" and e["src"] == "deny")
    assert deny["payload"]["capability"] == "input_automation"
    assert deny["payload"]["verb"] == "click" and "not granted" in deny["payload"]["reason"]


def test_gateway_allows_permitted_input(mock_shinkend, tmp_path):
    path = str(tmp_path / "ok.skn")
    with shinken.connect(mock_shinkend, record=True, enforce_capabilities=True) as env:
        env.click(x=5, y=5)  # input_automation granted by default → passes
        env.save_replay(path)
    rp = Replay.load(path)
    assert any(e["kind"] == "action" and e["src"] == "click" for e in rp.events)


def test_enforcement_is_opt_in(mock_shinkend):
    # a non-recording session does not enforce by default: a restricted envelope does
    # not block (back-compat for the plain connect() one-liner)
    with shinken.connect(mock_shinkend, sandbox_capabilities={"input_automation": False}) as env:
        env.click(x=1, y=1)  # must not raise


def test_enforcement_defaults_on_when_recording(mock_shinkend, tmp_path):
    # #161: a recorded session enforces its envelope by default (no enforce_capabilities
    # arg), so the envelope is a real reference boundary rather than audit-only.
    path = str(tmp_path / "rec.skn")
    with shinken.connect(
        mock_shinkend,
        record=True,
        sandbox_capabilities={"input_automation": False},
    ) as env:
        with pytest.raises(shinken.CapabilityDenied):
            env.click(x=1, y=1)
        env.save_replay(path)
    rp = Replay.load(path)
    # the denied action never reached the runtime, and a deny decision was recorded
    assert not any(e["kind"] == "action" and e["src"] == "click" for e in rp.events)
    assert any(e["kind"] == "permission" and e["src"] == "deny" for e in rp.events)


def test_enforcement_explicit_off_overrides_record(mock_shinkend):
    # #161: enforce_capabilities=False is honoured even when recording (explicit opt-out)
    with shinken.connect(
        mock_shinkend,
        record=True,
        enforce_capabilities=False,
        sandbox_capabilities={"input_automation": False},
    ) as env:
        env.click(x=1, y=1)  # must not raise — enforcement explicitly disabled
