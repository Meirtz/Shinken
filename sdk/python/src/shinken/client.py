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
import logging
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
from websockets.exceptions import ConnectionClosed as _WsConnectionClosed

from .artifacts import LocalArtifactStore
from .errors import classify_exception
from .gateway import CapabilityDenied, check_file_transfer, decide_action

__all__ = ["connect", "aconnect", "Sandbox", "AsyncSandbox", "Capabilities"]

_log = logging.getLogger("shinken.client")

DEFAULT_ADDR = "127.0.0.1:8765"
_CLIENT = {"name": "shinken-py", "version": "0.0.0"}
_FRAME_QUEUE_MAX = 32
# Bound inbound WebSocket frames (#136): generous enough for 4K screenshots / large a11y
# trees, but not unbounded — a malformed or hostile peer can't force unbounded buffering.
# (websockets' 1 MiB default would also reject legitimate large screenshots.)
_MAX_WS_MESSAGE = 16 * 1024 * 1024
# Bound the handshake recv so aconnect() can't hang forever against a server that accepts
# the socket but never sends `welcome`.
_HANDSHAKE_TIMEOUT = 10.0

# Sentinels pushed onto the frame queue. _STREAM_END = the stream stopped cleanly (server
# stop or astop_screencast); _STREAM_LOST = the connection died mid-stream, so a consumer
# can tell silent truncation from a normal end.
_STREAM_END = object()
_STREAM_LOST = object()

