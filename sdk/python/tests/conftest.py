"""Test fixtures: an in-process mock ``shinkend`` so the SDK can be tested without
the Rust binary. It mirrors the M0 message handling in shinkend/src/protocol.rs."""

from __future__ import annotations

import asyncio
import base64
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
    "drag",
    "mouse_down",
    "mouse_up",
    "scroll",
    "type_text",
    "key",
    "screenshot",
    "start_screencast",
    "stop_screencast",
    "wait",
    # structured-observation family (M1b) — mirrored from the runtime's verb set
    "observe",
    "invoke_action",
    "set_value",
    # typed in-guest exec channel (G1)
    "exec",
    # desktop verbs (G2+G3) — clipboard + app launch/activate
    "clipboard_get",
    "clipboard_set",
    "launch_app",
    "activate_window",
}

# The fixed window list the mock answers `query q="list_windows"` with — shaped like
# the real runtime's EWMH enumeration ({id, title, pid, x, y, w, h, focused}).
_MOCK_WINDOWS = [
    {
        "id": 0x1A,
        "title": "xterm",
        "pid": 1234,
        "x": 4,
        "y": 8,
        "w": 484,
        "h": 316,
        "focused": True,
    },
    {
        "id": 0x2B,
        "title": "xclock",
        "pid": 1235,
        "x": 40,
        "y": 40,
        "w": 200,
        "h": 200,
        "focused": False,
    },
]

# A valid 1x1 PNG (signature + IHDR/IDAT/IEND), base64-encoded.
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)
_PNG_1X1_BYTES = base64.b64decode(_PNG_1X1)


def _binary_frame(header: dict, payloads: list[bytes]) -> bytes:
    """Assemble one binary media frame the way shinkend does: ``u32 LE header_len |
    JSON header | concatenated payloads``. The caller's ``header`` already carries
    the image/tile ``off``/``len`` offsets into the payload area."""
    head = json.dumps(header).encode("utf-8")
    return len(head).to_bytes(4, "little") + head + b"".join(payloads)


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
    delta: bool = False,
    binary: bool = False,
) -> None:
    """Push server-pushed screencast frames with advancing seq until cancelled. The
    frame's reported width echoes ``max_long_edge`` so a test can confirm the cap
    travelled over the wire from the SDK; ``scope`` echoes the requested capture
    region, as the real runtime tags every frame with ``spec.scope`` (#143).
    ``start_seq``/``streams`` mirror the runtime's resume registry (#56): seq starts
    where the logical stream left off, and every sent frame records the next seq so a
    later ``resume_stream`` can continue it. With ``delta`` (B2), the FIRST frame of
    this push (start or resume — the runtime's tile baseline doesn't survive the
    stream task) is a full keyframe and the rest are dirty-tile frames (``tiles``
    instead of ``image``), like the real runtime's delta mode."""
    seq = start_seq
    period = max(1.0 / fps, 0.005)
    width = max_long_edge or 1
    first = True
    with contextlib.suppress(asyncio.CancelledError, Exception):
        while True:
            frame: dict = {
                "type": "observation",
                "obs_id": f"{stream_id}-{seq}",
                "stream": stream_id,
                "seq": seq,
            }
            if delta and not first:
                if binary:
                    frame["tiles"] = [
                        {"x": 0, "y": 0, "w": 1, "h": 1, "off": 0, "len": len(_PNG_1X1_BYTES)}
                    ]
                    payload = _binary_frame(frame, [_PNG_1X1_BYTES])
                else:
                    frame["tiles"] = [{"x": 0, "y": 0, "w": 1, "h": 1, "ref": _PNG_1X1}]
            else:
                meta = {"w": width, "h": 1, "scope": scope, "format": format}
                if binary:
                    frame["image"] = {"off": 0, "len": len(_PNG_1X1_BYTES), **meta}
                    payload = _binary_frame(frame, [_PNG_1X1_BYTES])
                else:
                    frame["image"] = {"ref": _PNG_1X1, **meta}
            await ws.send(payload if binary else json.dumps(frame))
            first = False
            seq += 1
            if streams is not None:
                streams[stream_id] = seq
            await asyncio.sleep(period)


