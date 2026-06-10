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
from dataclasses import dataclass, field
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
    # Image codecs the runtime can encode. Defaults to png-only: a welcome from a
    # pre-negotiation runtime omits the field, and those only ever encoded PNG.
    image_formats: list[str] = field(default_factory=lambda: ["png"])


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
            image_formats=caps.get("image_formats") or ["png"],
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
        # Per-instance call-id salt (#56): start_screencast call_ids seed the runtime's
        # SERVER-WIDE stream resume registry, so two SDK clients on one shinkend must
        # not mint colliding ids — deterministic c1/c2/... would.
        self._id_prefix = uuid.uuid4().hex[:6]
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
        # First-class capability/permission decision log (#83/E4): every gateway grant,
        # ask-resolution, and denial is appended here as a typed event, so a run can show
        # which boundary powers it exercised and how each was resolved (the audit surface
        # the eval/trajectory layers read).
        self._capability_events: list[dict] = []
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
        # Logical stream id of the active screencast (#56): seeded with the resumed id
        # or the start_screencast call_id, then updated from each frame's `stream`
        # field, which is authoritative (a resume may have fallen back to a fresh id).
        # What a caller passes to resume_screencast() on a NEW session after a
        # mid-stream ConnectionError.
        self._active_stream: str | None = None
        # Capture params of the last started screencast (#56): a resume continues
        # stream identity + seq only — the runtime does not remember fps/scope/
        # max_long_edge — so a same-session resume reuses these instead of silently
        # resetting to defaults.
        self._last_screencast_params: dict | None = None

    def _next_id(self) -> str:
        self._seq += 1
        return f"c{self._id_prefix}-{self._seq}"

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

    def _record_decision(
        self, capability: str, subject: str, decision: str, granted: bool, reason: str | None
    ) -> None:
        """Append one capability/permission decision event (#83/E4). ``decision`` is the
        gateway verdict (``allow``/``ask``/``deny``); ``granted`` is whether the action was
        ultimately permitted (an ``ask`` resolves to granted or not via ``on_ask``)."""
        self._capability_events.append(
            {
                "type": "capability",
                "capability": capability,
                "subject": subject,
                "decision": decision,
                "granted": granted,
                "reason": reason,
                "ts": time.time(),
            }
        )

    def _gate(self, verb: str) -> None:
        """Action Gateway check (#84/#7): when enforcing, decide allow / deny / **ask**
        before dispatch — so a denied or unapproved verb never reaches shinkend, and record
        the decision as a capability event (#83). A capability valued ``"ask"`` is *risky*:
        it pauses for approval via the ``on_ask`` handler (default: deny). GUI input passes
        when input_automation is granted (the default)."""
        if not self._enforce:
            return
        decision, cap, reason = decide_action(verb, self.sandbox_capabilities)
        if decision == "allow":
            self._record_decision(cap, verb, "allow", True, None)
            return
        if decision == "ask":
            granted = bool(self._on_ask(verb, cap, reason)) if self._on_ask else False
            self._record_decision(cap, verb, "ask", granted, None if granted else reason)
            if not granted:
                raise CapabilityDenied(f"{verb}: approval denied ({cap})")
            return
        self._record_decision(cap, verb, "deny", False, reason)
        raise CapabilityDenied(f"{verb}: {reason}")

    def gate_capability(self, cap_name: str, label: str) -> None:
        """Gateway check for a *named* capability (e.g. ``a11y`` for structured
        observation, #145) — allow / deny / ask, each recorded as a capability event (#83),
        with the same semantics as :meth:`_gate` does for verbs. No-op unless enforcing."""
        if not self._enforce:
            return
        val = self.sandbox_capabilities.get(cap_name, False)
        if isinstance(val, str) and val.lower() == "ask":
            granted = (
                bool(self._on_ask(label, cap_name, "requires approval")) if self._on_ask else False
            )
            self._record_decision(
                cap_name, label, "ask", granted, None if granted else "requires approval"
            )
            if not granted:
                raise CapabilityDenied(f"{label}: approval denied ({cap_name})")
            return
        if val:
            self._record_decision(cap_name, label, "allow", True, None)
            return
        reason = f"capability '{cap_name}' not granted"
        self._record_decision(cap_name, label, "deny", False, reason)
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

    def _check_image_format(self, format: str | None) -> None:
        """Codec negotiation: reject a requested ``format`` the runtime's welcome did
        not advertise (``capabilities.image_formats``) BEFORE anything goes on the
        wire, so the caller gets a typed :class:`ValueError` instead of a runtime nack
        round-trip. ``None`` (the png default) always passes — every runtime encodes
        PNG."""
        if format is not None and format not in self.capabilities.image_formats:
            raise ValueError(
                f"image format {format!r} not advertised by the runtime "
                f"(supported: {self.capabilities.image_formats})"
            )

    async def screenshot(
        self,
        scope: str = "screen",
        *,
        format: str | None = None,
        quality: int | None = None,
    ) -> dict:
        """Capture pixels on demand.

        ``format`` selects the wire codec: ``None``/``"png"`` (lossless default) or
        ``"jpeg"`` (the bandwidth lever — a 1080p desktop is ~1.8 MB as PNG vs ~0.1 MB as
        quality-80 JPEG). ``quality`` (1–100) tunes JPEG; ignored for PNG.

        Returns ``{'bytes', 'format', 'w', 'h'}`` — ``bytes`` is the raw encoded image and
        ``format`` says which codec. When the codec is PNG, ``'png'`` is kept as a
        back-compatible alias of ``bytes``; it is ABSENT for other codecs, so legacy
        code blindly reading ``shot['png']`` fails loudly rather than mislabeling JPEG
        bytes as PNG.

        Raises :class:`ValueError` before sending if ``format`` is not among the
        runtime's advertised ``capabilities.image_formats``."""
        self._gate("screenshot")
        self._check_image_format(format)
        call_id = self._next_id()
        action: dict = {"verb": "screenshot", "scope": scope}
        if format is not None:
            action["format"] = format
        if quality is not None:
            action["quality"] = quality
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        if reply.get("type") != "observation":
            raise RuntimeError(reply.get("error", "screenshot failed"))
        img = reply.get("image") or {}
        raw = base64.b64decode(img.get("ref", ""))
        out = {
            "bytes": raw,
            "format": img.get("format", "png"),
            "w": img.get("w"),
            "h": img.get("h"),
        }
        if out["format"] == "png":
            out["png"] = raw  # back-compat alias, only when it really is PNG
        return out

    async def type_text(self, text: str):
        return await self.act("type_text", text=text)

    async def key(self, keys: str):
        return await self.act("key", keys=keys)

    async def astart_screencast(
        self,
        fps: float = 5.0,
        max_long_edge: int | None = None,
        scope: str | None = None,
        resume_stream: str | None = None,
        format: str | None = None,
        quality: int | None = None,
        delta: bool = False,
    ) -> str:
        """Start a server-pushed screencast; returns the requested stream id. Frames
        arrive asynchronously and are read with :meth:`next_frame`. ``max_long_edge``
        caps each frame's longer edge (px) to save bandwidth; ``scope`` selects the
        capture region (``screen``, ``active_window``, or ``window:<id>``); ``format``
        (``png`` default / ``jpeg``) + ``quality`` pick the per-frame codec — ``jpeg`` is
        the high-fps bandwidth lever. Raises :class:`ValueError` before sending if
        ``format`` is not among the runtime's advertised ``capabilities.image_formats``.

        ``delta`` turns on dirty-tile delta mode (B2), the LOSSLESS bandwidth lever:
        the runtime pushes only the changed 64px tiles of each frame (read back as
        ``frame['tiles']`` from :meth:`next_frame`) instead of a full image, with a
        full keyframe first, after a resume, and periodically thereafter. Compositing
        tiles onto the last keyframe is the consumer's job.

        ``resume_stream`` asks the runtime to continue that logical stream (#56): if
        the runtime still holds its state, frames keep the SAME ``stream`` id and
        ``seq`` continues where it left off; otherwise a fresh stream starts (new id,
        seq 0). The ack carries no stream identity — the first frame's ``stream``/
        ``seq`` are authoritative for which of the two happened."""
        self._gate("start_screencast")
        self._check_image_format(format)
        self._clear_frames()
        # Remember the requested capture region so recorded frames carry the true
        # scope, not a hardcoded "screen" (#143). Frames also echo it from shinkend.
        self._stream_scope = scope or "screen"
        action: dict = {"verb": "start_screencast", "fps": fps}
        if max_long_edge is not None:
            action["max_long_edge"] = max_long_edge
        if scope is not None:
            action["scope"] = scope
        if resume_stream is not None:
            action["resume_stream"] = resume_stream
        if format is not None:
            action["format"] = format
        if quality is not None:
            action["quality"] = quality
        if delta:
            action["delta"] = True
        call_id = self._next_id()
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "start_screencast failed"))
        # Remember the capture params for a same-session resume (#56): resume
        # continues stream IDENTITY + SEQ only, never the capture parameters.
        self._last_screencast_params = {
            "fps": fps,
            "max_long_edge": max_long_edge,
            "scope": scope,
            "format": format,
            "quality": quality,
            "delta": delta,
        }
        self._active_stream = resume_stream or call_id
        return self._active_stream

    @property
    def active_stream(self) -> str | None:
        """The logical screencast stream id observed on frames — pass this to
        :meth:`aresume_screencast` after a drop. Updated from each frame's ``stream``
        field, which is authoritative: after a resume that fell back to a fresh
        stream, this is the LIVE id, not the requested dead one."""
        return self._active_stream

    async def aresume_screencast(
        self,
        stream_id: str,
        fps: float | None = None,
        max_long_edge: int | None = None,
        scope: str | None = None,
        format: str | None = None,
        quality: int | None = None,
        delta: bool | None = None,
    ) -> str:
        """Resume the logical screencast ``stream_id`` (#56) — typically after
        :meth:`next_frame` raised :class:`ConnectionError` and the caller reconnected
        with a fresh session.

        Resume continues stream IDENTITY + SEQ only — the runtime does not remember
        capture parameters. Params left as ``None`` reuse the ones this Sandbox
        started the stream with; on a NEW connection the caller must re-pass ALL the
        original capture parameters (including ``format``/``quality``/``delta`` —
        else a JPEG stream silently resumes as the PNG default, ~20× larger frames,
        and a delta stream resumes as full frames) or accept the standard defaults
        (fps=5.0, full resolution, ``screen`` scope, ``png``, full frames). A resumed
        delta stream restarts from a full keyframe — the runtime's tile baseline does
        not survive the stream task.

        Read the first frame to learn the outcome: ``stream`` == ``stream_id`` means
        seq continued — the seq gap counts frames the runtime emitted but the client
        never received; capture pauses while no connection holds the stream, so use
        the :class:`ConnectionError` window for temporal accounting. A different
        ``stream`` means the runtime had dropped the state and a fresh stream
        started at seq 0."""
        remembered = self._last_screencast_params or {}
        if fps is None:
            fps = remembered.get("fps", 5.0)
        if max_long_edge is None:
            max_long_edge = remembered.get("max_long_edge")
        if scope is None:
            scope = remembered.get("scope")
        if format is None:
            format = remembered.get("format")
        if quality is None:
            quality = remembered.get("quality")
        if delta is None:
            delta = remembered.get("delta", False)
        return await self.astart_screencast(
            fps,
            max_long_edge=max_long_edge,
            scope=scope,
            resume_stream=stream_id,
            format=format,
            quality=quality,
            delta=delta,
        )

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
        """Await the next screencast frame as ``{'bytes', 'format', 'w', 'h', 'seq',
        'stream'}`` (``'png'`` aliases ``bytes`` only when the codec really is PNG), or
        None if the stream ended cleanly or `timeout` seconds elapsed with no frame.
        Raises :class:`ConnectionError` if the connection dropped mid-stream, so silent
        truncation is distinguishable from a normal end.

        On a delta stream (``delta=True``), a dirty-tile frame comes back as
        ``{'tiles': [{'x', 'y', 'w', 'h', 'bytes'}, ...], 'seq', 'stream'}`` instead —
        no ``'bytes'``/``'png'`` keys. Each tile's ``bytes`` is one image encoded in
        the stream's format/quality, positioned at (x, y) in the last full keyframe's
        resolution. The SDK passes tiles through RAW: compositing them onto the last
        keyframe is the consumer's job (the SDK takes no imaging dependency)."""
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
        # The frame's `stream` is authoritative (#56): adopt it, so after a resume
        # that fell back to a fresh stream the recorded id is the LIVE one and the
        # next resume targets a stream the runtime actually holds.
        self._active_stream = item.get("stream") or self._active_stream
        tiles = item.get("tiles")
        if tiles is not None:
            return {
                "tiles": [
                    {
                        "x": t.get("x"),
                        "y": t.get("y"),
                        "w": t.get("w"),
                        "h": t.get("h"),
                        "bytes": base64.b64decode(t.get("ref", "")),
                    }
                    for t in tiles
                ],
                "seq": item.get("seq"),
                "stream": item.get("stream"),
            }
        img = item.get("image") or {}
        raw = base64.b64decode(img.get("ref", ""))
        frame = {
            "bytes": raw,
            "format": img.get("format", "png"),
            "w": img.get("w"),
            "h": img.get("h"),
            "seq": item.get("seq"),
            "stream": item.get("stream"),
        }
        if frame["format"] == "png":
            frame["png"] = raw  # back-compat alias, only when it really is PNG
        return frame

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
        is not granted (recorded as a capability event, #83); returns the effective scope."""
        scope = self.sandbox_capabilities.get("fs_scope", "session")
        if self._enforce:
            allowed, cap, reason = check_file_transfer(direction, self.sandbox_capabilities)
            if not allowed:
                self._record_decision(cap, direction, "deny", False, reason)
                raise CapabilityDenied(f"{direction}: {reason}")
            self._record_decision(cap, direction, "allow", True, None)
        return str(scope)

    @property
    def capability_events(self) -> list[dict]:
        """The capability/permission decisions recorded this session (#83/E4) — a copy, in
        order. Empty unless ``enforce_capabilities`` is on. Each event is
        ``{type:"capability", capability, subject, decision, granted, reason, ts}``."""
        return list(self._capability_events)

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


class SharedLoop:
    """One background event-loop thread that MANY sync :class:`Sandbox` sessions share.

    By default each :func:`connect` spins up its own loop thread — right for one session,
    but a process holding N sessions then pays a thread per session (measured ~7 MB + an
    OS thread each; see docs/engineering/streaming-bandwidth.md). Pass one ``SharedLoop``
    to several ``connect(..., loop=…)`` calls and they all multiplex onto this single
    thread::

        with shinken.SharedLoop() as loop:
            envs = [shinken.connect(a, token=t, loop=loop) for a, t in endpoints]
            ...
            for env in envs:
                env.close()   # closes the session; the shared loop stays up

    Closing a Sandbox that uses a shared loop never stops the loop — the ``SharedLoop``
    owns its thread and stops it on ``close()`` / context-manager exit.

    This is deliberately just a resource handle, not an orchestration surface: fan-out,
    batching, and failure policy belong to the caller (or the async core —
    ``aconnect()`` + ``asyncio.gather`` on your own loop is the canonical way to drive
    many sandboxes concurrently)."""

    def __init__(self) -> None:
        self._bg = _BackgroundLoop()
        self._closed = False

    def close(self) -> None:
        """Stop the shared loop thread. Close the Sandboxes using it first. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._bg.stop()

    def __enter__(self) -> SharedLoop:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


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
        fps: float | None,
        timeout: float | None,
        limit: int | None,
        max_long_edge: int | None,
        scope: str | None,
        resume_stream: str | None = None,
        format: str | None = None,
        quality: int | None = None,
        delta: bool | None = None,
    ) -> None:
        self._sb = sandbox
        self._fps = fps
        self._timeout = timeout
        self._limit = limit
        self._max_long_edge = max_long_edge
        self._scope = scope
        self._resume_stream = resume_stream
        self._format = format
        self._quality = quality
        self._delta = delta
        self._count = 0
        self._started = False

    def __enter__(self) -> _Screencast:
        # A resume routes through aresume_screencast so params left as None reuse
        # the ones this Sandbox started the stream with (#56) — resume continues
        # stream identity + seq only, never the capture parameters.
        if self._resume_stream is not None:
            self._sb._loop.run(
                self._sb._inner.aresume_screencast(
                    self._resume_stream,
                    self._fps,
                    self._max_long_edge,
                    self._scope,
                    format=self._format,
                    quality=self._quality,
                    delta=self._delta,
                )
            )
        else:
            self._sb._loop.run(
                self._sb._inner.astart_screencast(
                    self._fps,
                    self._max_long_edge,
                    self._scope,
                    format=self._format,
                    quality=self._quality,
                    delta=bool(self._delta),
                )
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

    def __init__(
        self, inner: AsyncSandbox, loop: _BackgroundLoop, *, owns_loop: bool = True
    ) -> None:
        self._inner = inner
        self._loop = loop
        # False when the loop is a caller-provided SharedLoop: closing this Sandbox must
        # not stop a loop other sessions are still multiplexed on.
        self._owns_loop = owns_loop
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

    @property
    def capability_events(self) -> list[dict]:
        """Capability/permission decisions recorded this session (#83/E4) — grants, ask
        resolutions, and denials made by the local gateway, in order. Empty unless
        ``enforce_capabilities`` is on. An eval/trajectory layer reads this as the run's
        first-class permission audit."""
        return self._inner.capability_events

    def gate_capability(self, cap_name: str, label: str) -> None:
        """Run a named-capability gateway check (allow/ask/deny), recording the decision —
        synchronous, no wire I/O. Raises :class:`~shinken.gateway.CapabilityDenied` if denied
        or an ask is not approved."""
        self._inner.gate_capability(cap_name, label)

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

    def screenshot(
        self, scope: str = "screen", *, format: str | None = None, quality: int | None = None
    ) -> dict:
        return self._loop.run(self._inner.screenshot(scope, format=format, quality=quality))

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
        format: str | None = None,
        quality: int | None = None,
        delta: bool = False,
    ) -> _Screencast:
        """Stream the screen in real time as a context manager yielding frames::

            with env.screencast(fps=10, limit=30, max_long_edge=720) as frames:
                for frame in frames:
                    ...  # frame: {'bytes', 'format', 'w', 'h', 'seq', 'stream'}

        Frames identical to the previous one are suppressed server-side, so an idle
        screen yields nothing until it changes. ``timeout`` bounds the wait per frame
        (None blocks indefinitely); ``limit`` caps the number of frames;
        ``max_long_edge`` downscales each frame to save bandwidth; ``scope`` selects
        the region (``screen``, ``active_window``, ``window:<id>``); ``format``
        (``png``/``jpeg``) + ``quality`` pick the per-frame codec.

        ``delta=True`` turns on dirty-tile delta mode (the lossless bandwidth lever):
        after a full keyframe, changed frames arrive as ``{'tiles': [{'x', 'y', 'w',
        'h', 'bytes'}, ...], 'seq', 'stream'}`` — only the changed 64px tiles, to
        composite onto the last keyframe yourself (the SDK takes no imaging
        dependency). A full keyframe is re-sent periodically and after a resume.
        """
        return _Screencast(
            self,
            fps,
            timeout,
            limit,
            max_long_edge,
            scope,
            format=format,
            quality=quality,
            delta=delta,
        )

    @property
    def active_stream(self) -> str | None:
        """The logical screencast stream id observed on frames — pass this to
        :meth:`resume_screencast` after a drop. Updated from each frame's ``stream``
        field, which is authoritative: after a resume that fell back to a fresh
        stream, this is the LIVE id, not the requested dead one."""
        return self._inner.active_stream

    def resume_screencast(
        self,
        stream_id: str,
        fps: float | None = None,
        *,
        timeout: float | None = 30.0,
        limit: int | None = None,
        max_long_edge: int | None = None,
        scope: str | None = None,
        format: str | None = None,
        quality: int | None = None,
        delta: bool | None = None,
    ) -> _Screencast:
        """Resume the logical screencast ``stream_id`` (#56), as the same
        frame-iterating context manager :meth:`screencast` returns.

        Resume continues stream IDENTITY + SEQ only — the runtime does not remember
        capture parameters. Params left as ``None`` reuse the ones this Sandbox
        started the stream with; on a NEW connection the caller must re-pass ALL the
        original capture parameters (including ``format``/``quality``/``delta`` —
        else a JPEG stream silently resumes as the PNG default and a delta stream
        as full frames) or accept the standard defaults (fps=5.0, full resolution,
        ``screen`` scope, ``png``, full frames). A resumed delta stream restarts from
        a full keyframe.

        The recovery flow: iterating a screencast raises :class:`ConnectionError`
        when the connection drops mid-stream; the caller reconnects (a NEW
        ``connect()``) and resumes the old stream id there. The first frame tells the
        outcome — ``stream`` == ``stream_id`` means ``seq`` continued: the seq gap
        counts frames the runtime emitted but the client never received; capture
        pauses while no connection holds the stream, so use the
        :class:`ConnectionError` window for temporal accounting. A different
        ``stream`` means the runtime no longer held the state and a fresh stream
        started at seq 0."""
        return _Screencast(
            self,
            fps,
            timeout,
            limit,
            max_long_edge,
            scope,
            stream_id,
            format=format,
            quality=quality,
            delta=delta,
        )

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
        if self._owns_loop:
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
    loop: SharedLoop | None = None,
) -> Sandbox:
    """Connect to a running ``shinkend`` and complete the ACI handshake (blocking).

    ``sandbox_capabilities`` overrides the session's v0 capability envelope.
    ``enforce_capabilities`` controls the local Action Gateway shim (#84/#161): when on,
    an action whose capability the envelope does not grant is denied before dispatch (so
    it never reaches shinkend), and a capability valued ``"ask"`` pauses for approval via
    ``on_ask`` (#7). This shim is a *client-side reference* boundary, not a security
    guarantee — a direct WebSocket client bypasses it; true enforcement is the server-side
    Action Gateway (D6).

    ``loop`` multiplexes this session onto a caller-owned :class:`SharedLoop` instead of
    spawning a dedicated background thread — pass the same one to every ``connect`` when
    a single process holds many sessions (closing such a Sandbox leaves the shared loop
    running). For CONCURRENT fan-out across many sandboxes, the async core is the native
    surface: ``aconnect()`` + ``asyncio.gather`` on your own event loop."""
    owns_loop = loop is None
    bg = _BackgroundLoop() if loop is None else loop._bg
    try:
        inner = bg.run(
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
        if owns_loop:
            bg.stop()  # never tear down a caller-owned shared loop on one failed dial
        raise
    return Sandbox(inner, bg, owns_loop=owns_loop)
