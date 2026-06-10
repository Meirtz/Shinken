"""Test fixtures: an in-process mock ``shinkend`` so the SDK can be tested without
the Rust binary. It mirrors the M0 message handling in shinkend/src/protocol.rs."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import socket
import threading

import pytest
from websockets.asyncio.server import serve

from shinken import protocol

# Verbs this mock advertises and will accept — kept in sync with the real runtime's
# advertised set so the mock rejects a verb the runtime would not implement (instead of
# acking any unknown verb ok=True, which would let emitted-verb drift pass silently).
_MOCK_VERBS = {
    "click",
    "double_click",
    "right_click",
    "move",
    "scroll",
    "type_text",
    "key",
    "screenshot",
    "start_screencast",
    "stop_screencast",
    "wait",
}

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


async def _push_frames(
    ws,
    stream_id: str,
    fps: float,
    max_long_edge: int | None = None,
    scope: str = "screen",
    start_seq: int = 0,
    streams: dict[str, int] | None = None,
    format: str = "png",
) -> None:
    """Push server-pushed screencast frames with advancing seq until cancelled. The
    frame's reported width echoes ``max_long_edge`` so a test can confirm the cap
    travelled over the wire from the SDK; ``scope`` echoes the requested capture
    region, as the real runtime tags every frame with ``spec.scope`` (#143).
    ``start_seq``/``streams`` mirror the runtime's resume registry (#56): seq starts
    where the logical stream left off, and every sent frame records the next seq so a
    later ``resume_stream`` can continue it."""
    seq = start_seq
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
                        "image": {
                            "ref": _PNG_1X1,
                            "w": width,
                            "h": 1,
                            "scope": scope,
                            "format": format,
                        },
                    }
                )
            )
            seq += 1
            if streams is not None:
                streams[stream_id] = seq
            await asyncio.sleep(period)


def _record(state: dict, action: dict) -> None:
    """Apply an inbound action to the mock's in-memory state, so a verifier can read back
    the *observed* effect (a click landed, text was typed, keys were pressed) instead of
    trusting the task's own inputs. This is what makes the eval fixtures non-vacuous."""
    verb = action.get("verb")
    if verb in ("click", "double_click", "right_click", "move"):
        tgt = action.get("target") or {}
        entry = {"verb": verb, "kind": tgt.get("kind"), "x": tgt.get("x"), "y": tgt.get("y")}
        (state["moves"] if verb == "move" else state["clicks"]).append(entry)
    elif verb == "type_text":
        state["typed"] += action.get("text", "")
    elif verb == "key":
        state["keys"].append(action.get("keys", ""))
    elif verb == "scroll":
        state["scrolls"].append({"dx": action.get("dx"), "dy": action.get("dy")})
    elif verb == "start_screencast":
        # The capture params each start/resume actually carried over the wire, so a
        # test can assert a resume re-sent the ORIGINAL fps/max_long_edge/scope (#56).
        state["casts"].append(
            {
                "fps": action.get("fps"),
                "max_long_edge": action.get("max_long_edge"),
                "scope": action.get("scope"),
                "resume_stream": action.get("resume_stream"),
                "format": action.get("format"),
                "quality": action.get("quality"),
            }
        )


async def _handler(ws, streams: dict[str, int] | None = None) -> None:
    cast: asyncio.Task | None = None
    state = {"typed": "", "keys": [], "clicks": [], "moves": [], "scrolls": [], "casts": []}
    # Resume registry (#56): logical stream id -> next seq. Server-instance scoped
    # (shared across this mock's connections, so a reconnecting client can resume).
    streams = {} if streams is None else streams
    async for raw in ws:
        msg = json.loads(raw)
        # Validate every inbound frame against the ACI schema, so SDK-emitted wire drift
        # (an out-of-contract field, a fps over the schema max, a malformed target) fails a
        # test instead of being silently acked ok=True. The lone exemption is the test-only
        # `query q="state"` channel the eval verifiers use to read the mock's recorded
        # effects — it is not part of the real ACI query enum.
        is_state_query = msg.get("type") == "query" and msg.get("q") == "state"
        try:
            if not is_state_query:
                protocol.validate(msg)
        except Exception as exc:  # noqa: BLE001 — surface any schema violation to the client
            await ws.send(
                json.dumps(
                    {
                        "type": "ack",
                        "call_id": msg.get("call_id", "?"),
                        "ok": False,
                        "error": f"schema violation: {exc}",
                    }
                )
            )
            continue
        kind = msg.get("type")
        if kind == "action":
            verb = (msg.get("action") or {}).get("verb")
            if verb not in _MOCK_VERBS:
                await ws.send(
                    json.dumps(
                        {
                            "type": "ack",
                            "call_id": msg.get("call_id", "?"),
                            "ok": False,
                            "error": f"unsupported verb: {verb}",
                        }
                    )
                )
                continue
        if kind == "hello":
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "v": 0,
                        "server": {"name": "mock-shinkend", "version": "0", "platform": "linux"},
                        "capabilities": {
                            "schema_version": 0,
                            "verbs": sorted(_MOCK_VERBS),
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
            q = msg.get("q")
            if q == "platform":
                value: object = "linux"
            elif q == "state":
                value = state  # observed effects, for eval verifiers
            else:
                value = {"w": 1280, "h": 800}
            reply = {"type": "result", "call_id": msg.get("call_id"), "ok": True, "value": value}
            await ws.send(json.dumps(reply))
        elif kind == "action":
            action = msg.get("action") or {}
            verb = action.get("verb")
            cid = msg.get("call_id")
            _record(state, action)
            if verb == "screenshot":
                # Echo the requested codec so a test can assert `format` travelled the
                # wire (the real runtime reports the codec it actually encoded in).
                fmt = action.get("format") or "png"
                await ws.send(
                    json.dumps(
                        {
                            "type": "observation",
                            "obs_id": "o1",
                            "cause": cid,
                            "image": {
                                "ref": _PNG_1X1,
                                "w": 1,
                                "h": 1,
                                "scope": "screen",
                                "format": fmt,
                            },
                        }
                    )
                )
            elif verb == "start_screencast":
                if cast is not None:
                    cast.cancel()
                    with contextlib.suppress(Exception):
                        await cast
                    cast = None
                # Honor resume_stream with the runtime's semantics (#56): a known
                # logical stream keeps its id and seq carries on; an unknown or
                # expired one starts fresh (id = call_id, seq 0).
                resume = action.get("resume_stream")
                stream_id, start_seq = cid, 0
                if resume is not None and resume in streams:
                    stream_id, start_seq = resume, streams[resume]
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
                fps = action.get("fps") or 50
                cast = asyncio.create_task(
                    _push_frames(
                        ws,
                        stream_id,
                        fps,
                        action.get("max_long_edge"),
                        action.get("scope") or "screen",
                        start_seq,
                        streams,
                        action.get("format") or "png",
                    )
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


def _start_mock_server() -> tuple[str, object, object]:
    """Start one mock shinkend on its own loop/thread; return (addr, loop, thread)."""
    port = _free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    streams: dict[str, int] = {}

    async def _boot():
        return await serve(functools.partial(_handler, streams=streams), "127.0.0.1", port)

    def run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_boot())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=run, name="mock-shinkend", daemon=True)
    thread.start()
    assert ready.wait(5), "mock shinkend failed to start"
    return f"127.0.0.1:{port}", loop, thread


@pytest.fixture
def mock_shinkend():
    # Resume registry shared across this server's connections (#56) — but NOT across
    # tests (fresh per fixture). SDK call_ids are salted per AsyncSandbox instance
    # (c<salt>-<n>), so concurrent clients can't collide as stream ids either.
    addr, loop, thread = _start_mock_server()
    try:
        yield addr
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


@pytest.fixture
def mock_shinkend_many():
    """A factory yielding ``n`` independent mock shinkend addrs — for tests that hold
    several distinct sandboxes at once (e.g. SharedLoop). Each call returns ONLY the
    servers it started; all are torn down at fixture exit."""
    servers: list[tuple[str, object, object]] = []

    def make(n: int) -> list[str]:
        new = [_start_mock_server() for _ in range(n)]
        servers.extend(new)
        return [s[0] for s in new]

    try:
        yield make
    finally:
        for _addr, loop, thread in servers:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
