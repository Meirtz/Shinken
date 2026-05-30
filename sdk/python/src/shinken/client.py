"""ACI v0 client — async core (:class:`AsyncSandbox`) + sync facade (:class:`Sandbox`).

The sync facade runs a dedicated asyncio event loop on a background thread so the
public API can stay a clean one-liner (``env = shinken.connect()``) without leaking
``async``/``await`` into simple scripts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as _ws_connect

from .skn import Recorder

__all__ = ["connect", "aconnect", "Sandbox", "AsyncSandbox", "Capabilities"]

DEFAULT_ADDR = "127.0.0.1:8765"
_CLIENT = {"name": "shinken-py", "version": "0.0.0"}


@dataclass
class Capabilities:
    """Capabilities advertised by the Guest Runtime during the handshake."""

    schema_version: int
    verbs: list[str]
    targets: list[str]
    observation_types: list[str]
    max_long_edge: int | None = None


def _to_uri(addr: str) -> str:
    return addr if addr.startswith("ws://") or addr.startswith("wss://") else f"ws://{addr}"


def _target(target: Any, x: float | None, y: float | None) -> dict | None:
    """Build an ACI target from (element-ref | dict | x,y). None if unspecified."""
    if target is not None:
        if isinstance(target, dict):
            return target
        if isinstance(target, str):
            return {"kind": "element_ref", "ref": target}
        ref = getattr(target, "ref", None)
        if ref is not None:
            return {"kind": "element_ref", "ref": ref}
        raise TypeError(f"unsupported target: {target!r}")
    if x is not None and y is not None:
        return {"kind": "point_px", "x": x, "y": y}
    return None


def _parse_welcome(welcome: dict) -> tuple[Capabilities, str]:
    """Validate a `welcome` payload and build (Capabilities, platform). Raises on mismatch."""
    if welcome.get("type") != "welcome":
        raise RuntimeError(f"expected 'welcome', got {welcome.get('type')!r}")
    if welcome.get("v") != 0:
        raise RuntimeError(f"unsupported ACI version in welcome: {welcome.get('v')!r}")
    caps = welcome.get("capabilities") or {}
    if caps.get("schema_version") != 0:
        raise RuntimeError(f"unsupported ACI schema_version: {caps.get('schema_version')!r}")
    return (
        Capabilities(
            schema_version=caps["schema_version"],
            verbs=caps.get("verbs", []),
            targets=caps.get("targets", []),
            observation_types=caps.get("observation_types", []),
            max_long_edge=caps.get("max_long_edge"),
        ),
        (welcome.get("server") or {}).get("platform", "linux"),
    )


class AsyncSandbox:
    """An async session against a running ``shinkend``."""

    def __init__(
        self, ws: Any, capabilities: Capabilities, platform: str, record: bool = False
    ) -> None:
        self._ws = ws
        self.capabilities = capabilities
        self.platform_name = platform
        self._seq = 0
        self._recorder = Recorder(platform=platform) if record else None

    def _next_id(self) -> str:
        self._seq += 1
        return f"c{self._seq}"

    async def _rpc(self, msg: dict) -> dict:
        await self._ws.send(json.dumps(msg))
        return json.loads(await self._ws.recv())

    async def ping(self) -> float:
        """Round-trip time to the runtime, in seconds."""
        t0 = time.perf_counter()
        await self._rpc({"type": "ping", "t": t0})
        return time.perf_counter() - t0

    async def query(self, q: str) -> Any:
        reply = await self._rpc({"type": "query", "call_id": self._next_id(), "q": q})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", f"query {q!r} failed"))
        return reply.get("value")

    async def screen_size(self) -> dict:
        return await self.query("screen_size")

    async def act(self, verb: str, target: dict | None = None, **kwargs: Any) -> dict:
        """Send one typed action and await its ack. Raises on failure."""
        action: dict = {"verb": verb}
        if target is not None:
            action["target"] = target
        action.update({k: v for k, v in kwargs.items() if v is not None})
        call_id = self._next_id()
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", f"action {verb!r} failed"))
        if self._recorder is not None:
            self._recorder.action(verb, action, call_id)
        return reply

    async def click(self, target: Any = None, *, x: float | None = None, y: float | None = None):
        return await self.act("click", _target(target, x, y))

    async def double_click(
        self, target: Any = None, *, x: float | None = None, y: float | None = None
    ):
        return await self.act("double_click", _target(target, x, y))

    async def right_click(
        self, target: Any = None, *, x: float | None = None, y: float | None = None
    ):
        return await self.act("right_click", _target(target, x, y))

    async def move(self, target: Any = None, *, x: float | None = None, y: float | None = None):
        return await self.act("move", _target(target, x, y))

    async def scroll(
        self, target: Any = None, *, x: float | None = None, y: float | None = None, dy: float = 0.0
    ):
        return await self.act("scroll", _target(target, x, y), dy=dy)

    async def screenshot(self, scope: str = "screen") -> dict:
        """Capture pixels on demand. Returns {'png': bytes, 'w': int, 'h': int}."""
        reply = await self._rpc(
            {
                "type": "action",
                "call_id": self._next_id(),
                "action": {"verb": "screenshot", "scope": scope},
            }
        )
        if reply.get("type") != "observation":
            raise RuntimeError(reply.get("error", "screenshot failed"))
        img = reply.get("image") or {}
        png = base64.b64decode(img.get("ref", ""))
        if self._recorder is not None:
            self._recorder.observation(
                {"image": {"w": img.get("w"), "h": img.get("h"), "scope": scope}}, png=png
            )
        return {"png": png, "w": img.get("w"), "h": img.get("h")}

    async def type_text(self, text: str):
        return await self.act("type_text", text=text)

    async def key(self, keys: str):
        return await self.act("key", keys=keys)

    def save_replay(self, path: str) -> str:
        """Write the recorded session to a `.skn` bundle. Requires record=True."""
        if self._recorder is None:
            raise RuntimeError("session is not recording; reconnect with record=True")
        return self._recorder.save(path)

    async def close(self) -> None:
        await self._ws.close()


async def aconnect(
    addr: str = DEFAULT_ADDR, record: bool = False, token: str | None = None
) -> AsyncSandbox:
    """Open an async session and complete the ACI handshake."""
    ws = await _ws_connect(_to_uri(addr))
    hello: dict = {"type": "hello", "v": 0, "client": _CLIENT}
    if token:
        hello["token"] = token
    await ws.send(json.dumps(hello))
    welcome = json.loads(await ws.recv())
    try:
        capabilities, platform = _parse_welcome(welcome)
    except Exception:
        await ws.close()
        raise
    return AsyncSandbox(ws, capabilities, platform, record)


class _BackgroundLoop:
    """A dedicated asyncio loop on a daemon thread, powering the sync facade."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="shinken-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any, timeout: float = 30.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


