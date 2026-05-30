"""Direct AsyncSandbox / aconnect coverage, without the sync facade (#155)."""

from __future__ import annotations

import asyncio

import pytest
from websockets.exceptions import ConnectionClosed

from shinken.client import _parse_welcome, aconnect


def _run(coro):
    return asyncio.run(coro)


def test_async_handshake_and_core_actions(mock_shinkend):
    async def go():
        sb = await aconnect(mock_shinkend)
        try:
            assert sb.platform_name == "linux" and "click" in sb.capabilities.verbs
            assert await sb.ping() >= 0
            assert await sb.screen_size() == {"w": 1280, "h": 800}
            ack = await sb.act("click", {"kind": "point_px", "x": 1, "y": 2})
            assert ack.get("ok") is True
            shot = await sb.screenshot()
            assert shot["png"][:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            await sb.close()

    _run(go())


def test_async_screencast_stream(mock_shinkend):
    async def go():
        sb = await aconnect(mock_shinkend)
        try:
            await sb.astart_screencast(8.0, None, None)
            frame = await sb.next_frame(2.0)
            assert frame is None or (frame.get("stream") is not None)
            await sb.astop_screencast()
        finally:
            await sb.close()

    _run(go())


def test_async_close_then_rpc_fails(mock_shinkend):
    async def go():
        sb = await aconnect(mock_shinkend)
        await sb.close()
        with pytest.raises((ConnectionClosed, ConnectionError)):  # closed → RPC can't complete
            await sb.query("screen_size")

    _run(go())


def test_parse_welcome_rejects_malformed():
    with pytest.raises(RuntimeError):
        _parse_welcome({"type": "nope"})  # wrong type
    with pytest.raises(RuntimeError):
        _parse_welcome({"type": "welcome", "v": 1})  # unsupported ACI version
    with pytest.raises(RuntimeError):
        _parse_welcome({"type": "welcome", "v": 0, "capabilities": {"schema_version": 9}})
    caps, platform = _parse_welcome(
        {
            "type": "welcome",
            "v": 0,
            "capabilities": {"schema_version": 0, "verbs": ["click"]},
            "server": {"platform": "linux"},
        }
    )
    assert platform == "linux" and "click" in caps.verbs
