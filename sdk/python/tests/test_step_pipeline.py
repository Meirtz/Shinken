"""Pipelined step (k actions + fused observation in ~1 RTT) —
``AsyncSandbox.step`` / ``Sandbox.step`` (many-sandbox-concurrency.md §5).

Covers: single-flight pipelining (proven against a server that withholds every reply
until ALL k+1 frames arrived — a serial client would deadlock), reply ordering,
observation fusion, the honest partial-failure surface (no ``skipped``: later actions
really execute server-side after an earlier failure), gateway-denial rows, and
connection death mid-step — through both the async core and the sync facade.
"""

from __future__ import annotations

import asyncio
import functools
import json

from websockets.asyncio.server import serve

from shinken import connect
from shinken.client import aconnect

_PT = {"kind": "point_px", "x": 10, "y": 20}
ACTIONS3 = [
    {"verb": "click", "target": _PT},
    {"verb": "type_text", "text": "hi"},
    {"verb": "key", "keys": "ctrl+s"},
]
OBSERVE = {"format": "jpeg", "quality": 80, "max_long_edge": 1024}

# A valid 1x1 PNG, base64 (same as conftest's).
_PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)

_WELCOME = {
    "type": "welcome",
    "v": 0,
    "server": {"name": "mock-shinkend", "version": "0", "platform": "linux"},
    "capabilities": {
        "schema_version": 0,
        "verbs": ["click", "type_text", "key", "screenshot"],
        "targets": ["point_px"],
        "observation_types": ["screenshot"],
        "image_formats": ["png", "jpeg"],
    },
}


def _reply_for(msg: dict) -> dict:
    action = msg.get("action") or {}
    if action.get("verb") == "screenshot":
        return {
            "type": "observation",
            "obs_id": "o1",
            "cause": msg["call_id"],
            "image": {
                "ref": _PNG_1X1,
                "w": 1,
                "h": 1,
                "scope": "screen",
                "format": action.get("format") or "png",
            },
        }
    return {"type": "ack", "call_id": msg["call_id"], "ok": True}


async def _deferred_handler(ws, expect: int, received: list[str]) -> None:
    """Withhold EVERY reply until all ``expect`` action frames arrived, then answer them
    in order. A client that awaits each reply before the next send (the serial facade)
    deadlocks here; only pipelined dispatch completes."""
    buffered: list[dict] = []
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("type") == "hello":
            await ws.send(json.dumps(_WELCOME))
        elif msg.get("type") == "action":
            received.append((msg.get("action") or {}).get("verb"))
            buffered.append(msg)
            if len(buffered) == expect:
                for m in buffered:
                    await ws.send(json.dumps(_reply_for(m)))
                buffered = []


async def _die_after_first_handler(ws) -> None:
    """Ack the first action, then drop the connection — the rest of the step is on the
    wire but never answered."""
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("type") == "hello":
            await ws.send(json.dumps(_WELCOME))
        elif msg.get("type") == "action":
            await ws.send(json.dumps(_reply_for(msg)))
            return  # handler exit closes the websocket


def test_step_is_single_flight_pipelined():
    """All k+1 frames must be on the wire BEFORE any reply is awaited."""

    async def go():
        received: list[str] = []
        handler = functools.partial(_deferred_handler, expect=4, received=received)
        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            sb = await aconnect(f"127.0.0.1:{port}")
            try:
                # bounded: a serial client would hang against the deferred server
                res = await asyncio.wait_for(
                    sb.step(ACTIONS3, observe={"format": "jpeg"}), timeout=5
                )
            finally:
                await sb.close()
        # the server saw the actions then the trailing screenshot, in caller order
        assert received == ["click", "type_text", "key", "screenshot"]
        assert [r["verb"] for r in res["results"]] == ["click", "type_text", "key"]
        assert [r["status"] for r in res["results"]] == ["ok", "ok", "ok"]
        assert [r["index"] for r in res["results"]] == [0, 1, 2]
        assert res["completed"] is True and res["failure_kind"] is None
        assert res["observation"] is not None and res["observation"]["format"] == "jpeg"

    asyncio.run(go())


