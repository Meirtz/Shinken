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
import shutil
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as _ws_connect

from .artifacts import LocalArtifactStore
from .gateway import CapabilityDenied, check_file_transfer, decide_action
from .skn import DEFAULT_CAPABILITIES, Recorder

__all__ = ["connect", "aconnect", "Sandbox", "AsyncSandbox", "Capabilities"]

DEFAULT_ADDR = "127.0.0.1:8765"
_CLIENT = {"name": "shinken-py", "version": "0.0.0"}
_FRAME_QUEUE_MAX = 32

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
        self,
        ws: Any,
        capabilities: Capabilities,
        platform: str,
        record: bool = False,
        sandbox_capabilities: dict | None = None,
        redact_media: bool = False,
        redact_text: bool = False,
        enforce_capabilities: bool = False,
        artifact_root: str | None = None,
        on_ask: Callable[[str, str | None, str], bool] | None = None,
    ) -> None:
        self._ws = ws
        self.capabilities = capabilities
        self.platform_name = platform
        self._seq = 0
        # File/artifact transfer (#85). The M0 transport is a co-located reference store
        # (a directory standing in for the sandbox filesystem); a per-session temp dir is
        # created lazily when no explicit root is given. Over-the-wire transfer is later.
        self._artifact_root = artifact_root
        self._artifacts: LocalArtifactStore | None = None
        self._artifact_tmp: Any = None
        # The session capability envelope (what the Sandbox may do) — reference
        # semantics, recorded into .skn; distinct from the ACI `capabilities` above.
        self.sandbox_capabilities = {**DEFAULT_CAPABILITIES, **(sandbox_capabilities or {})}
        self._enforce = enforce_capabilities  # Action Gateway shim (#84)
        # Approval handler for risky ("ask") capabilities (#7): (verb, cap, reason) -> bool.
        # None means risky steps are denied by default (conservative, still recorded).
        self._on_ask = on_ask
        self._recorder = (
            Recorder(
                platform=platform,
                capabilities=self.sandbox_capabilities,
                redact_media=redact_media,
                redact_text=redact_text,
            )
            if record
            else None
        )
        if self._recorder is not None:
            self._recorder.capability_envelope()  # declare the envelope at session start
        # Demux state. A single background reader task consumes every inbound frame
        # and routes it: RPC replies to their pending future (by call_id, or `cause`
        # for a one-shot observation), `pong` to the next ping waiter, and unsolicited
        # server-pushed stream frames (screencast) to the frame queue.
        self._pending: dict[str, asyncio.Future] = {}
        self._pong_waiters: deque[asyncio.Future] = deque()
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=_FRAME_QUEUE_MAX)
        self._reader: asyncio.Task | None = None
        self._rpc_timeout = 30.0  # per-RPC reply deadline (#142); async callers never hang

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
            self._push_frame(_STREAM_END)

    def _clear_frames(self) -> None:
        """Drop queued stream frames/sentinels before starting a new stream."""
        while True:
            try:
                self._frames.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _push_frame(self, item: object) -> None:
        """Bound the client-side frame queue with drop-oldest semantics."""
        if self._frames.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._frames.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._frames.put_nowait(item)

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        # An unsolicited stream frame is never an RPC reply — route it to the queue.
        if kind == "observation" and msg.get("stream") is not None:
            self._push_frame(msg)
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

    async def _rpc(self, msg: dict, timeout: float | None = None) -> dict:
        """Send a call_id-bearing message and await its correlated reply, bounded by a
        timeout (#142) so a missing, dropped, or uncorrelated reply can't hang an async
        caller; the pending future is also failed on connection close (``_fail_pending``)."""
        timeout = self._rpc_timeout if timeout is None else timeout
        call_id = msg["call_id"]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = fut
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            self._pending.pop(call_id, None)
            raise
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(call_id, None)
            raise TimeoutError(
                f"RPC {msg.get('type')!r} ({call_id}) timed out after {timeout}s"
            ) from None

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

    def _gate(self, verb: str) -> None:
        """Action Gateway check (#84/#7): when enforcing, decide allow / deny / **ask**
        before dispatch — so a denied or unapproved verb never reaches shinkend — and
        record the decision. A capability valued ``"ask"`` is *risky*: it pauses for
        approval via the ``on_ask`` handler (default: deny), and the resolution is
        recorded as a ``permission:ask`` event. GUI input passes when input_automation is
        granted (the default)."""
        if not self._enforce:
            return
        decision, cap, reason = decide_action(verb, self.sandbox_capabilities)
        if decision == "allow":
            return
        if decision == "ask":
            granted = bool(self._on_ask(verb, cap, reason)) if self._on_ask else False
            if self._recorder is not None:
                self._recorder.permission(
                    {
                        "decision": "ask",
                        "capability": cap,
                        "verb": verb,
                        "reason": reason,
                        "resolution": "grant" if granted else "deny",
                    }
                )
            if not granted:
                raise CapabilityDenied(f"{verb}: approval denied ({cap})")
            return
        if self._recorder is not None:  # decision == "deny"
            self._recorder.permission(
                {"decision": "deny", "capability": cap, "verb": verb, "reason": reason}
            )
        raise CapabilityDenied(f"{verb}: {reason}")

    def gate_capability(self, cap_name: str, label: str) -> None:
        """Gateway check for a *named* capability (e.g. ``a11y`` for structured
        observation, #145) — allow / deny / ask with the same recording semantics as
        :meth:`_gate` does for verbs. No-op unless enforcing."""
        if not self._enforce:
            return
        val = self.sandbox_capabilities.get(cap_name, False)
        if isinstance(val, str) and val.lower() == "ask":
            granted = (
                bool(self._on_ask(label, cap_name, "requires approval")) if self._on_ask else False
            )
            if self._recorder is not None:
                self._recorder.permission(
                    {
                        "decision": "ask",
                        "capability": cap_name,
                        "verb": label,
                        "reason": f"capability '{cap_name}' requires approval",
                        "resolution": "grant" if granted else "deny",
                    }
                )
            if not granted:
                raise CapabilityDenied(f"{label}: approval denied ({cap_name})")
            return
        if val:
            return
        reason = f"capability '{cap_name}' not granted"
        if self._recorder is not None:
            self._recorder.permission(
                {"decision": "deny", "capability": cap_name, "verb": label, "reason": reason}
            )
        raise CapabilityDenied(f"{label}: {reason}")

    def record_structured(self, obs: dict, *, diff: bool = False) -> None:
        """Record a structured (a11y/CDP) observation into the `.skn` when recording (#144).

        The structured path is co-located (it doesn't traverse shinkend), so it is
        recorded here. ``diff=True`` records an ``observe_diff`` payload; otherwise the
        full element list. No-op when not recording or the capture was unavailable."""
        if self._recorder is None or not obs.get("available"):
            return
        if diff:
            payload = {
                "tree": "diff",
                "added": obs.get("added", []),
                "removed": obs.get("removed", []),
                "changed": obs.get("changed", []),
                "unchanged": obs.get("unchanged", 0),
                "size": obs.get("size"),
            }
        else:
            payload = {
                "tree": obs.get("tree", "full"),
                "elements": obs.get("elements", []),
                "node_count": obs.get("node_count", 0),
            }
        self._recorder.observation(payload)

    async def act(
        self, verb: str, target: dict | None = None, *, _batch_id: str | None = None, **kwargs: Any
    ) -> dict:
        """Send one typed action and await its ack. Raises on failure. ``_batch_id`` tags
        the recorded action event when dispatched as part of a batch (#73)."""
        self._gate(verb)
        action: dict = {"verb": verb}
        if target is not None:
            action["target"] = target
        action.update({k: v for k, v in kwargs.items() if v is not None})
        call_id = self._next_id()
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", f"action {verb!r} failed"))
        if self._recorder is not None:
            self._recorder.action(verb, action, call_id, batch_id=_batch_id)
        return reply

    async def act_batch(
        self, actions: list[dict], *, stop_on_error: bool = True, batch_id: str | None = None
    ) -> dict:
        """Execute an ordered batch of ACI actions serially (#73).

        Each action is dispatched in order and recorded as its own ``.skn`` event sharing
        ``batch_id`` (per-action ``action_id`` is preserved for observation pairing).
        With ``stop_on_error`` (default) the batch halts at the first failing action and
        returns explicit partial state; otherwise it runs them all. Returns
        ``{batch_id, completed, stopped_at?, results:[{index, verb, ok, action_id/ack/error}]}``."""
        bid = batch_id or f"batch-{uuid.uuid4().hex[:8]}"
        results: list[dict] = []
        for i, a in enumerate(actions):
            verb = a.get("verb")
            target = a.get("target")
            rest = {k: v for k, v in a.items() if k not in ("verb", "target")}
            try:
                if not verb:
                    raise ValueError("action has no 'verb'")
                reply = await self.act(verb, target, _batch_id=bid, **rest)
                results.append(
                    {
                        "index": i,
                        "verb": verb,
                        "ok": True,
                        "action_id": reply.get("call_id") or reply.get("cause"),
                        "ack": reply,
                    }
                )
            except Exception as exc:
                results.append({"index": i, "verb": verb, "ok": False, "error": str(exc)})
                if stop_on_error:
                    return {
                        "batch_id": bid,
                        "completed": False,
                        "stopped_at": i,
                        "results": results,
                    }
        return {"batch_id": bid, "completed": True, "results": results}

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
        self._gate("screenshot")
        call_id = self._next_id()
        reply = await self._rpc(
            {"type": "action", "call_id": call_id, "action": {"verb": "screenshot", "scope": scope}}
        )
        if reply.get("type") != "observation":
            raise RuntimeError(reply.get("error", "screenshot failed"))
        img = reply.get("image") or {}
        png = base64.b64decode(img.get("ref", ""))
        if self._recorder is not None:
            # record the screenshot as an action + pair the observation to it (#160)
            self._recorder.action("screenshot", {"verb": "screenshot", "scope": scope}, call_id)
            self._recorder.observation(
                {"image": {"w": img.get("w"), "h": img.get("h"), "scope": scope}},
                png=png,
                action_id=call_id,
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
        self._gate("start_screencast")
        self._clear_frames()
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
        self._clear_frames()
        self._push_frame(_STREAM_END)

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

    def record_permission(
        self, decision: str, capability: str | None = None, **fields: Any
    ) -> None:
        """Record a boundary decision (grant / deny / narrow) as a `.skn` permission
        event. Reference semantics for replay/audit; requires record=True."""
        if self._recorder is None:
            raise RuntimeError("session is not recording; reconnect with record=True")
        payload = {"decision": decision}
        if capability is not None:
            payload["capability"] = capability
        payload.update(fields)
        self._recorder.permission(payload)

    def record_verifier_receipt(self, receipt: dict) -> bool:
        """Record an eval verifier receipt as a first-class `.skn` event (#149). Returns
        True if it was recorded, False when the session isn't recording (no-op)."""
        if self._recorder is None:
            return False
        self._recorder.verifier_receipt(receipt)
        return True

    def save_replay(self, path: str) -> str:
        """Write the recorded session to a `.skn` bundle. Requires record=True."""
        if self._recorder is None:
            raise RuntimeError("session is not recording; reconnect with record=True")
        return self._recorder.save(path)

    def _artifact_store(self) -> LocalArtifactStore:
        if self._artifacts is None:
            root = self._artifact_root
            if root is None:
                self._artifact_tmp = tempfile.mkdtemp(prefix="shinken-artifacts-")
                root = self._artifact_tmp
            self._artifacts = LocalArtifactStore(root)
        return self._artifacts

    def _gate_file(self, direction: str, sandbox_path: str) -> str:
        """Capability gate for file transfer (#85). When enforcing, deny if ``fs_scope``
        is not granted (recording the decision); returns the effective scope string."""
        scope = self.sandbox_capabilities.get("fs_scope", "session")
        if self._enforce:
            allowed, cap, reason = check_file_transfer(direction, self.sandbox_capabilities)
            if not allowed:
                if self._recorder is not None:
                    self._recorder.permission(
                        {"decision": "deny", "capability": cap, "verb": direction, "reason": reason}
                    )
                raise CapabilityDenied(f"{direction}: {reason}")
        return str(scope)

    def put_file(self, local_path: str, sandbox_path: str, *, archive: bool = False) -> dict:
        """Copy a host file into the sandbox, hash it, and record the transfer (#85).

        Returns the artifact ref (path / sha256 / size / scope / direction). With
        ``archive=True`` the bytes are content-addressed into the `.skn` media store so a
        replay can reproduce the file; by default only the ref is recorded."""
        scope = self._gate_file("put_file", sandbox_path)
        ref = self._artifact_store().put(local_path, sandbox_path, scope=scope)
        if self._recorder is not None:
            data = self._artifact_store().read_bytes(sandbox_path) if archive else None
            self._recorder.file_transfer(ref.to_event(), data=data)
        return ref.to_event()

    def get_file(
        self, sandbox_path: str, local_path: str, *, expect_sha256: str | None = None
    ) -> dict:
        """Copy a file out of the sandbox to the host, verifying its content hash (#85).

        Raises :class:`~shinken.artifacts.HashMismatch` if ``expect_sha256`` is given and
        the fetched content does not match. Records the transfer when recording."""
        scope = self._gate_file("get_file", sandbox_path)
        ref = self._artifact_store().get(
            sandbox_path, local_path, expect_sha256=expect_sha256, scope=scope
        )
        if self._recorder is not None:
            self._recorder.file_transfer(ref.to_event())
        return ref.to_event()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(Exception):
                await self._reader
        await self._ws.close()
        self._fail_pending(ConnectionError("session closed"))
        if self._artifact_tmp is not None:  # clean the per-session temp store, if we made one
            with contextlib.suppress(Exception):
                shutil.rmtree(self._artifact_tmp, ignore_errors=True)
            self._artifact_tmp = None


async def aconnect(
    addr: str = DEFAULT_ADDR,
    record: bool = False,
    token: str | None = None,
    sandbox_capabilities: dict | None = None,
    redact_media: bool = False,
    redact_text: bool = False,
    enforce_capabilities: bool = False,
    artifact_root: str | None = None,
    on_ask: Callable[[str, str | None, str], bool] | None = None,
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
    sandbox = AsyncSandbox(
        ws,
        capabilities,
        platform,
        record,
        sandbox_capabilities,
        redact_media,
        redact_text,
        enforce_capabilities,
        artifact_root,
        on_ask,
    )
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
        self._elements: dict[str, dict] = {}  # ref -> Element, from the last observe(structured)
        self._last_structured: list[dict] = []  # last full element list, for observe_diff (#4)

    @property
    def capabilities(self) -> Capabilities:
        return self._inner.capabilities

    @property
    def platform(self) -> str:
        return self._inner.platform_name

    @property
    def sandbox_capabilities(self) -> dict:
        """The session capability envelope (what this Sandbox is permitted to do —
        reference semantics, recorded into `.skn`)."""
        return self._inner.sandbox_capabilities

    def record_permission(
        self, decision: str, capability: str | None = None, **fields: Any
    ) -> None:
        """Record a boundary decision (grant / deny / narrow) into the `.skn` replay."""
        self._inner.record_permission(decision, capability, **fields)

    def record_verifier_receipt(self, receipt: dict) -> bool:
        """Record an eval verifier receipt as a first-class `.skn` event (#149); returns
        True if recorded (no-op + False when not recording)."""
        return self._inner.record_verifier_receipt(receipt)

    def ping(self) -> float:
        return self._loop.run(self._inner.ping())

    def screen_size(self) -> dict:
        return self._loop.run(self._inner.screen_size())

    def act(self, verb: str, target: dict | None = None, **kwargs: Any) -> dict:
        return self._loop.run(self._inner.act(verb, target, **kwargs))

    def act_batch(
        self, actions: list[dict], *, stop_on_error: bool = True, batch_id: str | None = None
    ) -> dict:
        """Execute an ordered batch of ACI actions serially with per-action replay
        events (#73). Pairs naturally with the CU adapters, which emit ordered batches."""
        return self._loop.run(
            self._inner.act_batch(actions, stop_on_error=stop_on_error, batch_id=batch_id)
        )

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

    def observe(self, structured: bool = False, source: Any = None) -> dict:
        """Observe the desktop. ``structured=True`` captures the accessibility tree as
        ACI ``Element``s (full tree + node_count + capture_ms), falling back gracefully
        with ``available=False`` if AT-SPI is unavailable; otherwise returns a
        screenshot observation. The structured path is a **co-located / local-reference**
        capture (AT-SPI on this host); the over-the-wire shinkend path is a follow-up."""
        if structured:
            obs = self._capture_structured(source)
            self._inner.record_structured(obs, diff=False)  # write the full tree to .skn (#144)
            return obs
        shot = self.screenshot()
        return {
            "type": "observation",
            "tree": "full",
            "image": {"w": shot["w"], "h": shot["h"], "scope": "screen"},
            "png": shot["png"],
        }

    def _capture_structured(self, source: Any = None) -> dict:
        """Gate the ``a11y`` capability (#145), capture a structured observation, and
        retain its ``ref → Element`` map (#78). Recording is done by the caller so a full
        capture and a diff capture each record the right shape (no double-recording)."""
        from .a11y import AtspiSource, observe_structured

        self._inner.gate_capability("a11y", "observe_structured")
        obs = observe_structured(source or AtspiSource())
        self._elements = {e["ref"]: e for e in obs.get("elements", [])}
        return obs

    def observe_diff(self, source: Any = None) -> dict:
        """Structured observation as a **diff** from the previous one (#4 / D3): returns
        ``{added, removed, changed, unchanged, size, available}`` — only what changed
        since the last ``observe_diff`` / ``observe(structured=True)``, plus the
        serialized diff-vs-full byte sizes (the tree-diff bandwidth measure). The first
        call has no baseline, so every element is ``added``. Falls back to
        ``available=False`` (like the full structured path) when the source is
        unavailable. Also refreshes the ``element_ref`` map, so ``resolve``/``act_on``
        keep working."""
        from .a11y import diff_elements, diff_size

        obs = self._capture_structured(source)
        if not obs.get("available", False):
            return {
                "available": False,
                "added": [],
                "removed": [],
                "changed": [],
                "unchanged": 0,
                "error": obs.get("error"),
            }
        curr = obs.get("elements", [])
        diff = diff_elements(self._last_structured, curr)
        diff["size"] = diff_size(diff, curr)
        diff["available"] = True
        self._last_structured = curr
        self._inner.record_structured(diff, diff=True)  # write the diff to .skn (#144)
        return diff

    def resolve(self, ref: str) -> dict:
        """Resolve an `element_ref` (from the last ``observe(structured=True)``) to a
        click point — the centre of its bounding box. Raises ``KeyError`` on an unknown
        ref and ``ValueError`` if the element has no usable bbox."""
        el = self._elements.get(ref)
        if el is None:
            raise KeyError(f"unknown element_ref {ref!r}; call observe(structured=True) first")
        x, y, w, h = el.get("bbox") or (0, 0, 0, 0)
        if w <= 0 or h <= 0:
            raise ValueError(f"element_ref {ref!r} has no usable bounding box")
        return {"x": x + w // 2, "y": y + h // 2, "element": el}

    def act_on(self, ref: str, verb: str = "click", **kwargs: Any) -> dict:
        """Semantic action routing: resolve an `element_ref` to a point via the last
        structured observation, then dispatch ``verb`` there as a typed pixel action —
        the structured fast path (no raw coordinates from the caller)."""
        point = self.resolve(ref)
        return self.act(verb, {"kind": "point_px", "x": point["x"], "y": point["y"]}, **kwargs)

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

    def put_file(self, local_path: str, sandbox_path: str, *, archive: bool = False) -> dict:
        """Upload a host file into the sandbox; return its content-addressed ref (#85)."""
        return self._inner.put_file(local_path, sandbox_path, archive=archive)

    def get_file(
        self, sandbox_path: str, local_path: str, *, expect_sha256: str | None = None
    ) -> dict:
        """Download a sandbox file to the host, verifying its content hash (#85)."""
        return self._inner.get_file(sandbox_path, local_path, expect_sha256=expect_sha256)

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


def connect(
    addr: str = DEFAULT_ADDR,
    record: bool = False,
    token: str | None = None,
    sandbox_capabilities: dict | None = None,
    redact_media: bool = False,
    redact_text: bool = False,
    enforce_capabilities: bool = False,
    artifact_root: str | None = None,
    on_ask: Callable[[str, str | None, str], bool] | None = None,
) -> Sandbox:
    """Connect to a running ``shinkend`` and complete the ACI handshake (blocking).

    ``sandbox_capabilities`` overrides the session's v0 capability envelope (recorded
    into the `.skn` replay as reference semantics). For sensitive runs, ``redact_media``
    drops captured screenshot bytes and ``redact_text`` strips typed text from the
    recording (#88). With ``enforce_capabilities``, the local Action Gateway shim (#84)
    denies actions whose capability the envelope does not grant, before dispatch; a
    capability valued ``"ask"`` is *risky* and pauses for approval via ``on_ask`` (#7)."""
    loop = _BackgroundLoop()
    try:
        inner = loop.run(
            aconnect(
                addr,
                record=record,
                token=token,
                sandbox_capabilities=sandbox_capabilities,
                redact_media=redact_media,
                redact_text=redact_text,
                enforce_capabilities=enforce_capabilities,
                artifact_root=artifact_root,
                on_ask=on_ask,
            )
        )
    except Exception:
        loop.stop()
        raise
    return Sandbox(inner, loop)