class Sandbox:
    """Synchronous session facade — the elegant one-liner API."""

    def __init__(self, inner: AsyncSandbox, loop: _BackgroundLoop) -> None:
        self._inner = inner
        self._loop = loop
        self._closed = False

    @property
    def capabilities(self) -> Capabilities:
        return self._inner.capabilities

    @property
    def platform(self) -> str:
        return self._inner.platform_name

    def ping(self) -> float:
        return self._loop.run(self._inner.ping())

    def screen_size(self) -> dict:
        return self._loop.run(self._inner.screen_size())

    def act(self, verb: str, target: dict | None = None, **kwargs: Any) -> dict:
        return self._loop.run(self._inner.act(verb, target, **kwargs))

    def click(self, target: Any = None, *, x: float | None = None, y: float | None = None):
        return self._loop.run(self._inner.click(target, x=x, y=y))

    def double_click(self, target: Any = None, *, x: float | None = None, y: float | None = None):
        return self._loop.run(self._inner.double_click(target, x=x, y=y))

    def right_click(self, target: Any = None, *, x: float | None = None, y: float | None = None):
        return self._loop.run(self._inner.right_click(target, x=x, y=y))

    def move(self, target: Any = None, *, x: float | None = None, y: float | None = None):
        return self._loop.run(self._inner.move(target, x=x, y=y))

    def scroll(
        self, target: Any = None, *, x: float | None = None, y: float | None = None, dy: float = 0.0
    ):
        return self._loop.run(self._inner.scroll(target, x=x, y=y, dy=dy))

    def screenshot(self, scope: str = "screen") -> dict:
        return self._loop.run(self._inner.screenshot(scope))

    def type_text(self, text: str):
        return self._loop.run(self._inner.type_text(text))

    def key(self, keys: str):
        return self._loop.run(self._inner.key(keys))

    def save_replay(self, path: str) -> str:
        return self._inner.save_replay(path)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._loop.run(self._inner.close())
        self._loop.stop()

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def connect(addr: str = DEFAULT_ADDR, record: bool = False, token: str | None = None) -> Sandbox:
    """Connect to a running ``shinkend`` and complete the ACI handshake (blocking)."""
    loop = _BackgroundLoop()
    try:
        inner = loop.run(aconnect(addr, record=record, token=token))
    except Exception:
        loop.stop()
        raise
    return Sandbox(inner, loop)
