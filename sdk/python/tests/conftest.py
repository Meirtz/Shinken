"""Test fixtures: an in-process mock ``shinkend`` so the SDK can be tested without
the Rust binary. It mirrors the M0 message handling in shinkend/src/protocol.rs."""

from __future__ import annotations

import asyncio
import json
import socket
import threading

import pytest
from websockets.asyncio.server import serve


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _handler(ws) -> None:
    async for raw in ws:
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "hello":
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "v": 0,
                        "server": {"name": "mock-shinkend", "version": "0", "platform": "linux"},
                        "capabilities": {
                            "schema_version": 0,
                            "verbs": ["click", "type_text", "key", "screenshot", "wait"],
                            "targets": ["point_px", "element_ref"],
                            "observation_types": ["a11y", "screenshot", "video"],
                            "max_long_edge": 2576,
                        },
                    }
                )
            )
        elif kind == "ping":
            await ws.send(json.dumps({"type": "pong", "t": msg.get("t")}))
        elif kind == "query":
            value = "linux" if msg.get("q") == "platform" else {"w": 1280, "h": 800}
            reply = {"type": "result", "call_id": msg.get("call_id"), "ok": True, "value": value}
            await ws.send(json.dumps(reply))


@pytest.fixture
def mock_shinkend():
    port = _free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    async def _boot():
        return await serve(_handler, "127.0.0.1", port)

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_boot())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="mock-shinkend", daemon=True)
    thread.start()
    assert ready.wait(5), "mock shinkend failed to start"
    try:
        yield f"127.0.0.1:{port}"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
