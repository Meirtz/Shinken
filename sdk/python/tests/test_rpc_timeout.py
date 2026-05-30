"""AsyncSandbox RPC timeout — a missing reply can't hang an async caller (#142)."""

from __future__ import annotations

import asyncio

import pytest

from shinken.client import AsyncSandbox, Capabilities


class _FakeWS:
    async def send(self, s):
        pass  # accept the send; never deliver a correlated reply (no reader running)

    async def close(self):
        pass


def test_rpc_times_out_and_cleans_up_pending():
    sb = AsyncSandbox(_FakeWS(), Capabilities(0, [], [], []), "linux")

    async def go():
        with pytest.raises(TimeoutError):
            await sb._rpc({"type": "query", "call_id": "c1", "q": "x"}, timeout=0.2)
        assert "c1" not in sb._pending  # the pending future is removed on timeout (no leak)

    asyncio.run(go())