async def _send_observation(ws, cid: str, fmt: str, binary: bool, scope: str = "screen") -> None:
    """Send one image-bearing observation answering ``cid`` via ``cause`` — the shape
    shared by the one-shot screenshot reply and the act-returns-observation follow-up
    (binary frame on a binary-negotiated session, base64-in-JSON text otherwise)."""
    meta = {"w": 1, "h": 1, "scope": scope, "format": fmt}
    # pointer metadata rides one-shot observations (capture pixels, [x, y]); the mock
    # mirrors the runtime so SDK passthrough is exercised on every screenshot test
    frame = {"type": "observation", "obs_id": f"obs-{cid}", "cause": cid, "pointer": [11, 22]}
    if binary:
        frame["image"] = {"off": 0, "len": len(_PNG_1X1_BYTES), **meta}
        await ws.send(_binary_frame(frame, [_PNG_1X1_BYTES]))
    else:
        frame["image"] = {"ref": _PNG_1X1, **meta}
        await ws.send(json.dumps(frame))


def _record(state: dict, action: dict) -> None:
    """Apply an inbound action to the mock's in-memory state, so a verifier can read back
    the *observed* effect (a click landed, text was typed, keys were pressed) instead of
    trusting the task's own inputs. This is what makes the eval fixtures non-vacuous."""
    verb = action.get("verb")
    if verb in ("click", "double_click", "right_click", "move"):
        tgt = action.get("target") or {}
        entry = {"verb": verb, "kind": tgt.get("kind"), "x": tgt.get("x"), "y": tgt.get("y")}
        (state["moves"] if verb == "move" else state["clicks"]).append(entry)
    elif verb == "drag":
        tgt, to = action.get("target") or {}, action.get("to") or {}
        state["drags"].append(
            {
                "from": {"x": tgt.get("x"), "y": tgt.get("y")},
                "to": {"x": to.get("x"), "y": to.get("y")},
                "duration_ms": action.get("duration_ms"),
                "button": action.get("button"),
            }
        )
    elif verb in ("mouse_down", "mouse_up"):
        tgt = action.get("target") or {}
        state["buttons"].append(
            {
                "verb": verb,
                "button": action.get("button"),
                "x": tgt.get("x"),
                "y": tgt.get("y"),
            }
        )
    elif verb == "screenshot":
        # Capture-lever passthrough (scope/format/quality/max_long_edge): lets a test
        # assert the requested codec/downscale really travelled over the wire.
        state["screenshots"].append(
            {k: action.get(k) for k in ("scope", "format", "quality", "max_long_edge")}
        )
    elif verb == "type_text":
        state["typed"] += action.get("text", "")
    elif verb == "key":
        state["keys"].append(action.get("keys", ""))
    elif verb == "scroll":
        state["scrolls"].append({"dx": action.get("dx"), "dy": action.get("dy")})
    elif verb in ("invoke_action", "set_value"):
        # Element verbs (M1b): record the ref + payload so a verifier can read back
        # what the AX path was asked to do.
        state["element_calls"].append(
            {
                "verb": verb,
                "ref": (action.get("target") or {}).get("ref"),
                "text": action.get("text"),
            }
        )
    elif verb == "clipboard_set":
        # Desktop verbs (G2): the mock keeps a real clipboard so set→get roundtrips.
        state["clipboard"] = action.get("text", "")
    elif verb == "launch_app":
        state["launches"].append({"app": action.get("app"), "args": action.get("args")})
    elif verb == "activate_window":
        state["activations"].append(
            {"window_id": action.get("window_id"), "app": action.get("app")}
        )
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
                "delta": action.get("delta"),
            }
        )


def _emulate_exec(action: dict) -> dict:
    """The mock's deterministic exec emulation (no real process runs): ``echo``
    behaves like echo, ``false`` exits 1, ``sleepy`` simulates a guest-side timeout
    kill; anything else answers a JSON dump of the received parameters on stdout
    (plus a fixed stderr probe), so tests can assert the full plumbing."""
    argv = action.get("argv")
    if argv and argv[0] == "echo":
        return {"rc": 0, "stdout": " ".join(argv[1:]) + "\n", "stderr": ""}
    if argv and argv[0] == "false":
        return {"rc": 1, "stdout": "", "stderr": ""}
    if argv and argv[0] == "sleepy":
        return {"rc": None, "stdout": "", "stderr": "", "timed_out": True, "signal": 9}
    echo = {k: action.get(k) for k in ("argv", "shell", "cwd", "env", "stdin")}
    return {"rc": 0, "stdout": json.dumps(echo), "stderr": "err-probe\n"}


