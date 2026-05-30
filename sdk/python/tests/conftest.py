"""Test fixtures: an in-process mock ``shinkend`` so the SDK can be tested without
the Rust binary. It mirrors the M0 message handling in shinkend/src/protocol.rs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading

import pytest
from websockets.asyncio.server import serve

# A valid 1x1 PNG (signature + IHDR/IDAT/IEND), base64-encoded.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _push_frames(ws, stream_id: str, fps: float, max_long_edge: int | None = None) -> None:
    """Push server-pushed screencast frames with advancing seq until cancelled. The
    frame's reported width echoes ``max_long_edge`` so a test can confirm the cap
    travelled over the wire from the SDK."""
    seq = 0
    period = max(1.0 / fps, 0.005)
    width = max_long_edge or 1
    with contextlib.suppress(asyncio.CancelledError, Exception):
        while True:
            await ws.send(
                json.dumps(
                    {
                        "type": "observation",
                        "obs_id": f"{stream_id}-{seq}",
                        "stream": stream_id,
                        "seq": seq,
                        "image": {"ref": _PNG_1X1, "w": width, "h": 1, "scope": "screen"},
                    }
                )
            )
            seq += 1
            await asyncio.sleep(period)


async def _handler(ws) -> None:
    cast: asyncio.Task | None = None
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
                            "verbs": [
                                "click",
                                "type_text",
                                "key",
                                "screenshot",
                                "start_screencast",
                                "stop_screencast",
                                "wait",
                            ],
                            "targets": ["point_px", "element_ref"],
                            "observation_types": ["a11y", "screenshot", "screencast"],
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
        elif kind == "action":
            verb = (msg.get("action") or {}).get("verb")
            cid = msg.get("call_id")
            if verb == "screenshot":
                await ws.send(
                    json.dumps(
                        {
                            "type": "observation",
                            "obs_id": "o1",
                            "cause": cid,
                            "image": {"ref": _PNG_1X1, "w": 1, "h": 1, "scope": "screen"},
                        }
                    )
                )
            elif verb == "start_screencast":
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
                action = msg.get("action") or {}
                fps = action.get("fps") or 50
                cast = asyncio.create_task(
                    _push_frames(ws, cid, fps, action.get("max_long_edge"))
                )
            elif verb == "stop_screencast":
                if cast is not None:
                    cast.cancel()
                    with contextlib.suppress(Exception):
                        await cast
                    cast = None
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
            else:
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
    if cast is not None:
        cast.cancel()


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
