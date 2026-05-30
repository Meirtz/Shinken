"""ACI v0 client — async core (:class:`AsyncSandbox`) + sync facade (:class:`Sandbox`).

The sync facade runs a dedicated asyncio event loop on a background thread so the
public API can stay a clean one-liner (``env = shinken.connect()``) without leaking
``async``/``await`` into simple scripts.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as _ws_connect

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


class AsyncSandbox:
    """An async session against a running ``shinkend``."""

    def __init__(self, ws: Any, capabilities: Capabilities, platform: str) -> None:
        self._ws = ws
        self.capabilities = capabilities
        self.platform_name = platform
        self._seq = 0

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

    async def close(self) -> None:
        await self._ws.close()


async def aconnect(addr: str = DEFAULT_ADDR) -> AsyncSandbox:
    """Open an async session and complete the ACI handshake."""
    ws = await _ws_connect(_to_uri(addr))
    await ws.send(json.dumps({"type": "hello", "v": 0, "client": _CLIENT}))
    welcome = json.loads(await ws.recv())
    if welcome.get("type") != "welcome":
        await ws.close()
        raise RuntimeError(f"expected 'welcome', got {welcome.get('type')!r}")
    caps = welcome.get("capabilities", {})
    return AsyncSandbox(
        ws,
        Capabilities(
            schema_version=caps.get("schema_version", 0),
            verbs=caps.get("verbs", []),
            targets=caps.get("targets", []),
            observation_types=caps.get("observation_types", []),
            max_long_edge=caps.get("max_long_edge"),
        ),
        welcome.get("server", {}).get("platform", "linux"),
    )


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


class Sandbox:
    """Synchronous session facade — the elegant one-liner API."""

    def __init__(self, inner: AsyncSandbox, loop: _BackgroundLoop) -> None:
        self._inner = inner
        self._loop = loop

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

    def close(self) -> None:
        try:
            self._loop.run(self._inner.close())
        finally:
            self._loop.stop()

    def __enter__(self) -> Sandbox:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def connect(addr: str = DEFAULT_ADDR) -> Sandbox:
    """Connect to a running ``shinkend`` and complete the ACI handshake (blocking)."""
    loop = _BackgroundLoop()
    try:
        inner = loop.run(aconnect(addr))
    except Exception:
        loop.stop()
        raise
    return Sandbox(inner, loop)
