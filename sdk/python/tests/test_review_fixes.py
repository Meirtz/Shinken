"""Regression tests for review-wave bug fixes (#146 scroll-zero, #147 eval connect, #148 CDP)."""

from __future__ import annotations

import json as _json

import pytest
from websockets.exceptions import ConnectionClosed

import shinken
from shinken.adapters import OpenAIComputerUseAdapter
from shinken.cdp import CdpSource
from shinken.eval import click_then_type_task, run_eval

OAI = OpenAIComputerUseAdapter()


# --- #146: OpenAI adapter preserves zero-valued scroll components -------------------
@pytest.mark.parametrize("sx,sy", [(0, 5), (5, 0), (0, 0), (3, -3)])
def test_openai_scroll_preserves_zero_axes(sx, sy):
    call = {"action": {"type": "scroll", "x": 1, "y": 2, "scroll_x": sx, "scroll_y": sy}}
    out = OAI.to_aci_actions(call)[0]
    assert out["verb"] == "scroll" and out["target"] == {"kind": "point_px", "x": 1, "y": 2}
    assert out["dx"] == sx and out["dy"] == sy  # both axes preserved, including explicit 0


def test_openai_scroll_omits_absent_axis():
    out = OAI.to_aci_actions({"action": {"type": "scroll", "x": 1, "y": 2, "scroll_y": 4}})[0]
    assert "dx" not in out and out["dy"] == 4  # an absent axis stays absent (not forced to 0)


# --- #148: CdpSource._call bounds the wait + fails fast on a closed socket -----------
class _FakeWS:
    def __init__(self, script):
        self._script = list(script)
        self.sent: list[str] = []

    def send(self, s):
        self.sent.append(s)

    def recv(self, timeout=None):
        if not self._script:
            raise TimeoutError("no more frames")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_cdp_call_skips_unrelated_events_then_returns():
    ws = _FakeWS(
        [_json.dumps({"method": "Some.event"}), _json.dumps({"id": 1, "result": {"ok": 1}})]
    )
    assert CdpSource(ws_url="ws://x")._call(ws, "X.y") == {"ok": 1}


def test_cdp_call_times_out():
    ws = _FakeWS([TimeoutError("slow")])
    with pytest.raises(TimeoutError):
        CdpSource(ws_url="ws://x", timeout=0.2)._call(ws, "X.y")


def test_cdp_call_fails_fast_on_closed_ws():
    ws = _FakeWS([ConnectionClosed(None, None)])
    with pytest.raises(RuntimeError):
        CdpSource(ws_url="ws://x")._call(ws, "X.y")


# --- #147: run_eval turns a connect failure into a per-replica error, not a crash ---
def test_run_eval_records_connect_failure_per_replica(mock_shinkend, tmp_path):
    calls = [0]

    def factory():
        calls[0] += 1
        if calls[0] == 2:
            raise ConnectionError("sandbox unavailable")
        return shinken.connect(mock_shinkend, record=True)

    summary = run_eval(click_then_type_task(10, 20, "hi"), factory, n=3, out_dir=str(tmp_path))
    assert summary.n == 3 and summary.setup_errors == 1  # the failed connect is one error
    errs = [r for r in summary.results if r.error]
    assert len(errs) == 1 and "sandbox unavailable" in errs[0].error and errs[0].bundle is None
    assert summary.passed == 2  # the other two replicas still ran + verified