async def _send_exec_reply(ws, cid: str, action: dict, binary: bool) -> None:
    """Answer one mock exec action the way the real runtime does: the buffered form
    with a typed ``result`` (value = ExecResult), the streamed form with an ack,
    seq-ordered ``exec_output`` events (binary frames on a binary session — header
    ``data.off/len`` + raw payload), and one terminal ``exec_exit``."""
    emu = _emulate_exec(action)
    if not action.get("stream"):
        await ws.send(
            json.dumps(
                {
                    "type": "result",
                    "call_id": cid,
                    "ok": True,
                    "value": {
                        "exit_code": emu["rc"],
                        "signal": emu.get("signal"),
                        "timed_out": emu.get("timed_out", False),
                        "stdout": emu["stdout"],
                        "stderr": emu["stderr"],
                        "stdout_truncated": False,
                        "stderr_truncated": False,
                        "duration_ms": 1.5,
                    },
                }
            )
        )
        return
    await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
    out_bytes = emu["stdout"].encode()
    half = max(1, len(out_bytes) // 2)
    # two stdout chunks (so consumers must reassemble in seq order) + one stderr
    chunks = [("stdout", out_bytes[:half]), ("stdout", out_bytes[half:])]
    chunks.append(("stderr", emu["stderr"].encode()))
    seq = 0
    for channel, data in chunks:
        if not data:
            continue
        if binary:
            header = {
                "type": "exec_output",
                "cause": cid,
                "seq": seq,
                "channel": channel,
                "data": {"off": 0, "len": len(data)},
            }
            await ws.send(_binary_frame(header, [data]))
        else:
            await ws.send(
                json.dumps(
                    {
                        "type": "exec_output",
                        "cause": cid,
                        "seq": seq,
                        "channel": channel,
                        "data_b64": base64.b64encode(data).decode(),
                    }
                )
            )
        seq += 1
    await ws.send(
        json.dumps(
            {
                "type": "exec_exit",
                "cause": cid,
                "exit_code": emu["rc"],
                "signal": emu.get("signal"),
                "timed_out": emu.get("timed_out", False),
                "duration_ms": 1.5,
                "truncated": False,
            }
        )
    )


def _mock_frame_hash(state: dict) -> str:
    """The mock's screen-content identity: a deterministic function of the observed
    effects, so typing/clicking 'changes the screen' (the hash) like a real desktop
    and an untouched session keeps a stable hash — what the dedup tests need."""
    return f"fh-{len(state['typed'])}-{len(state['clicks'])}"


async def _handler(
    ws,
    streams: dict[str, int] | None = None,
    support_binary: bool = True,
    support_dedup: bool = True,
    support_structured: bool = True,
    support_exec: bool = True,
) -> None:
    cast: asyncio.Task | None = None
    state = {
        "typed": "",
        "keys": [],
        "clicks": [],
        "moves": [],
        "scrolls": [],
        "casts": [],
        "drags": [],
        "buttons": [],
        "element_calls": [],
        "execs": [],
        "screenshots": [],
        "observes": 0,
        "clipboard": None,
        "launches": [],
        "activations": [],
    }
    # Resume registry (#56): logical stream id -> next seq. Server-instance scoped
    # (shared across this mock's connections, so a reconnecting client can resume).
    streams = {} if streams is None else streams
    # Binary media framing, negotiated like the real runtime: on iff the client's
    # hello carried accept.binary_frames AND this mock advertises the capability
    # (support_binary=False simulates a pre-binary runtime, which ignores the offer).
    binary = False
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
            if not support_structured and verb in ("observe", "invoke_action", "set_value"):
                # simulate a pre-engine runtime: the verb is unknown to it
                await ws.send(
                    json.dumps(
                        {
                            "type": "ack",
                            "call_id": msg.get("call_id", "?"),
                            "ok": False,
                            "error": f"unknown verb: {verb}",
                        }
                    )
                )
                continue
            if not support_exec and verb == "exec":
                # simulate a pre-exec runtime: ActionSpec rejects the unknown fields
                await ws.send(
                    json.dumps(
                        {
                            "type": "ack",
                            "call_id": msg.get("call_id", "?"),
                            "ok": False,
                            "error": "bad action: unknown field `argv`",
                        }
                    )
                )
                continue
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
            binary = support_binary and bool((msg.get("accept") or {}).get("binary_frames"))
            capabilities = {
                "schema_version": 0,
                "verbs": sorted(_MOCK_VERBS - (set() if support_exec else {"exec"})),
                "targets": ["point_px", "element_ref"],
                "observation_types": ["a11y", "screenshot", "screencast"],
                "max_long_edge": 2576,
                "image_formats": ["png", "jpeg"],
                "observe_after_act": True,
            }
            if support_binary:
                capabilities["binary_frames"] = True
            if support_dedup:
                capabilities["frame_dedup"] = True
            if support_structured:
                capabilities["structured_observation"] = True
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "v": 0,
                        "server": {"name": "mock-shinkend", "version": "0", "platform": "linux"},
                        "capabilities": capabilities,
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
            elif q == "list_windows":
                value = _MOCK_WINDOWS
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
                # A PRE-DEDUP runtime parses actions with deny_unknown_fields: an
                # if_none_match it has never heard of is a hard nack, not a silent
                # ignore — the back-compat behavior the SDK's capability gate must
                # never trigger.
                if not support_dedup and "if_none_match" in action:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ack",
                                "call_id": cid,
                                "ok": False,
                                "error": "bad action: unknown field `if_none_match`",
                            }
                        )
                    )
                    continue
                frame_hash = _mock_frame_hash(state)
                # Content negotiation (frame_dedup): a hash hit answers with the
                # compact not_modified observation — TEXT even on a binary session
                # (no payload to carry), like the real runtime.
                if support_dedup and action.get("if_none_match") == frame_hash:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "observation",
                                "obs_id": "o1",
                                "cause": cid,
                                "not_modified": True,
                                "frame_hash": frame_hash,
                            }
                        )
                    )
                    continue
                # Echo the requested codec so a test can assert `format` travelled the
                # wire (the real runtime reports the codec it actually encoded in).
                fmt = action.get("format") or "png"
                meta = {"w": 1, "h": 1, "scope": "screen", "format": fmt}
                extra = {"frame_hash": frame_hash} if support_dedup else {}
                # pointer metadata rides one-shot screenshots (capture pixels, [x, y])
                extra["pointer"] = [11, 22]
                if binary:
                    await ws.send(
                        _binary_frame(
                            {
                                "type": "observation",
                                "obs_id": "o1",
                                "cause": cid,
                                "image": {"off": 0, "len": len(_PNG_1X1_BYTES), **meta},
                                **extra,
                            },
                            [_PNG_1X1_BYTES],
                        )
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "observation",
                                "obs_id": "o1",
                                "cause": cid,
                                "image": {"ref": _PNG_1X1, **meta},
                                **extra,
                            }
                        )
                    )
            elif verb == "observe":
                # Guest structured observation (M1b), mirroring shinkend's reply:
                # tree_text + full elements + revision/focus; rev>1 changes the
                # entry's value so a diff observe has something to show.
                state["observes"] += 1
                rev = state["observes"]
                value = "hello" if rev > 1 else ""
                elements = [
                    {"ref": "e1", "role": "frame", "name": "Win", "bbox": [0, 0, 800, 600]},
                    {
                        "ref": "e2",
                        "role": "push button",
                        "name": "OK",
                        "bbox": [10, 10, 80, 30],
                        "states": ["enabled"],
                        "actions": ["click"],
                    },
                    {
                        "ref": "e3",
                        "role": "entry",
                        "value": value,
                        "bbox": [10, 60, 200, 30],
                        "states": ["editable", "focused"],
                        "focused": True,
                    },
                ]
                for el in elements:
                    el["source"] = "atspi"
                diff = bool(action.get("diff")) and rev > 1
                obs = {
                    "type": "observation",
                    "obs_id": f"obs-{cid}",
                    "cause": cid,
                    "tree": "diff" if diff else "full",
                    "revision": rev,
                    "focus": "e3",
                    "node_count": len(elements),
                    "capture_ms": 1.5,
                    "elements": elements,
                }
                if diff:
                    obs["diff_of"] = rev - 1
                    obs["tree_text"] = (
                        f"app: mock  (revision {rev}, diff of revision {rev - 1})\n"
                        f'~   e3 entry (editable,focused) Value:"{value}"\nfocus: e3'
                    )
                else:
                    obs["tree_text"] = (
                        f"app: mock  (revision {rev}, 3 nodes)\n"
                        'e1 frame "Win"\n'
                        '  e2 push button (enabled) "OK" Actions:[click]\n'
                        f'  e3 entry (editable,focused) Value:"{value}"\n'
                        "focus: e3"
                    )
                await ws.send(json.dumps(obs))
            elif verb == "exec":
                # Typed in-guest exec (G1): record the request (so verifiers can read
                # back WHAT was asked) and answer like the real runtime would.
                state["execs"].append({k: v for k, v in action.items() if k != "verb"})
                await _send_exec_reply(ws, cid, action, binary)
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
                        action.get("delta") or False,
                        binary,
                    )
                )
            elif verb == "stop_screencast":
                if cast is not None:
                    cast.cancel()
                    with contextlib.suppress(Exception):
                        await cast
                    cast = None
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
            elif verb == "clipboard_get":
                # The read answers a typed `result` carrying {text} (an ack has no
                # payload); an empty clipboard nacks, like the real runtime.
                if state["clipboard"] is None:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "ack",
                                "call_id": cid,
                                "ok": False,
                                "error": "clipboard is empty (nothing set this session)",
                            }
                        )
                    )
                else:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "result",
                                "call_id": cid,
                                "ok": True,
                                "value": {"text": state["clipboard"]},
                            }
                        )
                    )
            else:
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))
                # Act-returns-observation: a mutating action with `observe` gets a
                # fresh observation right after the ack (cause = call_id), like the
                # real runtime's follow-up.
                obs = action.get("observe")
                if obs is not None:
                    await _send_observation(
                        ws, cid, obs.get("format") or "png", binary, obs.get("scope") or "screen"
                    )
    if cast is not None:
        cast.cancel()