def test_step_sync_facade_with_observation_fusion(mock_shinkend):
    with connect(mock_shinkend) as env:
        before = env.actions_dispatched
        res = env.step(ACTIONS3, observe=OBSERVE)
        # the action counter counts the k actions, not the fused observation
        assert env.actions_dispatched == before + 3
        # the server really executed the actions (recorded effects, not echoes)
        state = env.query("state")
    assert res["completed"] is True and res["failure_kind"] is None
    assert all(r["ok"] and r["status"] == "ok" and r["ack"] for r in res["results"])
    obs = res["observation"]
    assert obs["format"] == "jpeg" and obs["bytes"] and obs["w"] == 1
    assert "png" not in obs  # the png alias only exists when the codec really is PNG
    assert res["observation_error"] is None
    assert state["typed"] == "hi" and state["keys"] == ["ctrl+s"]


def test_step_without_observe_returns_no_observation(mock_shinkend):
    with connect(mock_shinkend) as env:
        res = env.step(ACTIONS3)
    assert res["completed"] is True
    assert res["observation"] is None and res["observation_error"] is None


def test_step_partial_failure_has_no_skipped_and_observation_still_returns(mock_shinkend):
    """The honest semantics: a mid-step server-side failure does NOT stop later actions
    (they are already on the wire and execute), and the fused observation still returns."""
    actions = [
        {"verb": "click", "target": _PT},
        {"verb": "explode"},  # the mock nacks an unknown verb
        {"verb": "type_text", "text": "after-failure"},
    ]
    with connect(mock_shinkend) as env:
        res = env.step(actions, observe=OBSERVE)
        state = env.query("state")
    assert [r["status"] for r in res["results"]] == ["ok", "error", "ok"]
    assert "skipped" not in {r["status"] for r in res["results"]}
    assert res["results"][1]["ok"] is False and res["results"][1]["error"]
    assert res["failure_kind"] == "error"
    # every action got a deliberate outcome, so the step is complete despite the failure
    assert res["completed"] is True
    # action 2 really executed server-side AFTER action 1 failed
    assert state["typed"] == "after-failure"
    assert res["observation"] is not None and res["observation"]["format"] == "jpeg"


def test_step_gateway_denial_is_error_row_and_never_dispatched(mock_shinkend):
    caps = {"screenshot": False}
    actions = [
        {"verb": "click", "target": _PT},
        {"verb": "screenshot"},
        {"verb": "key", "keys": "a"},
    ]
    with connect(mock_shinkend, enforce_capabilities=True, sandbox_capabilities=caps) as env:
        res = env.step(actions)
        state = env.query("state")
    # the denied action is an error row; the OTHER actions still shipped (no early stop)
    assert [r["status"] for r in res["results"]] == ["ok", "error", "ok"]
    assert res["failure_kind"] == "error" and res["completed"] is True
    assert state["keys"] == ["a"]


def test_step_denied_observe_does_not_suppress_actions(mock_shinkend):
    caps = {"screenshot": False}
    with connect(mock_shinkend, enforce_capabilities=True, sandbox_capabilities=caps) as env:
        res = env.step([{"verb": "click", "target": _PT}], observe=OBSERVE)
    assert res["results"][0]["status"] == "ok"
    assert res["observation"] is None and res["observation_error"]
    assert res["failure_kind"] is None  # the action list itself succeeded


def test_step_async_api(mock_shinkend):
    async def go():
        sb = await aconnect(mock_shinkend)
        try:
            res = await sb.step(ACTIONS3, observe={"format": "png"})
        finally:
            await sb.close()
        assert [r["status"] for r in res["results"]] == ["ok", "ok", "ok"]
        obs = res["observation"]
        assert obs["format"] == "png" and obs["png"][:8] == b"\x89PNG\r\n\x1a\n"

    asyncio.run(go())


def test_step_connection_death_mid_step_classifies_sandbox_died():
    async def go():
        async with serve(_die_after_first_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            sb = await aconnect(f"127.0.0.1:{port}")
            try:
                res = await asyncio.wait_for(sb.step(ACTIONS3, observe=OBSERVE), timeout=10)
            finally:
                await sb.close()
        assert res["results"][0]["status"] == "ok"
        # the rest were on the wire when the connection died: unknowable whether they
        # executed, classified as infrastructure death (#56), never as `skipped`
        assert res["results"][1]["status"] == "sandbox_died"
        assert res["results"][2]["status"] == "sandbox_died"
        assert res["failure_kind"] == "sandbox_died" and res["completed"] is False
        assert res["observation"] is None and res["observation_error"]

    asyncio.run(go())
