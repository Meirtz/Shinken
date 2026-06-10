"""Global frame-queue byte budget (docs/engineering/many-sandbox-concurrency.md §2).

The per-connection 32-frame drop-oldest bound is N-unbounded in aggregate; the
``GLOBAL_FRAME_BUDGET_BYTES`` knob shares one byte budget across every AsyncSandbox on
one event loop and evicts the approximately globally-oldest queued frames. These tests
drive the queue machinery directly (no wire), inside ``asyncio.run`` so each test gets
a fresh loop — and therefore fresh per-loop budget accounting.
"""

from __future__ import annotations

import asyncio

import pytest

import shinken.client as client
from shinken.client import _STREAM_LOST, AsyncSandbox, Capabilities


def _sandbox() -> AsyncSandbox:
    caps = Capabilities(schema_version=0, verbs=[], targets=[], observation_types=[])
    return AsyncSandbox(None, caps, "linux")


def _frame(seq: int, ref_len: int = 1000) -> dict:
    # ref_len % 4 == 0 keeps the payload valid base64 for next_frame's decode
    return {
        "type": "observation",
        "stream": "s",
        "seq": seq,
        "image": {"ref": "A" * ref_len, "w": 1, "h": 1, "format": "png"},
    }


def test_budget_off_keeps_per_connection_count_bound():
    """Default (None): exactly the historical behavior — 32-deep drop-oldest per conn."""
    assert client.GLOBAL_FRAME_BUDGET_BYTES is None

    async def run():
        sb = _sandbox()
        for i in range(40):
            sb._push_frame(_frame(i))
        assert sb._frames.qsize() == 32
        # bookkeeping is maintained even with the knob off (so enabling it later works)
        assert sb._budget_state["used"] == 32 * 1000
        first = await sb.next_frame(1.0)
        assert first["seq"] == 8  # the 8 oldest were dropped by the count bound

    asyncio.run(run())


def test_budget_evicts_oldest_within_one_connection(monkeypatch):
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 5_000)

    async def run():
        sb = _sandbox()
        for i in range(8):
            sb._push_frame(_frame(i, ref_len=1000))
        state = sb._budget_state
        assert state["used"] == 5_000  # 3 oldest evicted to fit 5 × 1000
        seqs = [(await sb.next_frame(1.0))["seq"] for _ in range(5)]
        assert seqs == [3, 4, 5, 6, 7]
        assert state["used"] == 0

    asyncio.run(run())


def test_budget_evicts_across_sandboxes_oldest_anywhere(monkeypatch):
    """Drop-oldest-ANYWHERE: another connection's older frames are evicted first."""
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 5_000)

    async def run():
        a, b = _sandbox(), _sandbox()
        for i in range(3):
            a._push_frame(_frame(i, ref_len=1000))
        for i in range(5):
            b._push_frame(_frame(10 + i, ref_len=1000))
        # a's 3 frames were globally oldest — all evicted; b keeps all 5
        assert a._frames.qsize() == 0 and not a._queued_bytes
        assert b._frames.qsize() == 5
        assert a._budget_state is b._budget_state  # same loop, shared accounting
        assert a._budget_state["used"] == 5_000

    asyncio.run(run())


def test_consumption_releases_budget(monkeypatch):
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 3_000)

    async def run():
        sb = _sandbox()
        for i in range(3):
            sb._push_frame(_frame(i, ref_len=1000))
        assert sb._budget_state["used"] == 3_000
        assert (await sb.next_frame(1.0))["seq"] == 0
        assert sb._budget_state["used"] == 2_000
        sb._push_frame(_frame(3, ref_len=1000))  # fits again — nothing evicted
        assert sb._frames.qsize() == 3
        assert (await sb.next_frame(1.0))["seq"] == 1

    asyncio.run(run())


def test_lost_sentinel_is_never_counted_or_evicted(monkeypatch):
    """A dead sandbox's loss signal survives budget pressure from other connections."""
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 2_000)

    async def run():
        dead, busy = _sandbox(), _sandbox()
        dead._push_frame(_STREAM_LOST)
        for i in range(5):
            busy._push_frame(_frame(i, ref_len=1000))
        assert busy._budget_state["used"] <= 2_000
        assert dead._frames.qsize() == 1  # the sentinel was not evicted
        with pytest.raises(ConnectionError):
            await dead.next_frame(1.0)

    asyncio.run(run())


def test_close_releases_budget_but_keeps_sentinel(monkeypatch):
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 100_000)

    async def run():
        sb = _sandbox()
        for i in range(3):
            sb._push_frame(_frame(i, ref_len=1000))
        sb._push_frame(_STREAM_LOST)
        state = sb._budget_state
        assert state["used"] == 3_000
        await sb.close()
        assert state["used"] == 0 and state["frames"] == 0
        with pytest.raises(ConnectionError):  # the loss signal outlives the frames
            await sb.next_frame(1.0)

    asyncio.run(run())


def test_clear_frames_releases_budget(monkeypatch):
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 100_000)

    async def run():
        sb = _sandbox()
        for i in range(4):
            sb._push_frame(_frame(i, ref_len=1000))
        assert sb._budget_state["used"] == 4_000
        sb._clear_frames()
        assert sb._budget_state["used"] == 0
        assert sb._budget_state["frames"] == 0

    asyncio.run(run())


def test_budget_state_is_per_event_loop(monkeypatch):
    """The budget is per-loop (the deployment unit): a second loop starts from zero."""
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 2_000)
    states = []

    async def run():
        sb = _sandbox()
        for i in range(4):
            sb._push_frame(_frame(i, ref_len=1000))
        states.append(sb._budget_state)
        return sb._frames.qsize()

    assert asyncio.run(run()) == 2  # evicted down to the budget on loop #1
    assert asyncio.run(run()) == 2  # loop #2: same result, independent accounting
    assert states[0] is not states[1]


def test_order_bookkeeping_stays_bounded(monkeypatch):
    """Stale push-order entries are head-drained and compacted, not accumulated."""
    monkeypatch.setattr(client, "GLOBAL_FRAME_BUDGET_BYTES", 10**9)

    async def run():
        holder, churner = _sandbox(), _sandbox()
        holder._push_frame(_frame(0))  # one long-lived queued frame at the order head
        for i in range(600):
            churner._push_frame(_frame(i))
            await churner.next_frame(1.0)
        # entries for churner's 600 consumed frames must not pile up behind the holder
        assert len(holder._budget_state["order"]) <= 70

    asyncio.run(run())