def _start_mock_server(
    support_binary: bool = True,
    support_dedup: bool = True,
    support_structured: bool = True,
    support_exec: bool = True,
) -> tuple[str, object, object]:
    """Start one mock shinkend on its own loop/thread; return (addr, loop, thread)."""
    port = _free_port()
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    streams: dict[str, int] = {}

    async def _boot():
        return await serve(
            functools.partial(
                _handler,
                streams=streams,
                support_binary=support_binary,
                support_dedup=support_dedup,
                support_structured=support_structured,
                support_exec=support_exec,
            ),
            "127.0.0.1",
            port,
        )

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
def mock_shinkend_text_only():
    """A mock that simulates a PRE-BINARY runtime: no ``binary_frames`` capability in
    the welcome, the client's ``accept.binary_frames`` offer is ignored, and every
    observation stays base64-in-JSON text — the old-server back-compat path."""
    addr, loop, thread = _start_mock_server(support_binary=False)
    try:
        yield addr
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


@pytest.fixture
def mock_shinkend_no_dedup():
    """A mock that simulates a PRE-DEDUP runtime: no ``frame_dedup`` capability in
    the welcome, and any screenshot carrying ``if_none_match`` is hard-nacked (the
    real old runtime's deny_unknown_fields behavior) — so a test passes only if the
    SDK's capability gate kept the field off the wire."""
    addr, loop, thread = _start_mock_server(support_dedup=False)
    try:
        yield addr
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


@pytest.fixture
def mock_shinkend_no_structured():
    """A mock that simulates a PRE-ENGINE runtime: no ``structured_observation``
    capability in the welcome and the observe family answers ``unknown verb`` —
    the SDK must fall back to its co-located structured path."""
    addr, loop, thread = _start_mock_server(support_structured=False)
    try:
        yield addr
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


@pytest.fixture
def mock_shinkend_no_exec():
    """A mock that simulates a PRE-EXEC runtime: ``exec`` is absent from the
    welcome's verbs and an exec action is hard-nacked (the real old runtime's
    deny_unknown_fields behavior) — so a test passes only if the SDK's verb gate
    kept exec off the wire and consumers fell back to the out-of-band channel."""
    addr, loop, thread = _start_mock_server(support_exec=False)
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
