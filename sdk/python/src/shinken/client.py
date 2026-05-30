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
from collections import deque
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as _ws_connect

from .skn import Recorder

__all__ = ["connect", "aconnect", "Sandbox", "AsyncSandbox", "Capabilities"]

DEFAULT_ADDR = "127.0.0.1:8765"
_CLIENT = {"name": "shinken-py", "version": "0.0.1"}

# Sentinel pushed onto the frame queue when the stream/connection ends.
_STREAM_END = object()


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
        # Demux state. A single background reader task consumes every inbound frame
        # and routes it: RPC replies to their pending future (by call_id, or `cause`
        # for a one-shot observation), `pong` to the next ping waiter, and unsolicited
        # server-pushed stream frames (screencast) to the frame queue.
        self._pending: dict[str, asyncio.Future] = {}
        self._pong_waiters: deque[asyncio.Future] = deque()
        self._frames: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    def _next_id(self) -> str:
        self._seq += 1
        return f"c{self._seq}"

    def _start_reader(self) -> None:
        self._reader = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    self._dispatch(json.loads(raw))
                except Exception:
                    continue  # one malformed frame must not kill the reader
        except Exception:
            pass
        finally:
            self._fail_pending(ConnectionError("connection closed"))
            with contextlib.suppress(Exception):
                self._frames.put_nowait(_STREAM_END)

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        # An unsolicited stream frame is never an RPC reply — route it to the queue.
        if kind == "observation" and msg.get("stream") is not None:
            self._frames.put_nowait(msg)
            return
        if kind == "pong":
            while self._pong_waiters:
                fut = self._pong_waiters.popleft()
                if not fut.done():
                    fut.set_result(msg)
                    return
            return
        cid = msg.get("call_id") or msg.get("cause")
        fut = self._pending.pop(cid, None) if cid is not None else None
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
        while self._pong_waiters:
            fut = self._pong_waiters.popleft()
            if not fut.done():
                fut.set_exception(exc)

    async def _rpc(self, msg: dict) -> dict:
        """Send a call_id-bearing message and await its correlated reply."""
        call_id = msg["call_id"]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            self._pending.pop(call_id, None)
            raise
        return await fut

    async def ping(self) -> float:
        """Round-trip time to the runtime, in seconds."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pong_waiters.append(fut)
        t0 = time.perf_counter()
        await self._ws.send(json.dumps({"type": "ping", "t": t0}))
        await fut
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

    async def astart_screencast(
        self, fps: float = 5.0, max_long_edge: int | None = None, scope: str | None = None
    ) -> str:
        """Start a server-pushed screencast; returns its stream id. Frames arrive
        asynchronously and are read with :meth:`next_frame`. ``max_long_edge`` caps
        each frame's longer edge (px) to save bandwidth; ``scope`` selects the capture
        region (``screen``, ``active_window``, or ``window:<id>``)."""
        action: dict = {"verb": "start_screencast", "fps": fps}
        if max_long_edge is not None:
            action["max_long_edge"] = max_long_edge
        if scope is not None:
            action["scope"] = scope
        call_id = self._next_id()
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "start_screencast failed"))
        return call_id

    async def astop_screencast(self) -> None:
        """Ask the runtime to stop streaming frames."""
        call_id = self._next_id()
        with contextlib.suppress(Exception):
            await self._rpc(
                {
                    "type": "action",
                    "call_id": call_id,
                    "action": {"verb": "stop_screencast"},
                }
            )

    async def next_frame(self, timeout: float | None = None) -> dict | None:
        """Await the next screencast frame as {'png', 'w', 'h', 'seq', 'stream'},
        or None if the stream ended or `timeout` seconds elapsed with no frame."""
        try:
            item = (
                await asyncio.wait_for(self._frames.get(), timeout)
                if timeout is not None
                else await self._frames.get()
            )
        except asyncio.TimeoutError:
            return None
        if item is _STREAM_END:
            return None
        img = item.get("image") or {}
        png = base64.b64decode(img.get("ref", ""))
        if self._recorder is not None:
            self._recorder.observation(
                {"image": {"w": img.get("w"), "h": img.get("h"), "scope": "screen"}}, png=png
            )
        return {
            "png": png,
            "w": img.get("w"),
            "h": img.get("h"),
            "seq": item.get("seq"),
            "stream": item.get("stream"),
        }

    def save_replay(self, path: str) -> str:
        """Write the recorded session to a `.skn` bundle. Requires record=True."""
        if self._recorder is None:
            raise RuntimeError("session is not recording; reconnect with record=True")
        return self._recorder.save(path)

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(Exception):
                await self._reader
        await self._ws.close()
        self._fail_pending(ConnectionError("session closed"))


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
    sandbox = AsyncSandbox(ws, capabilities, platform, record)
    sandbox._start_reader()  # the reader owns recv() from here on
    return sandbox


class _BackgroundLoop:
    """A dedicated asyncio loop on a daemon thread, powering the sync facade."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="shinken-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any, timeout: float | None = 30.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        # Cancel lingering loop tasks (e.g. the websockets keepalive) before stopping,
        # so teardown is clean and doesn't warn "Task was destroyed but it is pending".
        async def _drain() -> None:
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


class _Screencast:
    """Synchronous, bounded iterator over screencast frames; stops the stream on exit.

    Usage::

        with env.screencast(fps=10, limit=30) as frames:
            for frame in frames:
                handle(frame["png"], frame["seq"])
    """

    def __init__(
        self,
        sandbox: Sandbox,
        fps: float,
        timeout: float | None,
        limit: int | None,
        max_long_edge: int | None,
        scope: str | None,
    ) -> None:
        self._sb = sandbox
        self._fps = fps
        self._timeout = timeout
        self._limit = limit
        self._max_long_edge = max_long_edge
        self._scope = scope
        self._count = 0
        self._started = False

    def __enter__(self) -> _Screencast:
        self._sb._loop.run(
            self._sb._inner.astart_screencast(self._fps, self._max_long_edge, self._scope)
        )
        self._started = True
        return self

    def __iter__(self) -> _Screencast:
        return self

    def __next__(self) -> dict:
        if self._limit is not None and self._count >= self._limit:
            raise StopIteration
        outer = None if self._timeout is None else self._timeout + 5.0
        frame = self._sb._loop.run(self._sb._inner.next_frame(self._timeout), timeout=outer)
        if frame is None:
            raise StopIteration
        self._count += 1
        return frame

    def __exit__(self, *_exc: object) -> None:
        if self._started:
            with contextlib.suppress(Exception):
                self._sb._loop.run(self._sb._inner.astop_screencast())


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

    def screencast(
        self,
        fps: float = 5.0,
        *,
        timeout: float | None = 30.0,
        limit: int | None = None,
        max_long_edge: int | None = None,
        scope: str | None = None,
    ) -> _Screencast:
        """Stream the screen in real time as a context manager yielding frames::

            with env.screencast(fps=10, limit=30, max_long_edge=720) as frames:
                for frame in frames:
                    ...  # frame: {'png', 'w', 'h', 'seq', 'stream'}

        Frames identical to the previous one are suppressed server-side, so an idle
        screen yields nothing until it changes. ``timeout`` bounds the wait per frame
        (None blocks indefinitely); ``limit`` caps the number of frames;
        ``max_long_edge`` downscales each frame to save bandwidth; ``scope`` selects
        the region (``screen``, ``active_window``, ``window:<id>``).
        """
        return _Screencast(self, fps, timeout, limit, max_long_edge, scope)

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