# v0 session capability envelope — reference semantics for the local gateway shim,
# not a production policy engine.
DEFAULT_SANDBOX_CAPABILITIES: dict[str, Any] = {
    "input_automation": True,
    "screenshot": True,
    "a11y": True,
    "fs_scope": "session",
    "egress": False,
    "credentials": False,
    "clipboard": False,
    "privileged_install": False,
}


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
        # shinkend rejects a bad/missing token with a `result` (ok=false) then closes.
        # Surface its actionable reason instead of a cryptic type mismatch.
        if welcome.get("type") == "result" and welcome.get("ok") is False:
            raise RuntimeError(
                f"handshake rejected by server: {welcome.get('error', 'unknown error')}"
            )
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
        sandbox_capabilities: dict | None = None,
        enforce_capabilities: bool | None = None,
        artifact_root: str | None = None,
        on_ask: Callable[[str, str | None, str], bool] | None = None,
    ) -> None:
        self._ws = ws
        self.capabilities = capabilities
        self.platform_name = platform
        self._seq = 0
        # Count of successfully-acked ACI actions this session. Read as a delta around a
        # task's run() to give the eval harness a real step count (#86/#87) without
        # coupling to any backend — every dispatched action flows through act().
        self._action_count = 0
        # File/artifact transfer (#85). The M0 transport is a co-located reference store
        # (a directory standing in for the sandbox filesystem); a per-session temp dir is
        # created lazily when no explicit root is given. Over-the-wire transfer is later.
        self._artifact_root = artifact_root
        self._artifacts: LocalArtifactStore | None = None
        self._artifact_tmp: Any = None
        # A guest-boundary transfer backend (e.g. Docker `cp`) a provider may attach so
        # put_file/get_file move bytes through the real Sandbox FS, not the reference
        # store (#154). None → fall back to the co-located LocalArtifactStore.
        self._guest_transport: Any = None
        # The managing provider + handle, attached by provider.connect() so checkpoint()
        # can snapshot substrate state. None for a plain connect() with no provider behind it.
        self._provider: Any = None
        self._handle: Any = None
        # The session capability envelope (what the Sandbox may do) — reference semantics
        # for the local gateway shim, distinct from the ACI `capabilities` above.
        self.sandbox_capabilities = {
            **DEFAULT_SANDBOX_CAPABILITIES,
            **(sandbox_capabilities or {}),
        }
        # Action Gateway shim (#84/#161). Enforcement is a *client-side reference*
        # boundary, not a security guarantee (a direct WebSocket client bypasses it;
        # true enforcement is the server-side Action Gateway, D6).
        self._enforce = bool(enforce_capabilities)
        # Approval handler for risky ("ask") capabilities (#7): (verb, cap, reason) -> bool.
        # None means risky steps are denied by default.
        self._on_ask = on_ask
        # Demux state. A single background reader task consumes every inbound frame
        # and routes it: RPC replies to their pending future (by call_id, or `cause`
        # for a one-shot observation), `pong` to the next ping waiter, and unsolicited
        # server-pushed stream frames (screencast) to the frame queue.
        self._pending: dict[str, asyncio.Future] = {}
        self._pong_waiters: deque[asyncio.Future] = deque()
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=_FRAME_QUEUE_MAX)
        self._reader: asyncio.Task | None = None
        self._rpc_timeout = 30.0  # per-RPC reply deadline (#142); async callers never hang
        self._stream_scope = "screen"  # capture region of the active screencast (#143)

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
                    # one malformed frame must not kill the reader — but make it visible
                    _log.debug("dropping malformed/undispatchable frame", exc_info=True)
                    continue
        except Exception:
            _log.debug("read loop terminated by connection error", exc_info=True)
        finally:
            self._fail_pending(ConnectionError("connection closed"))
            # The connection ended: signal LOST so a mid-stream drop is distinguishable
            # from a clean stop (which pushes _STREAM_END via astop_screencast).
            self._push_frame(_STREAM_LOST)

    def _discard_pong_waiter(self, fut: asyncio.Future) -> None:
        """Remove a pong waiter from the deque (on timeout/send failure) so a stale future
        can't later swallow a live ping's pong and misattribute RTT."""
        with contextlib.suppress(ValueError):
            self._pong_waiters.remove(fut)

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
            return
        # No pending future matched. shinkend emits protocol-error frames with call_id "?"
        # (e.g. "bad message", "already authenticated"); surface those rather than swallow.
        if kind == "result" and msg.get("ok") is False:
            _log.warning("uncorrelated server error frame: %s", msg.get("error"))
        else:
            _log.debug("dropping uncorrelated frame (type=%s, call_id=%s)", kind, cid)

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
        except _WsConnectionClosed as exc:
            # Normalize the library's send-on-dead-socket exception (NOT a ConnectionError
            # subclass) at the transport boundary, mirroring _read_loop's wrap — so the
            # death-while-idle timing (sandbox dies between actions; the NEXT send fails)
            # classifies as connection loss everywhere downstream (#56 taxonomy).
            self._pending.pop(call_id, None)
            raise ConnectionError(f"connection closed sending {msg.get('type')!r}") from exc
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
        """Round-trip time to the runtime, in seconds. Bounded by the RPC timeout (#142),
        so an async caller can't hang forever if the server never pongs."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pong_waiters.append(fut)
        t0 = time.perf_counter()
        try:
            await self._ws.send(json.dumps({"type": "ping", "t": t0}))
            await asyncio.wait_for(fut, self._rpc_timeout)
        except asyncio.TimeoutError:
            self._discard_pong_waiter(fut)
            raise TimeoutError(f"ping timed out after {self._rpc_timeout}s") from None
        except _WsConnectionClosed as exc:
            self._discard_pong_waiter(fut)
            raise ConnectionError("connection closed sending 'ping'") from exc
        except Exception:
            self._discard_pong_waiter(fut)
            raise
        return time.perf_counter() - t0

    async def query(self, q: str) -> Any:
        reply = await self._rpc({"type": "query", "call_id": self._next_id(), "q": q})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", f"query {q!r} failed"))
        return reply.get("value")

    async def screen_size(self) -> dict:
        return await self.query("screen_size")

    @property
    def actions_dispatched(self) -> int:
        """Number of ACI actions successfully dispatched (and acked) this session."""
        return self._action_count

    def _gate(self, verb: str) -> None:
        """Action Gateway check (#84/#7): when enforcing, decide allow / deny / **ask**
        before dispatch — so a denied or unapproved verb never reaches shinkend. A
        capability valued ``"ask"`` is *risky*: it pauses for approval via the ``on_ask``
        handler (default: deny). GUI input passes when input_automation is granted (the
        default)."""
        if not self._enforce:
            return
        decision, cap, reason = decide_action(verb, self.sandbox_capabilities)
        if decision == "allow":
            return
        if decision == "ask":
            granted = bool(self._on_ask(verb, cap, reason)) if self._on_ask else False
            if not granted:
                raise CapabilityDenied(f"{verb}: approval denied ({cap})")
            return
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
            if not granted:
                raise CapabilityDenied(f"{label}: approval denied ({cap_name})")
            return
        if val:
            return
        reason = f"capability '{cap_name}' not granted"
        raise CapabilityDenied(f"{label}: {reason}")

    async def act(self, verb: str, target: dict | None = None, **kwargs: Any) -> dict:
        """Send one typed action and await its reply. Raises on failure."""
        self._gate(verb)
        action: dict = {"verb": verb}
        if target is not None:
            action["target"] = target
        action.update({k: v for k, v in kwargs.items() if v is not None})
        call_id = self._next_id()
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        # A successful one-shot `screenshot` is answered with an `observation` (which has
        # no `ok` field), not an `ack` — so an adapter-translated model screenshot
        # tool-call routed through act()/act_batch() must not be treated as a failure.
        ok = True if reply.get("type") == "observation" else reply.get("ok")
        if not ok:
            raise RuntimeError(reply.get("error", f"action {verb!r} failed"))
        self._action_count += 1
        return reply

    async def act_batch(
        self, actions: list[dict], *, stop_on_error: bool = True, batch_id: str | None = None
    ) -> dict:
        """Execute an ordered batch of ACI actions serially (#73).

        Each action is dispatched in order. The returned result preserves per-action
        acknowledgements and the batch id for callers that need traceability. Every action
        row carries a typed ``status`` (#56) — ``ok | error | timeout | skipped |
        sandbox_died`` — so a consumer branches on infrastructure death vs a per-action
        error without string-matching. With ``stop_on_error`` (default) the batch halts at
        the first failing action, marks the remaining actions ``skipped``, and sets
        ``failure_kind`` to the failing action's status; otherwise it runs them all. Returns
        ``{batch_id, completed, stopped_at?, failure_kind?, results:[{index, verb, ok,
        status, action_id/ack/error}]}``."""
        bid = batch_id or f"batch-{uuid.uuid4().hex[:8]}"
        results: list[dict] = []
        for i, a in enumerate(actions):
            verb = a.get("verb")
            target = a.get("target")
            rest = {k: v for k, v in a.items() if k not in ("verb", "target")}
            try:
                if not verb:
                    raise ValueError("action has no 'verb'")
                reply = await self.act(verb, target, **rest)
                results.append(
                    {
                        "index": i,
                        "verb": verb,
                        "ok": True,
                        "status": "ok",
                        "action_id": reply.get("call_id") or reply.get("cause"),
                        "ack": reply,
                    }
                )
            except Exception as exc:
                status = classify_exception(exc)
                results.append(
                    {"index": i, "verb": verb, "ok": False, "status": status, "error": str(exc)}
                )
                # A dead sandbox is batch-fatal regardless of stop_on_error: dispatching the
                # rest would only pile up failed sends against a gone connection.
                if stop_on_error or status == "sandbox_died":
                    for j in range(i + 1, len(actions)):
                        results.append(
                            {
                                "index": j,
                                "verb": actions[j].get("verb"),
                                "ok": False,
                                "status": "skipped",
                                "error": "skipped: a prior action in the batch failed",
                            }
                        )
                    return {
                        "batch_id": bid,
                        "completed": False,
                        "stopped_at": i,
                        "failure_kind": status,
                        "results": results,
                    }
        # continue-mode batch finished; surface a failure_kind if any action failed.
        failed = next((r["status"] for r in results if not r["ok"]), None)
        return {
            "batch_id": bid,
            "completed": True,
            "failure_kind": failed,
            "results": results,
        }

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
        # Remember the requested capture region so recorded frames carry the true
        # scope, not a hardcoded "screen" (#143). Frames also echo it from shinkend.
        self._stream_scope = scope or "screen"
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
        or None if the stream ended cleanly or `timeout` seconds elapsed with no frame.
        Raises :class:`ConnectionError` if the connection dropped mid-stream, so silent
        truncation is distinguishable from a normal end."""
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
        if item is _STREAM_LOST:
            raise ConnectionError("connection lost mid-screencast")
        img = item.get("image") or {}
        png = base64.b64decode(img.get("ref", ""))
        return {
            "png": png,
            "w": img.get("w"),
            "h": img.get("h"),
            "seq": item.get("seq"),
            "stream": item.get("stream"),
        }

    def _artifact_store(self) -> LocalArtifactStore:
        if self._artifacts is None:
            root = self._artifact_root
            if root is None:
                self._artifact_tmp = tempfile.mkdtemp(prefix="shinken-artifacts-")
                root = self._artifact_tmp
            self._artifacts = LocalArtifactStore(root)
        return self._artifacts

    def _set_guest_transport(self, transport: Any) -> None:
        """Attach a guest-boundary transfer backend (provider-supplied, e.g. Docker `cp`,
        #154) so put_file/get_file move bytes through the real Sandbox filesystem."""
        self._guest_transport = transport

    def _set_provider_context(self, provider: Any, handle: Any) -> None:
        """Attach the managing provider + handle (set by provider.connect())."""
        self._provider = provider
        self._handle = handle

    async def checkpoint(
        self, name: str | None = None, *, agent_state_ref: str | None = None
    ) -> str:
        """Create a runtime checkpoint of this sandbox (#206).

        The provider snapshots substrate state and returns the checkpoint id.

        Requires a provider-managed session (use a provider's ``connect()``)."""
        if self._provider is None or self._handle is None:
            raise RuntimeError(
                "checkpoint() needs a provider-managed session; open it via a provider's connect()"
            )
        checkpoint_id = await asyncio.to_thread(
            self._provider.checkpoint,
            self._handle,
            name=name,
            event_seq=None,
            agent_state_ref=agent_state_ref,
        )
        return checkpoint_id

    async def fork(self) -> Any:
        """Fork the current provider-managed sandbox and return the new provider handle."""
        if self._provider is None or self._handle is None:
            raise RuntimeError("fork() needs a provider-managed session")
        return await asyncio.to_thread(self._provider.fork, self._handle)

    async def resume(self, handle_or_checkpoint: Any) -> Any:
        """Resume a provider handle or checkpoint id and return a live provider handle."""
        if self._provider is None:
            raise RuntimeError("resume() needs a provider-managed session")
        return await asyncio.to_thread(self._provider.resume, handle_or_checkpoint)

    def _transfer(self) -> Any:
        """The active file-transfer backend: a provider-attached guest transport (#154)
        if present, else the co-located reference store (#85). Both share put/get."""
        return self._guest_transport or self._artifact_store()

    def _gate_file(self, direction: str, sandbox_path: str) -> str:
        """Capability gate for file transfer (#85). When enforcing, deny if ``fs_scope``
        is not granted; returns the effective scope string."""
        scope = self.sandbox_capabilities.get("fs_scope", "session")
        if self._enforce:
            allowed, cap, reason = check_file_transfer(direction, self.sandbox_capabilities)
            if not allowed:
                raise CapabilityDenied(f"{direction}: {reason}")
        return str(scope)

    def put_file(self, local_path: str, sandbox_path: str) -> dict:
        """Copy a host file into the sandbox, hash it, and record the transfer (#85).

        Returns the artifact ref (path / sha256 / size / scope / direction)."""
        scope = self._gate_file("put_file", sandbox_path)
        ref = self._transfer().put(local_path, sandbox_path, scope=scope)
        return ref.to_event()

    def get_file(
        self, sandbox_path: str, local_path: str, *, expect_sha256: str | None = None
    ) -> dict:
        """Copy a file out of the sandbox to the host, verifying its content hash (#85).

        Raises :class:`~shinken.artifacts.HashMismatch` if ``expect_sha256`` is given and
        the fetched content does not match."""
        scope = self._gate_file("get_file", sandbox_path)
        ref = self._transfer().get(
            sandbox_path, local_path, expect_sha256=expect_sha256, scope=scope
        )
        return ref.to_event()

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            # awaiting a cancelled task raises CancelledError (a BaseException, so plain
            # suppress(Exception) misses it) — suppress it explicitly (#155)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._fail_pending(ConnectionError("session closed"))
        if self._artifact_tmp is not None:  # clean the per-session temp store, if we made one
            with contextlib.suppress(Exception):
                shutil.rmtree(self._artifact_tmp, ignore_errors=True)
            self._artifact_tmp = None


async def aconnect(
    addr: str = DEFAULT_ADDR,
    token: str | None = None,
    sandbox_capabilities: dict | None = None,
    enforce_capabilities: bool | None = None,
    artifact_root: str | None = None,
    on_ask: Callable[[str, str | None, str], bool] | None = None,
) -> AsyncSandbox:
    """Open an async session and complete the ACI handshake."""
    ws = await _ws_connect(_to_uri(addr), max_size=_MAX_WS_MESSAGE)
    hello: dict = {"type": "hello", "v": 0, "client": _CLIENT}
    if token:
        hello["token"] = token
    try:
        await ws.send(json.dumps(hello))
        # Bound the welcome read so a server that accepts the socket but never replies
        # can't hang aconnect() forever.
        welcome = json.loads(await asyncio.wait_for(ws.recv(), _HANDSHAKE_TIMEOUT))
        capabilities, platform = _parse_welcome(welcome)
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close()
        raise
    sandbox = AsyncSandbox(
        ws,
        capabilities,
        platform,
        sandbox_capabilities=sandbox_capabilities,
        enforce_capabilities=enforce_capabilities,
        artifact_root=artifact_root,
        on_ask=on_ask,
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

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        # No outer ceiling by default: liveness comes from the inner per-RPC timeout
        # (#142) and connection-close fan-out (_fail_pending), so legitimately long
        # operations (multi-`wait` batches, docker-commit checkpoint/fork/resume) are
        # not severed mid-flight while the coroutine keeps running detached.
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
        reference semantics for the local gateway shim)."""
        return self._inner.sandbox_capabilities

    def ping(self) -> float:
        return self._loop.run(self._inner.ping())

    def screen_size(self) -> dict:
        return self._loop.run(self._inner.screen_size())

    def query(self, q: str) -> Any:
        """Ask the runtime for a property (e.g. ``platform``, ``screen_size``). Used by
        eval verifiers to read observed environment state."""
        return self._loop.run(self._inner.query(q))

    @property
    def actions_dispatched(self) -> int:
        """Number of ACI actions successfully dispatched this session (eval step count)."""
        return self._inner.actions_dispatched

    def act(self, verb: str, target: dict | None = None, **kwargs: Any) -> dict:
        return self._loop.run(self._inner.act(verb, target, **kwargs))

    def act_batch(
        self, actions: list[dict], *, stop_on_error: bool = True, batch_id: str | None = None
    ) -> dict:
        """Execute an ordered batch of ACI actions serially (#73)."""
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
        retain its ``ref → Element`` map (#78)."""
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

    def _set_guest_transport(self, transport: Any) -> None:
        """Attach a guest-boundary transfer backend (provider-supplied, #154)."""
        self._inner._set_guest_transport(transport)

    def _set_provider_context(self, provider: Any, handle: Any) -> None:
        """Attach the managing provider + handle (provider.connect(), #206)."""
        self._inner._set_provider_context(provider, handle)

    def checkpoint(self, name: str | None = None, *, agent_state_ref: str | None = None) -> str:
        """Create a runtime checkpoint and return its checkpoint id.

        Requires a provider's connect()."""
        return self._loop.run(self._inner.checkpoint(name, agent_state_ref=agent_state_ref))

    def fork(self) -> Any:
        """Fork the current provider-managed sandbox and return the new provider handle."""
        return self._loop.run(self._inner.fork())

    def resume(self, handle_or_checkpoint: Any) -> Any:
        """Resume a provider handle or checkpoint id and return a live provider handle."""
        return self._loop.run(self._inner.resume(handle_or_checkpoint))

    def put_file(self, local_path: str, sandbox_path: str) -> dict:
        """Upload a host file into the sandbox; return its content-addressed ref (#85)."""
        return self._inner.put_file(local_path, sandbox_path)

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
    token: str | None = None,
    sandbox_capabilities: dict | None = None,
    enforce_capabilities: bool | None = None,
    artifact_root: str | None = None,
    on_ask: Callable[[str, str | None, str], bool] | None = None,
) -> Sandbox:
    """Connect to a running ``shinkend`` and complete the ACI handshake (blocking).

    ``sandbox_capabilities`` overrides the session's v0 capability envelope.
    ``enforce_capabilities`` controls the local Action Gateway shim (#84/#161): when on,
    an action whose capability the envelope does not grant is denied before dispatch (so
    it never reaches shinkend), and a capability valued ``"ask"`` pauses for approval via
    ``on_ask`` (#7). This shim is a *client-side reference* boundary, not a security
    guarantee — a direct WebSocket client bypasses it; true enforcement is the server-side
    Action Gateway (D6)."""
    loop = _BackgroundLoop()
    try:
        inner = loop.run(
            aconnect(
                addr,
                token=token,
                sandbox_capabilities=sandbox_capabilities,
                enforce_capabilities=enforce_capabilities,
                artifact_root=artifact_root,
                on_ask=on_ask,
            )
        )
    except Exception:
        loop.stop()
        raise
    return Sandbox(inner, loop)
