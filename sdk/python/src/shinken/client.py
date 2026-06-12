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
import os
import random
import shutil
import tempfile
import threading
import time
import uuid
import weakref
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as _ws_connect
from websockets.exceptions import ConnectionClosed as _WsConnectionClosed
from websockets.exceptions import InvalidHandshake as _WsInvalidHandshake
from websockets.exceptions import InvalidURI as _WsInvalidURI

from .artifacts import LocalArtifactStore
from .errors import (
    ConnectError,
    ProviderRequired,
    SessionClosed,
    ShinkenError,
    UnknownVerb,
    classify_exception,
)
from .gateway import CapabilityDenied, check_file_transfer, decide_action

__all__ = [
    "connect",
    "aconnect",
    "Sandbox",
    "AsyncSandbox",
    "Capabilities",
    "Checkpoint",
    "FrameCache",
    "SandboxFleet",
    "SharedLoop",
]

_log = logging.getLogger("shinken.client")

DEFAULT_ADDR = "127.0.0.1:8765"
_CLIENT = {"name": "shinken-py", "version": "0.1.0"}
_FRAME_QUEUE_MAX = 32
# Bound inbound WebSocket frames (#136): generous enough for 4K screenshots / large a11y
# trees, but not unbounded — a malformed or hostile peer can't force unbounded buffering.
# (websockets' 1 MiB default would also reject legitimate large screenshots.)
_MAX_WS_MESSAGE = 16 * 1024 * 1024
# Bound the handshake recv so aconnect() can't hang forever against a server that accepts
# the socket but never sends `welcome`.
_HANDSHAKE_TIMEOUT = 10.0
# The websockets library's keepalive default, restated so a jittered dial (`ping_jitter`)
# adds its uniform offset to the same base the un-jittered path uses.
_PING_INTERVAL = 20.0

# ---- Many-sandbox memory governance (docs/engineering/many-sandbox-concurrency.md §2) ----
# Each connection bounds its frame queue by COUNT (_FRAME_QUEUE_MAX, drop-oldest) — right for
# one sandbox, N-unbounded in aggregate: 1024 conns × 32 frames × ~100 KB JPEG ≈ 3.2 GB worst
# case; with PNG (~1.8 MB/frame) ~59 GB. This knob adds an OPT-IN byte budget shared by every
# AsyncSandbox on one event loop: when set, each push accounts the frame's resident bytes and
# evicts the approximately globally-oldest queued frames — whichever connection holds them —
# until the budget holds.
#
# Scope: PER EVENT LOOP. The loop is the deployment unit (one fleet = one loop — the async
# core directly, or one SharedLoop for sync callers), and the loop's single-threadedness is
# what makes the shared counter/deque safe WITHOUT locks on the hot path. Two loops = two
# independent budgets. Default None = off (exactly the current per-connection behavior).
# Recommended fleet setting: 256 MiB (256 << 20). Set it BEFORE connecting the fleet: frames
# queued while the knob was off are not in the eviction order and simply drain on consumption.
GLOBAL_FRAME_BUDGET_BYTES: int | None = None

# loop -> {"used": queued bytes, "frames": live tracked-frame count, "order": deque of
# AsyncSandbox refs in push order (one per tracked frame; stale entries lazily drained)}.
# WeakKey so a finished loop's accounting is collectable with it.
_FRAME_BUDGET_STATES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _evict_over_budget(state: dict) -> None:
    """Evict the approximately globally-oldest queued frames on this loop until the byte
    budget is satisfied. Only ever runs on the owning loop's thread (no locks). ``order``
    holds one entry per tracked frame push; an entry whose frame was already consumed is
    stale — skipped here, drained at the head opportunistically, and compacted when stale
    entries outnumber live frames, so the push path stays O(1) amortized."""
    order: deque = state["order"]
    while order and not order[0]._queued_bytes:  # head-drain consumed-frame entries
        order.popleft()
    budget = GLOBAL_FRAME_BUDGET_BYTES
    if budget is not None:
        while state["used"] > budget and order:
            sb = order.popleft()
            if sb._queued_bytes:
                sb._drop_oldest_frame()
    # Periodic compaction: stale middle entries (consumed frames) can pile up behind a
    # long-lived head entry; rebuild so entries == live frames when 4:1 outnumbered.
    if len(order) > 64 and len(order) > 4 * state["frames"]:
        per_sandbox: dict[int, int] = {}
        fresh: deque = deque()
        for sb in order:
            n = per_sandbox.get(id(sb), 0)
            if n < len(sb._queued_bytes):
                fresh.append(sb)
                per_sandbox[id(sb)] = n + 1
        state["order"] = fresh


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
    # In-guest exec (G1): granted by default like input automation — eval/setup flows
    # are the primary consumer. Set "ask" for approval-gated runs or False to deny;
    # every decision is recorded with the argv/shell in the event's `detail`.
    "exec": True,
    # Starting desktop apps (launch_app) is core task behavior, granted by default
    # like input; deny it per-session when a workload must not spawn processes.
    "app_launch": True,
    "fs_scope": "session",
    "egress": False,
    "credentials": False,
    # The clipboard is a data channel (boundary-ish): both clipboard_get and
    # clipboard_set gate on this, default-off.
    "clipboard": False,
    "privileged_install": False,
}

#: Runtime-side default exec deadline (mirrors shinkend's DEFAULT_TIMEOUT_MS) — used
#: to size the SDK's reply wait when the caller passes no timeout.
DEFAULT_EXEC_TIMEOUT = 60.0
#: Grace added on top of the exec deadline before the SDK gives up on the reply.
_EXEC_RPC_GRACE = 30.0


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
    # Whether the runtime can deliver image observations as binary WebSocket frames
    # (u32 LE header_len | JSON header | raw codec payload) when the client opts in
    # via hello.accept.binary_frames. False on welcomes from pre-binary runtimes.
    binary_frames: bool = False
    # Whether the runtime understands content-negotiated screenshots: frame_hash on
    # screenshot observations, if_none_match on the screenshot action, not_modified
    # on a hash hit. False on older welcomes — the SDK never sends if_none_match
    # then (pre-dedup runtimes reject unknown action fields).
    frame_dedup: bool = False
    # Whether the runtime honors the per-action ``observe`` argument
    # (act-returns-observation): a mutating action's ack is followed by a fresh
    # observation with cause = the action's call_id. False on older welcomes — the
    # SDK then rejects ``observe=`` before anything goes on the wire.
    observe_after_act: bool = False
    # Whether the runtime ships the guest-side structured-observation engine (the
    # `observe` verb with stable element refs + tree_text, element_ref targets,
    # invoke_action/set_value). False on welcomes from pre-engine runtimes — the SDK
    # then falls back to its co-located AT-SPI helper.
    structured_observation: bool = False


def _to_uri(addr: str) -> str:
    return addr if addr.startswith("ws://") or addr.startswith("wss://") else f"ws://{addr}"


def _image_bytes(obj: dict) -> bytes:
    """Raw encoded bytes of one image/tile dict: the binary path's ``data`` verbatim
    (already raw codec bytes), else the text path's base64 ``ref`` decoded."""
    data = obj.get("data")
    if data is not None:
        return bytes(data)
    return base64.b64decode(obj.get("ref", ""))


def _frame_resident_bytes(item: object) -> int:
    """Resident payload size of one queued stream frame, for the frame-budget
    accounting: raw payload ``bytes`` on the binary path (``data``), the queued
    base64 string on the text path (``ref``) — image plus any tiles."""
    if not isinstance(item, dict):
        return 0
    img = item.get("image") or {}
    size = len(img.get("data") or img.get("ref") or "")
    for tile in item.get("tiles") or []:
        size += len(tile.get("data") or tile.get("ref") or "")
    return size


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


def _image_payload(reply: dict) -> dict:
    """Decode an ``observation`` reply's image into the SDK screenshot shape:
    ``{'bytes', 'format', 'w', 'h', 'wire_len'}`` (+ ``frame_hash`` when the runtime
    stamped one) — plus a ``'png'`` alias of ``bytes`` ONLY when the codec really is
    PNG, so legacy code blindly reading ``shot['png']`` fails loudly rather than
    mislabeling JPEG bytes as PNG. Decodes both wire paths via :func:`_image_bytes`:
    binary-frame raw payloads and text-path base64 ``ref``."""
    img = reply.get("image") or {}
    raw = _image_bytes(img)  # binary-path raw bytes, or text-path base64 decoded
    out = {
        "bytes": raw,
        "format": img.get("format", "png"),
        "w": img.get("w"),
        "h": img.get("h"),
        # On-the-wire size of the observation that carried this image (binary
        # frame or JSON text), for bandwidth accounting. None on mocked replies
        # injected without a transport.
        "wire_len": reply.get("wire_len"),
    }
    if out["format"] == "png":
        out["png"] = raw  # back-compat alias, only when it really is PNG
    frame_hash = reply.get("frame_hash")
    if frame_hash is not None:
        out["frame_hash"] = frame_hash
    pointer = reply.get("pointer")
    if pointer is not None:
        # live pointer position in capture pixels [x, y] — observation metadata;
        # the frame itself is cursor-free (frame-hash dedup depends on that)
        out["pointer"] = pointer
    return out


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
            binary_frames=bool(caps.get("binary_frames", False)),
            frame_dedup=bool(caps.get("frame_dedup", False)),
            observe_after_act=bool(caps.get("observe_after_act", False)),
            structured_observation=bool(caps.get("structured_observation", False)),
        ),
        (welcome.get("server") or {}).get("platform", "linux"),
    )


def _action_error(reply: dict, verb: str) -> RuntimeError:
    """Build the typed exception for a nacked action: the runtime's
    ``unknown verb: …`` / ``unsupported verb: …`` rejections become
    :class:`~shinken.errors.UnknownVerb` (so capability-negotiation fallbacks can
    branch on type), everything else stays the historical :class:`RuntimeError`."""
    msg = reply.get("error") or f"action {verb!r} failed"
    low = msg.lower()
    if "unknown verb" in low or "unsupported verb" in low or "unrecognized verb" in low:
        return UnknownVerb(msg)
    return RuntimeError(msg)


def _observation_dict(reply: dict) -> dict:
    """Shape one image-bearing ``observation`` message into the dict the SDK returns
    for screenshots and act-returns-observation: ``{'bytes', 'format', 'w', 'h',
    'wire_len'}`` — plus the ``'png'`` back-compat alias only when the codec really is
    PNG, so legacy readers fail loudly instead of mislabeling JPEG bytes."""
    img = reply.get("image") or {}
    raw = _image_bytes(img)  # binary-path raw bytes, or text-path base64 decoded
    out = {
        "bytes": raw,
        "format": img.get("format", "png"),
        "w": img.get("w"),
        "h": img.get("h"),
        # On-the-wire size of the observation that carried this image (binary
        # frame or JSON text), for bandwidth accounting. None on mocked replies
        # injected without a transport.
        "wire_len": reply.get("wire_len"),
    }
    if out["format"] == "png":
        out["png"] = raw  # back-compat alias, only when it really is PNG
    return out


#: The keys an ``observe=`` dict admits — the screenshot-shaped capture levers
#: (schema ``$defs.ObserveSpec``).
_OBSERVE_KEYS = {"format", "quality", "max_long_edge", "scope"}


def _parse_binary_frame(raw: bytes) -> dict:
    """Parse one binary media frame — ``u32 LE header_len | JSON header | payload`` —
    into the SAME message-dict shape the JSON path produces, except that the image /
    tile payloads arrive as raw ``bytes`` under ``"data"`` instead of base64 under
    ``"ref"``. The header is small (a few hundred bytes), so this replaces the
    megabyte ``json.loads`` + ``b64decode`` of the text path with one JSON parse of
    the header and one slice per payload."""
    if len(raw) < 4:
        raise ValueError("binary frame shorter than its 4-byte header length")
    header_len = int.from_bytes(raw[:4], "little")
    base = 4 + header_len
    if base > len(raw):
        raise ValueError(f"binary frame header_len {header_len} exceeds frame size {len(raw)}")
    msg = json.loads(raw[4:base].decode("utf-8"))
    payload_len = len(raw) - base

    def _slice(off: int, ln: int) -> bytes:
        if off < 0 or ln < 0 or off + ln > payload_len:
            raise ValueError(f"payload slice ({off}, {ln}) outside payload of {payload_len}")
        return raw[base + off : base + off + ln]

    img = msg.get("image")
    if img is not None:
        img["data"] = _slice(img.pop("off", 0), img.pop("len", payload_len))
    for tile in msg.get("tiles") or []:
        tile["data"] = _slice(tile.pop("off"), tile.pop("len"))
    # exec_output binary frames (G1): the chunk bytes live at data.off/len — replace
    # the locator dict with the raw bytes, the shape the stream consumer reads.
    data = msg.get("data")
    if isinstance(data, dict) and "off" in data:
        msg["data"] = _slice(data["off"], data["len"])
    msg["wire_len"] = len(raw)
    return msg


# Env default for screenshot dedup (`screenshot(dedup=...)` / `connect(screenshot_dedup=...)`):
# set SHINKEN_SCREENSHOT_DEDUP=1 to turn content-negotiated screenshots on for every session
# that doesn't say otherwise. Read at connect time, not import time, so tests/processes can
# flip it without reimporting.
_DEDUP_ENV = "SHINKEN_SCREENSHOT_DEDUP"


def _env_dedup_default() -> bool:
    return os.environ.get(_DEDUP_ENV, "").strip().lower() in ("1", "true", "yes", "on")


class FrameCache:
    """Content-addressed screenshot store for content-negotiated observation
    (``if_none_match``) — ``frame_hash → frame dict`` with an LRU cap.

    Every dedup-enabled session keeps a private one by default (per-session
    last-frame dedup). The fork-fleet move is to share ONE across many sessions::

        cache = shinken.FrameCache()
        envs = [provider.connect(h, frame_cache=cache) for h in replicas]

    N replicas forked from one checkpoint show near-identical screens, and the
    frame hash is computed runtime-side over RAW pixels (codec-independent), so a
    frame fetched once from ANY replica answers the other N-1 observes with a
    ~200-byte ``not_modified`` instead of N full payloads.

    Thread-safe (a lock around every access): sessions on different event loops /
    a SharedLoop thread can share one cache. The LRU cap bounds memory at roughly
    ``max_entries`` × one encoded frame."""

    def __init__(self, max_entries: int = 64) -> None:
        self._lock = threading.Lock()
        # (params_key, frame_hash) -> frame dict ({'bytes', 'format', 'w', 'h'}
        # [+ 'png' alias]). Keyed by params TOO, not the hash alone: the hash is over
        # raw pixels (codec-independent), so the same screen under png and jpeg
        # shares a hash while the cached payload bytes differ.
        self._entries: OrderedDict[tuple[str, str], dict] = OrderedDict()
        # params_key (scope|format|quality|max_long_edge) -> the hash most recently SEEN under those
        # params (stored or matched), i.e. the best if_none_match candidate for a
        # session that has no frame of its own yet.
        self._last: dict[str, str] = {}
        self._max = max(1, int(max_entries))
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def candidate(self, params_key: str) -> tuple[str, dict] | None:
        """The best ``(frame_hash, frame)`` to offer as ``if_none_match`` for these
        capture params, or None. The returned frame reference stays valid even if
        the entry is evicted afterwards (the caller holds it across the RPC)."""
        with self._lock:
            h = self._last.get(params_key)
            if h is None:
                return None
            frame = self._entries.get((params_key, h))
            if frame is None:
                return None
            self._entries.move_to_end((params_key, h))
            return h, frame

    def lookup(self, params_key: str, frame_hash: str) -> dict | None:
        """The cached frame for these params + content hash, or None (refreshes
        LRU recency)."""
        with self._lock:
            frame = self._entries.get((params_key, frame_hash))
            if frame is not None:
                self._entries.move_to_end((params_key, frame_hash))
            return frame

    def put(self, params_key: str, frame_hash: str, frame: dict) -> None:
        """Store a full frame under its params + content hash and make it the
        candidate for ``params_key``; evicts least-recently-used entries past the cap."""
        with self._lock:
            self._entries[(params_key, frame_hash)] = frame
            self._entries.move_to_end((params_key, frame_hash))
            self._last[params_key] = frame_hash
            while len(self._entries) > self._max:
                (evicted_key, evicted_hash), _ = self._entries.popitem(last=False)
                # drop a dangling candidate pointer so candidate() stays honest
                if self._last.get(evicted_key) == evicted_hash:
                    del self._last[evicted_key]

    def touch(self, params_key: str, frame_hash: str) -> None:
        """Record a confirmed hit: ``frame_hash`` is the live content under these
        params (keeps the candidate pointer fresh across interleaved sessions)."""
        with self._lock:
            if (params_key, frame_hash) in self._entries:
                self._entries.move_to_end((params_key, frame_hash))
                self._last[params_key] = frame_hash

    def record(self, hit: bool) -> None:
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1

    @property
    def hit_rate(self) -> float | None:
        """hits / (hits + misses), or None before any dedup-enabled screenshot."""
        with self._lock:
            total = self.hits + self.misses
            return (self.hits / total) if total else None


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
        frame_cache: FrameCache | None = None,
        screenshot_dedup: bool | None = None,
    ) -> None:
        self._ws = ws
        self.capabilities = capabilities
        self.platform_name = platform
        # Content-negotiated screenshots (if_none_match). The frame cache is private
        # per session by default; pass ONE shared shinken.FrameCache() to many
        # sessions so forked replicas dedup against each other's frames. The default
        # dedup posture comes from the ctor knob, falling back to the
        # SHINKEN_SCREENSHOT_DEDUP env var; screenshot(dedup=...) overrides per call.
        self._frame_cache = frame_cache if frame_cache is not None else FrameCache(max_entries=8)
        self._dedup_default = (
            _env_dedup_default() if screenshot_dedup is None else bool(screenshot_dedup)
        )
        # This session's own last full frame per params_key: (frame_hash, frame dict).
        # Preferred over the shared cache's candidate, so a replica that diverged from
        # the fleet re-converges on ITS OWN content after one miss.
        self._last_shot: dict[str, tuple[str, dict]] = {}
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
        # Act-returns-observation futures (#observe-after-act): an action sent with
        # ``observe`` gets TWO correlated replies on the same call_id — the ack, then
        # the observation (cause = call_id). Both futures are registered BEFORE the
        # send; the demux resolves ``_pending`` first (the ack) and falls back here
        # (the follow-up), so the strictly-ordered wire can never race a re-register.
        self._pending_obs: dict[str, asyncio.Future] = {}
        self._pong_waiters: deque[asyncio.Future] = deque()
        # Streamed-exec demux (G1): call_id -> queue of exec_output/exec_exit events
        # (or a ConnectionError on close). Registered by exec_stream BEFORE the send.
        self._exec_streams: dict[str, asyncio.Queue] = {}
        self._frames: asyncio.Queue = asyncio.Queue(maxsize=_FRAME_QUEUE_MAX)
        # Frame-budget accounting (GLOBAL_FRAME_BUDGET_BYTES): resident size of each queued
        # FRAME in queue order (sentinels untracked), plus this loop's shared state (lazy).
        self._queued_bytes: deque[int] = deque()
        self._budget_state: dict | None = None
        self._reader: asyncio.Task | None = None
        # Set by close() (idempotent); every subsequent wire-touching method raises the
        # typed SessionClosed immediately instead of failing obscurely (or, on the sync
        # facade, deadlocking against a stopped loop).
        self._closed = False
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

    def _ensure_open(self) -> None:
        """Raise the typed :class:`~shinken.errors.SessionClosed` if close() ran."""
        if self._closed:
            raise SessionClosed(
                "session is closed (close() was already called); open a new session "
                "via connect()/provider.connect()"
            )

    def _start_reader(self) -> None:
        self._reader = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    # Binary media frames (negotiated via hello.accept.binary_frames)
                    # carry raw image bytes after a small JSON header; everything else
                    # is a JSON text message. wire_len records the on-the-wire size of
                    # observations so bandwidth accounting can use real numbers.
                    if isinstance(raw, bytes | bytearray):
                        msg = _parse_binary_frame(bytes(raw))
                    else:
                        msg = json.loads(raw)
                        if msg.get("type") == "observation":
                            msg["wire_len"] = len(raw)
                    self._dispatch(msg)
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
                item = self._frames.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._account_popped(item)

    def _loop_budget_state(self) -> dict:
        """This event loop's shared frame-budget accounting (created lazily, cached)."""
        state = self._budget_state
        if state is None:
            loop = asyncio.get_running_loop()
            state = _FRAME_BUDGET_STATES.get(loop)
            if state is None:
                state = {"used": 0, "frames": 0, "order": deque()}
                _FRAME_BUDGET_STATES[loop] = state
            self._budget_state = state
        return state

    def _account_popped(self, item: object) -> None:
        """Release the budget accounting for one item popped off the frame queue.
        Sentinels are untracked; ``_queued_bytes`` parallels the queue's frame
        subsequence, so the popped frame's size is always its head."""
        if item is _STREAM_END or item is _STREAM_LOST or not self._queued_bytes:
            return
        size = self._queued_bytes.popleft()
        if self._budget_state is not None:
            self._budget_state["used"] -= size
            self._budget_state["frames"] -= 1

    def _drop_oldest_frame(self) -> None:
        """Drop this queue's oldest item (frame or sentinel) and release its accounting."""
        try:
            item = self._frames.get_nowait()
        except asyncio.QueueEmpty:
            return
        self._account_popped(item)

    def _push_frame(self, item: object) -> None:
        """Bound the client-side frame queue: drop-oldest at ``_FRAME_QUEUE_MAX`` per
        connection, plus the opt-in per-loop byte budget (``GLOBAL_FRAME_BUDGET_BYTES``)
        shared across every AsyncSandbox on this event loop — see
        docs/engineering/many-sandbox-concurrency.md §2. Always runs on the owning loop's
        thread (reader task / session coroutines), so the shared accounting needs no locks."""
        if self._frames.full():
            self._drop_oldest_frame()
        try:
            self._frames.put_nowait(item)
        except asyncio.QueueFull:  # pragma: no cover — full() cleared just above
            return
        if item is _STREAM_END or item is _STREAM_LOST:
            return  # sentinels are never accounted and never evicted by the budget
        state = self._loop_budget_state()
        size = _frame_resident_bytes(item)
        self._queued_bytes.append(size)
        state["used"] += size
        state["frames"] += 1
        if GLOBAL_FRAME_BUDGET_BYTES is not None:
            state["order"].append(self)
            _evict_over_budget(state)

    def _dispatch(self, msg: dict) -> None:
        kind = msg.get("type")
        # An unsolicited stream frame is never an RPC reply — route it to the queue.
        if kind == "observation" and msg.get("stream") is not None:
            self._push_frame(msg)
            return
        # Streamed-exec events (G1): routed to their exec's queue by `cause`. The
        # queue is registered BEFORE the action is sent, so no event can slip by.
        if kind in ("exec_output", "exec_exit"):
            q = self._exec_streams.get(msg.get("cause"))
            if q is not None:
                q.put_nowait(msg)
            else:
                _log.debug("dropping exec event for unknown cause %s", msg.get("cause"))
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
        if fut is None and cid is not None:
            # Second correlated reply on one call_id: the act-returns-observation
            # follow-up (the ack already consumed the primary future).
            fut = self._pending_obs.pop(cid, None)
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
        for fut in self._pending_obs.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending_obs.clear()
        while self._pong_waiters:
            fut = self._pong_waiters.popleft()
            if not fut.done():
                fut.set_exception(exc)
        # Wake live exec streams: the consumer reads the exception off its queue, so
        # a connection death mid-stream surfaces instead of hanging until timeout.
        for q in self._exec_streams.values():
            q.put_nowait(exc)
        self._exec_streams.clear()

    async def _rpc(self, msg: dict, timeout: float | None = None) -> dict:
        """Send a call_id-bearing message and await its correlated reply, bounded by a
        timeout (#142) so a missing, dropped, or uncorrelated reply can't hang an async
        caller; the pending future is also failed on connection close (``_fail_pending``)."""
        self._ensure_open()
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
        self._ensure_open()
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
        self,
        capability: str,
        subject: str,
        decision: str,
        granted: bool,
        reason: str | None,
        detail: dict | None = None,
    ) -> None:
        """Append one capability/permission decision event (#83/E4). ``decision`` is the
        gateway verdict (``allow``/``ask``/``deny``); ``granted`` is whether the action was
        ultimately permitted (an ``ask`` resolves to granted or not via ``on_ask``).
        ``detail`` is the verb-specific audit payload — exec records its argv/shell."""
        event = {
            "type": "capability",
            "capability": capability,
            "subject": subject,
            "decision": decision,
            "granted": granted,
            "reason": reason,
            "ts": time.time(),
        }
        if detail is not None:
            event["detail"] = detail
        self._capability_events.append(event)

    def _gate(self, verb: str, detail: dict | None = None) -> None:
        """Action Gateway check (#84/#7): when enforcing, decide allow / deny / **ask**
        before dispatch — so a denied or unapproved verb never reaches shinkend, and record
        the decision as a capability event (#83). A capability valued ``"ask"`` is *risky*:
        it pauses for approval via the ``on_ask`` handler (default: deny). GUI input passes
        when input_automation is granted (the default). ``detail`` rides the recorded
        event (exec puts its argv/shell there, so the audit shows WHAT was run)."""
        if not self._enforce:
            return
        decision, cap, reason = decide_action(verb, self.sandbox_capabilities)
        if decision == "allow":
            self._record_decision(cap, verb, "allow", True, None, detail)
            return
        if decision == "ask":
            granted = bool(self._on_ask(verb, cap, reason)) if self._on_ask else False
            self._record_decision(cap, verb, "ask", granted, None if granted else reason, detail)
            if not granted:
                raise CapabilityDenied(f"{verb}: approval denied ({cap})")
            return
        self._record_decision(cap, verb, "deny", False, reason, detail)
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

    def _observe_spec(self, observe: Any) -> dict | None:
        """Normalize + validate an ``observe=`` argument (act-returns-observation).
        ``True`` → runtime defaults; a dict admits only the screenshot-shaped keys
        (``format``/``quality``/``max_long_edge``/``scope``). Rejected with a typed
        :class:`ValueError` BEFORE anything goes on the wire when the runtime's
        welcome did not advertise ``capabilities.observe_after_act`` (old runtimes
        never see the field) or the requested codec is not advertised."""
        if observe is None or observe is False:
            return None
        if not self.capabilities.observe_after_act:
            raise ValueError(
                "observe-after-act not advertised by the runtime "
                "(capabilities.observe_after_act); take a separate screenshot instead"
            )
        spec = {} if observe is True else dict(observe)
        unknown = set(spec) - _OBSERVE_KEYS
        if unknown:
            raise ValueError(f"unknown observe keys: {sorted(unknown)}")
        self._check_image_format(spec.get("format"))
        return spec

    async def act(
        self, verb: str, target: dict | None = None, observe: Any = None, **kwargs: Any
    ) -> dict:
        """Send one typed action and await its reply. Raises on failure.

        ``observe`` (act-returns-observation) asks the runtime to follow the ack with
        a fresh observation correlated by the same call_id — one round trip instead of
        act + screenshot. Pass ``True`` for runtime defaults or a dict of the
        screenshot-shaped capture levers (``format``/``quality``/``max_long_edge``/
        ``scope``). When set, the OBSERVATION dict (same shape as :meth:`screenshot`)
        is returned instead of the ack. Only mutating verbs admit it; requires the
        runtime's ``capabilities.observe_after_act``."""
        self._gate(verb)
        obs_spec = self._observe_spec(observe)
        if obs_spec is not None:
            # The follow-up is a capture: gate it like one, so a session without the
            # screenshot capability can't smuggle pixels out via observe.
            self._gate("screenshot")
        action: dict = {"verb": verb}
        if target is not None:
            action["target"] = target
        if obs_spec is not None:
            action["observe"] = obs_spec
        action.update({k: v for k, v in kwargs.items() if v is not None})
        call_id = self._next_id()
        obs_fut: asyncio.Future | None = None
        if obs_spec is not None:
            # Register the follow-up future BEFORE the send: the wire is strictly
            # ordered (ack then observation), and _dispatch resolves _pending first,
            # falling back here — so the observation can never slip past a window
            # between the ack resolving and a late registration.
            obs_fut = asyncio.get_running_loop().create_future()
            self._pending_obs[call_id] = obs_fut
        try:
            reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
            # A successful one-shot `screenshot` is answered with an `observation` (which
            # has no `ok` field), not an `ack` — so an adapter-translated model screenshot
            # tool-call routed through act()/act_batch() must not be treated as a failure.
            ok = True if reply.get("type") == "observation" else reply.get("ok")
            if not ok:
                raise _action_error(reply, verb)
        except BaseException:
            # A nacked/failed action never gets a follow-up — drop the orphan future.
            if obs_fut is not None:
                self._pending_obs.pop(call_id, None)
            raise
        self._action_count += 1
        if obs_fut is None:
            return reply
        try:
            obs_reply = await asyncio.wait_for(obs_fut, self._rpc_timeout)
        except asyncio.TimeoutError:
            self._pending_obs.pop(call_id, None)
            raise TimeoutError(
                f"observe follow-up for {verb!r} ({call_id}) timed out after {self._rpc_timeout}s"
            ) from None
        if obs_reply.get("type") != "observation":
            # The action landed but the capture failed: the runtime sent a typed
            # error result instead of the observation.
            raise RuntimeError(obs_reply.get("error", f"observe after {verb!r} failed"))
        return _observation_dict(obs_reply)

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

    async def step(
        self,
        actions: list[dict],
        *,
        observe: dict | None = None,
        step_id: str | None = None,
    ) -> dict:
        """Execute one agent step — k actions plus an optional trailing observation — in
        **~ONE round-trip** instead of k+1 (docs/engineering/many-sandbox-concurrency.md §5).

        All k action frames (and the trailing ``screenshot`` when ``observe`` is given)
        are sent WITHOUT awaiting a reply between them; the k(+1) correlated replies are
        then awaited in order. shinkend processes a connection's messages strictly in
        order and the reader/demux correlates replies by ``call_id``, so this needs **no
        protocol change**: the wire carries the exact same standard ``action`` frames as
        ``act()``. (The alternative — a server-side batch verb — was rejected: the
        schema's ``ActionBatch`` is deliberately a client-side convention, NOT a wire
        message, and a new wire verb would buy no latency over pipelining while growing
        the contract surface.)

        ``observe`` is a screenshot parameter dict (``scope`` / ``format`` / ``quality``
        / ``max_long_edge``), e.g. ``{"format": "jpeg", "quality": 80, "max_long_edge":
        1024}`` — the post-step observation comes back **fused in the same exchange**,
        decoded like :meth:`screenshot`. ``None`` skips it. An unadvertised ``format``
        raises :class:`ValueError` before anything is sent.

        **Honest failure semantics — there is no ``skipped`` here.** Unlike
        ``act_batch(stop_on_error=True)``, every action is already on the wire when an
        earlier one fails, and the runtime executes the rest regardless; each result row
        therefore reports that action's REAL outcome with the #56 taxonomy (``ok | error
        | timeout | sandbox_died``), and the trailing observation still returns after a
        mid-step per-action error. The one exception is a local gateway denial — decided
        client-side, before dispatch — which yields an ``error`` row whose action never
        reached the runtime (the remaining actions still ship; pipelined dispatch has no
        early stop). A denied ``observe`` yields ``observation=None`` plus
        ``observation_error`` without suppressing the actions. If the connection dies
        mid-step, replies already received keep their results and the rest classify
        ``sandbox_died`` — whether those actions executed before the death is unknowable
        from the client, exactly like any other write that raced a crash.

        Returns ``{step_id, completed, failure_kind, results: [{index, verb, ok, status,
        action_id/ack | error}], observation, observation_error}`` — ``completed`` is
        True when every action row carries a deliberate outcome (``ok``/``error``; no
        ``timeout``/``sandbox_died``), and ``failure_kind`` mirrors the first failing
        row's status like continue-mode :meth:`act_batch`."""
        self._ensure_open()
        sid = step_id or f"step-{uuid.uuid4().hex[:8]}"
        if observe is not None:
            self._check_image_format(observe.get("format"))
        loop = asyncio.get_running_loop()
        rows: list[dict | None] = [None] * len(actions)
        sends: list[tuple[int, str, dict]] = []  # (index, call_id, wire message)
        for i, a in enumerate(actions):
            verb = a.get("verb")
            try:
                if not verb:
                    raise ValueError("action has no 'verb'")
                self._gate(verb)
            except Exception as exc:
                # Never dispatched (malformed, or gateway-denied before the wire).
                rows[i] = {
                    "index": i,
                    "verb": verb,
                    "ok": False,
                    "status": classify_exception(exc),
                    "error": str(exc),
                }
                continue
            action: dict = {"verb": verb}
            if a.get("target") is not None:
                action["target"] = a["target"]
            action.update(
                {k: v for k, v in a.items() if k not in ("verb", "target") and v is not None}
            )
            cid = self._next_id()
            sends.append((i, cid, {"type": "action", "call_id": cid, "action": action}))
        observation: dict | None = None
        observation_error: str | None = None
        obs_cid: str | None = None
        wire: list[tuple[int | None, str, dict]] = list(sends)
        if observe is not None:
            try:
                self._gate("screenshot")
                action = {"verb": "screenshot"}
                action.update({k: v for k, v in observe.items() if v is not None})
                obs_cid = self._next_id()
                msg = {"type": "action", "call_id": obs_cid, "action": action}
                wire.append((None, obs_cid, msg))
            except CapabilityDenied as exc:
                observation_error = str(exc)
        # Register EVERY future before the first send: a reply can land while later
        # sends are still going out, and the demux must already know the call_id.
        futs: dict[str, asyncio.Future] = {}
        for _i, cid, _msg in wire:
            fut = loop.create_future()
            self._pending[cid] = fut
            futs[cid] = fut
        # Pipelined dispatch: every frame goes out before ANY reply is awaited. A send
        # failure mid-burst promptly fails this step's still-pending futures so the
        # await loop below classifies them at once (instead of burning one RPC timeout
        # each); replies that already landed keep their results.
        for _i, _cid, msg in wire:
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as exc:
                err: Exception = exc
                if isinstance(exc, _WsConnectionClosed):
                    err = ConnectionError(
                        f"connection closed sending {msg['action'].get('verb')!r}"
                    )
                    err.__cause__ = exc
                for cid, fut in futs.items():
                    self._pending.pop(cid, None)
                    if not fut.done():
                        fut.set_exception(err)
                break
        timeout = self._rpc_timeout
        for i, cid, msg in sends:
            verb = msg["action"]["verb"]
            try:
                reply = await asyncio.wait_for(futs[cid], timeout)
            except asyncio.TimeoutError:
                self._pending.pop(cid, None)
                rows[i] = {
                    "index": i,
                    "verb": verb,
                    "ok": False,
                    "status": "timeout",
                    "error": f"reply for {verb!r} ({cid}) timed out after {timeout}s",
                }
                continue
            except Exception as exc:
                rows[i] = {
                    "index": i,
                    "verb": verb,
                    "ok": False,
                    "status": classify_exception(exc),
                    "error": str(exc),
                }
                continue
            # A successful one-shot `screenshot` inside the step is answered with an
            # `observation` (no `ok` field), like act() handles it.
            ok = True if reply.get("type") == "observation" else reply.get("ok")
            if ok:
                self._action_count += 1
                rows[i] = {
                    "index": i,
                    "verb": verb,
                    "ok": True,
                    "status": "ok",
                    "action_id": reply.get("call_id") or reply.get("cause"),
                    "ack": reply,
                }
            else:
                rows[i] = {
                    "index": i,
                    "verb": verb,
                    "ok": False,
                    "status": "error",
                    "error": reply.get("error", f"action {verb!r} failed"),
                }
        if obs_cid is not None:
            try:
                reply = await asyncio.wait_for(futs[obs_cid], timeout)
                if reply.get("type") == "observation":
                    observation = _image_payload(reply)
                else:
                    observation_error = reply.get("error", "screenshot failed")
            except asyncio.TimeoutError:
                self._pending.pop(obs_cid, None)
                observation_error = f"observation ({obs_cid}) timed out after {timeout}s"
            except Exception as exc:
                observation_error = str(exc)
        failure_kind = next((r["status"] for r in rows if r and not r["ok"]), None)
        completed = all(r is not None and r["status"] in ("ok", "error") for r in rows)
        return {
            "step_id": sid,
            "completed": completed,
            "failure_kind": failure_kind,
            "results": rows,
            "observation": observation,
            "observation_error": observation_error,
        }

    async def click(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return await self.act("click", _target(target, x, y), observe=observe)

    async def double_click(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return await self.act("double_click", _target(target, x, y), observe=observe)

    async def right_click(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return await self.act("right_click", _target(target, x, y), observe=observe)

    async def move(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return await self.act("move", _target(target, x, y), observe=observe)

    async def drag(
        self,
        target: Any = None,
        to: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        to_x: float | None = None,
        to_y: float | None = None,
        duration_ms: int | None = None,
        button: str | None = None,
        observe: Any = None,
    ):
        """Drag: pointer down at the source, interpolated moves, button up at the
        destination. Source/destination are targets (or ``x``/``y`` + ``to_x``/``to_y``
        pixel pairs); ``duration_ms`` spreads the gesture over time (omitted = fastest;
        the runtime clamps absurd values) and ``button`` picks ``left`` (default) /
        ``middle`` / ``right``."""
        src = _target(target, x, y)
        if src is None:
            raise ValueError("drag requires a source (target= or x=/y=)")
        dst = _target(to, to_x, to_y)
        if dst is None:
            raise ValueError("drag requires a destination (to= or to_x=/to_y=)")
        return await self.act(
            "drag", src, to=dst, duration_ms=duration_ms, button=button, observe=observe
        )

    async def mouse_down(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str | None = None,
        observe: Any = None,
    ):
        """Press (and hold) a pointer button — the decomposed half of a free-form
        gesture (down → moves → up). With a target the pointer moves there first;
        without one it presses at the current position."""
        return await self.act("mouse_down", _target(target, x, y), button=button, observe=observe)

    async def mouse_up(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str | None = None,
        observe: Any = None,
    ):
        """Release a pointer button (see :meth:`mouse_down`)."""
        return await self.act("mouse_up", _target(target, x, y), button=button, observe=observe)

    async def scroll(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        dy: float = 0.0,
        observe: Any = None,
    ):
        return await self.act("scroll", _target(target, x, y), dy=dy, observe=observe)

    async def list_windows(self) -> list[dict]:
        """Enumerate visible top-level windows — the Linux "enumerate apps" read
        primitive (EWMH ``_NET_CLIENT_LIST`` / ``_NET_WM_NAME`` / ``_NET_ACTIVE_WINDOW``
        guest-side). Each entry is ``{id, title, pid, x, y, w, h, focused}``; ``id`` is
        usable as the ``window:<id>`` capture scope."""
        return await self.query("list_windows")

    def _exec_action(
        self,
        argv: Any,
        shell: str | None,
        cwd: str | None,
        env: dict | None,
        timeout: float | None,
        stdin: str | None,
        stream: bool = False,
    ) -> tuple[dict, float]:
        """Validate + gate one exec request (G1) BEFORE anything goes on the wire;
        returns ``(action dict, reply deadline seconds)``. Typed failures: exactly one
        of argv/shell, a runtime that doesn't advertise the verb, a gateway denial
        (recorded with the argv/shell in the event's ``detail``)."""
        if (argv is None) == (shell is None):
            raise ValueError(
                "exec takes exactly one of argv (the default, no shell) or "
                "shell (the explicit opt-in)"
            )
        if argv is not None and (not isinstance(argv, list | tuple) or len(argv) == 0):
            raise ValueError("exec argv must be a non-empty list of strings")
        if "exec" not in (self.capabilities.verbs or []):
            raise UnknownVerb(
                "exec verb not advertised by this runtime (pre-exec shinkend): "
                "fall back to the substrate's out-of-band channel"
            )
        detail = {"argv": [str(a) for a in argv]} if argv is not None else {"shell": str(shell)}
        self._gate("exec", detail=detail)
        action: dict = {"verb": "exec", **detail}
        if cwd is not None:
            action["cwd"] = str(cwd)
        if env:
            action["env"] = {str(k): str(v) for k, v in env.items()}
        if timeout is not None:
            action["timeout_ms"] = max(1, int(timeout * 1000))
        if stdin is not None:
            action["stdin"] = stdin
        if stream:
            action["stream"] = True
        # The reply can take as long as the command runs: wait out the guest-side
        # deadline (the runtime default when unspecified) plus a grace, never less
        # than the ordinary RPC timeout.
        deadline = (timeout if timeout is not None else DEFAULT_EXEC_TIMEOUT) + _EXEC_RPC_GRACE
        return action, max(self._rpc_timeout, deadline)

    async def exec(
        self,
        argv: list[str] | None = None,
        *,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> dict:
        """Run one command **inside the Sandbox** over the ACI (the typed in-band exec
        channel, G1) and return its typed result.

        ``argv`` (default form) executes the program directly — no shell
        interpretation; ``shell`` is the explicit opt-in, run via the guest's
        ``/bin/sh -c``. Exactly one of the two. ``cwd``/``env`` shape the child;
        ``timeout`` (seconds) is the guest-side kill-the-process-group deadline
        (runtime default 60 s); ``stdin`` is written to the child and closed.

        Returns ``{'exit_code', 'signal', 'timed_out', 'stdout', 'stderr',
        'stdout_truncated', 'stderr_truncated', 'duration_ms'}`` — a NONZERO exit
        code is the command's outcome, returned, not raised; a timeout is reported
        honestly (``timed_out=True``, ``exit_code=None``). stdout/stderr are UTF-8
        with lossy replacement and per-channel caps (use :meth:`exec_stream` on a
        binary-framed session for byte-exact output). Raises :class:`RuntimeError`
        on a runtime that doesn't advertise the verb or a spawn failure, and
        :class:`~shinken.gateway.CapabilityDenied` when the ``exec`` capability is
        not granted (the decision event records the argv/shell)."""
        action, deadline = self._exec_action(argv, shell, cwd, env, timeout, stdin)
        reply = await self._rpc(
            {"type": "action", "call_id": self._next_id(), "action": action},
            timeout=deadline,
        )
        if reply.get("type") == "result" and reply.get("ok"):
            self._action_count += 1
            return reply.get("value") or {}
        raise RuntimeError(reply.get("error", "exec failed"))

    async def exec_stream(
        self,
        argv: list[str] | None = None,
        *,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ):
        """Run one in-guest command with **incremental output** (G1 streamed form):
        an async iterator of chunk dicts ``{'channel': 'stdout'|'stderr', 'data':
        bytes, 'seq': int}`` (raw bytes — base64-decoded on text sessions, sliced
        straight out of binary frames on a binary-negotiated one), terminated by one
        ``{'channel': 'exit', 'exit_code', 'signal', 'timed_out', 'duration_ms',
        'truncated'}``. ``truncated`` means the runtime's total output budget dropped
        later chunks. Raises like :meth:`exec` (plus a typed error when the spawn
        fails after the ack, and :class:`ConnectionError` if the session dies
        mid-stream)."""
        action, deadline = self._exec_action(argv, shell, cwd, env, timeout, stdin, stream=True)
        call_id = self._next_id()
        # Register the event queue BEFORE the send: the wire orders ack before the
        # first exec_output, and the demux must already know the cause either way.
        q: asyncio.Queue = asyncio.Queue()
        self._exec_streams[call_id] = q
        try:
            reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error", "exec failed"))
            self._action_count += 1
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), deadline)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"exec stream ({call_id}) produced no event within {deadline}s"
                    ) from None
                if isinstance(item, Exception):
                    raise item
                if item.get("type") == "exec_output":
                    data = item.get("data")
                    if data is None:
                        data = base64.b64decode(item.get("data_b64", ""))
                    yield {
                        "channel": item.get("channel"),
                        "data": bytes(data),
                        "seq": item.get("seq"),
                    }
                    continue
                # exec_exit — the terminal event (a spawn failure raises typed).
                if item.get("error"):
                    raise RuntimeError(item["error"])
                yield {
                    "channel": "exit",
                    "exit_code": item.get("exit_code"),
                    "signal": item.get("signal"),
                    "timed_out": bool(item.get("timed_out", False)),
                    "duration_ms": item.get("duration_ms"),
                    "truncated": bool(item.get("truncated", False)),
                }
                return
        finally:
            self._exec_streams.pop(call_id, None)

    async def clipboard_get(self) -> str:
        """Read the guest clipboard as text (the ``clipboard_get`` verb; v1 is
        text-only and size-capped guest-side). The runtime answers with a typed
        ``result`` carrying ``{text}``; an empty clipboard or an unsupported backend
        raises with the runtime's typed error. Gated on the ``clipboard``
        capability (default-off) when enforcing."""
        reply = await self.act("clipboard_get")
        return (reply.get("value") or {}).get("text", "")

    async def clipboard_set(self, text: str, *, observe: Any = None):
        """Set the guest clipboard to ``text`` (Linux v1: shinkend becomes the X11
        CLIPBOARD selection owner — no xclip). Mutating: supports
        ``observe`` (act-returns-observation). Gated on the ``clipboard``
        capability (default-off) when enforcing."""
        return await self.act("clipboard_set", text=text, observe=observe)

    async def launch_app(self, app: str, args: list[str] | None = None, *, observe: Any = None):
        """Start an application on the sandbox desktop: ``app`` is an executable
        name (guest PATH) or absolute path, ``args`` the argv tail — spawned
        detached with the session environment ($DISPLAY + session D-Bus), never
        through a shell. Find the new window via :meth:`list_windows` (title /
        ``pid``). Mutating: supports ``observe``. Gated on the ``app_launch``
        capability when enforcing."""
        return await self.act("launch_app", app=app, args=args, observe=observe)

    async def activate_window(
        self,
        window_id: int | None = None,
        *,
        app: str | None = None,
        observe: Any = None,
    ):
        """Raise + focus a window: by ``window_id`` (a :meth:`list_windows` id) or by
        ``app`` (the first window whose title contains it, case-insensitive). Linux
        v1 sends the EWMH ``_NET_ACTIVE_WINDOW`` client message, with a raise+focus
        fallback on WM-less displays. Mutating: supports ``observe``."""
        if window_id is None and app is None:
            raise ValueError("activate_window requires window_id or app")
        return await self.act("activate_window", window_id=window_id, app=app, observe=observe)

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
        max_long_edge: int | None = None,
        dedup: bool | None = None,
    ) -> dict:
        """Capture pixels on demand.

        ``format`` selects the wire codec: ``None``/``"png"`` (lossless default) or
        ``"jpeg"`` (the bandwidth lever — a 1080p desktop is ~1.8 MB as PNG vs ~0.1 MB as
        quality-80 JPEG). ``quality`` (1–100) tunes JPEG; ignored for PNG.
        ``max_long_edge`` caps the frame's longer edge (px) — the downscale bandwidth
        lever, same as on :meth:`astart_screencast` (the runtime has always accepted it
        on the ``screenshot`` action; the facade simply lacked the parameter).

        ``dedup`` turns on content-negotiated observation: the request carries
        ``if_none_match`` with the last seen ``frame_hash`` (this session's, or — with
        a shared :class:`FrameCache` — any session's on the same cache), and when the
        captured frame's raw-pixel hash matches, the runtime answers a ~200-byte
        ``not_modified`` instead of re-sending the payload; the SDK transparently
        returns the cached frame with ``deduped=True``. ``None`` defers to the
        session default (``connect(screenshot_dedup=…)`` / ``$SHINKEN_SCREENSHOT_DEDUP``).
        Silently inactive against a runtime that doesn't advertise
        ``capabilities.frame_dedup`` (no ``if_none_match`` ever goes on the wire —
        pre-dedup runtimes reject unknown action fields).

        Returns ``{'bytes', 'format', 'w', 'h', 'deduped', …}`` — ``bytes`` is the raw
        encoded image and ``format`` says which codec; ``frame_hash`` is present when
        the runtime computed one. When the codec is PNG, ``'png'`` is kept as a
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
        if max_long_edge is not None:
            action["max_long_edge"] = max_long_edge
        # Content negotiation, capability-gated: only a frame_dedup runtime ever sees
        # if_none_match (older ones reject unknown action fields). Prefer this
        # session's own last frame (fast re-convergence after divergence), then the
        # shared cache's candidate (the cross-replica fleet case). The candidate
        # frame reference is held across the RPC, so a concurrent LRU eviction can
        # never strand a not_modified answer. The cache key carries every parameter
        # that changes the encoded frame — including max_long_edge, so a downscaled
        # request can never be served a cached native-resolution frame.
        use_dedup = (self._dedup_default if dedup is None else bool(dedup)) and bool(
            self.capabilities.frame_dedup
        )
        key = f"{scope}|{format or 'png'}|{quality}|{max_long_edge}"
        candidate: tuple[str, dict] | None = None
        if use_dedup:
            candidate = self._last_shot.get(key) or self._frame_cache.candidate(key)
            if candidate is not None:
                action["if_none_match"] = candidate[0]
        reply = await self._rpc({"type": "action", "call_id": call_id, "action": action})
        if reply.get("type") != "observation":
            raise RuntimeError(reply.get("error", "screenshot failed"))
        if reply.get("not_modified"):
            if candidate is None:  # never offered a hash — a protocol violation
                raise RuntimeError("runtime sent not_modified to a request without if_none_match")
            frame_hash, frame = candidate
            self._last_shot[key] = candidate
            self._frame_cache.touch(key, frame_hash)
            self._frame_cache.record(hit=True)
            return {
                **frame,
                "deduped": True,
                "frame_hash": reply.get("frame_hash", frame_hash),
                # the wire really moved only the compact not_modified observation
                "wire_len": reply.get("wire_len"),
            }
        out = _image_payload(reply)
        out["deduped"] = False
        frame_hash = out.get("frame_hash")
        if frame_hash is not None and use_dedup:
            # cache the frame WITHOUT the per-reply fields, keyed by content
            frame = {k: v for k, v in out.items() if k not in ("deduped", "wire_len")}
            self._last_shot[key] = (frame_hash, frame)
            self._frame_cache.put(key, frame_hash, frame)
        if use_dedup:
            self._frame_cache.record(hit=False)
        return out

    async def type_text(self, text: str, *, observe: Any = None):
        return await self.act("type_text", text=text, observe=observe)

    async def key(self, keys: str, *, observe: Any = None):
        return await self.act("key", keys=keys, observe=observe)

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
        keyframe is the consumer's job (the SDK takes no imaging dependency).

        Deliberately NOT gated on ``close()``: the stream sentinels stay readable
        after close, so a consumer still observes the END/LOST signal instead of a
        :class:`~shinken.errors.SessionClosed` masking how the stream ended."""
        try:
            item = (
                await asyncio.wait_for(self._frames.get(), timeout)
                if timeout is not None
                else await self._frames.get()
            )
        except asyncio.TimeoutError:
            return None
        self._account_popped(item)  # release the consumed frame's budget bytes
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
                        "bytes": _image_bytes(t),
                    }
                    for t in tiles
                ],
                "seq": item.get("seq"),
                "stream": item.get("stream"),
                "wire_len": item.get("wire_len"),
            }
        img = item.get("image") or {}
        raw = _image_bytes(img)  # binary-path raw bytes, or text-path base64 decoded
        frame = {
            "bytes": raw,
            "format": img.get("format", "png"),
            "w": img.get("w"),
            "h": img.get("h"),
            "seq": item.get("seq"),
            "stream": item.get("stream"),
            "wire_len": item.get("wire_len"),
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

    @property
    def handle(self) -> Any:
        """The provider :class:`~shinken.providers.base.SandboxHandle` managing this
        session (attached by ``provider.connect()``), or ``None`` on a bare
        ``aconnect()`` with no provider behind it."""
        return self._handle

    def _require_provider(self, op: str) -> tuple[Any, Any]:
        if self._provider is None or self._handle is None:
            raise ProviderRequired(
                f"{op} needs a provider-managed session; open it via a provider's "
                "connect()/session() instead of a bare connect()"
            )
        return self._provider, self._handle

    async def checkpoint(
        self, name: str | None = None, *, agent_state_ref: str | None = None
    ) -> Checkpoint:
        """Create a runtime checkpoint of this sandbox (#206) and return a first-class
        :class:`Checkpoint` handle.

        The provider snapshots substrate state; the returned :class:`Checkpoint` is a
        ``str`` subclass equal to the checkpoint id (every legacy str-consumer keeps
        working) and carries the provider + spec-compat info, with ``spawn()`` /
        ``spawn_many()`` / ``delete()`` methods.

        Requires a provider-managed session (use a provider's ``connect()``), else
        raises the typed :class:`~shinken.errors.ProviderRequired`."""
        provider, handle = self._require_provider("checkpoint()")
        checkpoint_id = await asyncio.to_thread(
            provider.checkpoint,
            handle,
            name=name,
            event_seq=None,
            agent_state_ref=agent_state_ref,
        )
        spec = None
        spec_of = getattr(provider, "snapshot_spec", None)
        if spec_of is not None:
            with contextlib.suppress(Exception):
                spec = spec_of(checkpoint_id)
        return Checkpoint(checkpoint_id, provider=provider, name=name, spec=spec)

    async def fork(self) -> Any:
        """Fork the current provider-managed sandbox and return the new provider handle
        (see the sync facade's :meth:`Sandbox.spawn` for the fork-and-connect one-liner).
        Raises :class:`~shinken.errors.ProviderRequired` without a provider context."""
        provider, handle = self._require_provider("fork()")
        return await asyncio.to_thread(provider.fork, handle)

    async def resume(self, handle_or_checkpoint: Any) -> Any:
        """**Deprecated alias of the provider's** ``restore`` **verb** — kept for
        back-compat. RESTORE semantics: materializes a NEW live sandbox from a
        snapshot/checkpoint id and returns its provider handle. It does **not**
        un-pause this session — calling it while the source sandbox is alive mints a
        SIBLING, not the same sandbox. Prefer ``Checkpoint.spawn()`` /
        ``provider.restore()``."""
        if self._provider is None:
            raise ProviderRequired("resume() needs a provider-managed session")
        return await asyncio.to_thread(self._provider.resume, handle_or_checkpoint)

    async def destroy(self) -> None:
        """Tear the sandbox down for real: close the WS session, then ask the managing
        provider to destroy the substrate (``provider.destroy(handle)``). ``close()``
        alone only ends the session — the sandbox keeps running. Raises the typed
        :class:`~shinken.errors.ProviderRequired` when no provider context is attached
        (a bare ``aconnect()``), leaving the session untouched."""
        provider, handle = self._require_provider("destroy()")
        await self.close()
        await asyncio.to_thread(provider.destroy, handle)

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
        self._ensure_open()
        scope = self._gate_file("put_file", sandbox_path)
        ref = self._transfer().put(local_path, sandbox_path, scope=scope)
        return ref.to_event()

    def get_file(
        self, sandbox_path: str, local_path: str, *, expect_sha256: str | None = None
    ) -> dict:
        """Copy a file out of the sandbox to the host, verifying its content hash (#85).

        Raises :class:`~shinken.artifacts.HashMismatch` if ``expect_sha256`` is given and
        the fetched content does not match."""
        self._ensure_open()
        scope = self._gate_file("get_file", sandbox_path)
        ref = self._transfer().get(
            sandbox_path, local_path, expect_sha256=expect_sha256, scope=scope
        )
        return ref.to_event()

    async def close(self) -> None:
        """End the session (idempotent — a second close returns immediately). After
        close, wire-touching methods raise the typed
        :class:`~shinken.errors.SessionClosed`; :meth:`next_frame` still drains the
        stream-end sentinels. The substrate keeps running — :meth:`destroy` is the
        teardown that also reclaims it."""
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            # awaiting a cancelled task raises CancelledError (a BaseException, so plain
            # suppress(Exception) misses it) — suppress it explicitly (#155)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._reader
        with contextlib.suppress(Exception):
            await self._ws.close()
        self._fail_pending(ConnectionError("session closed"))
        # Release this session's frame-budget bytes (a closed session must not pin the
        # shared per-loop budget), but keep the stream sentinels readable: a consumer
        # calling next_frame() after close still sees END/LOST rather than stale frames.
        sentinels = []
        while True:
            try:
                item = self._frames.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STREAM_END or item is _STREAM_LOST:
                sentinels.append(item)
            else:
                self._account_popped(item)
        for sentinel in sentinels:
            with contextlib.suppress(asyncio.QueueFull):
                self._frames.put_nowait(sentinel)
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
    ping_jitter: float = 0.0,
    binary_frames: bool = True,
    frame_cache: FrameCache | None = None,
    screenshot_dedup: bool | None = None,
) -> AsyncSandbox:
    """Open an async session and complete the ACI handshake.

    ``binary_frames`` (default on) offers binary media framing in the hello
    (``accept.binary_frames``): a runtime that advertises ``capabilities.binary_frames``
    then delivers image observations as binary WebSocket frames (raw codec bytes after
    a small JSON header — no base64 inflation, no megabyte JSON parse) and the SDK
    returns the exact same frame/screenshot dicts to callers. A pre-binary runtime
    ignores the offer and keeps text frames, so this is always safe to leave on; pass
    ``False`` to pin the legacy base64-in-JSON path (e.g. for A/B measurements).

    ``frame_cache`` / ``screenshot_dedup`` configure content-negotiated screenshots
    (``if_none_match``): ``screenshot_dedup=True`` makes every ``screenshot()`` dedup
    by default (otherwise ``$SHINKEN_SCREENSHOT_DEDUP`` decides, default off), and
    passing ONE shared :class:`FrameCache` to many sessions lets forked replicas
    dedup against EACH OTHER's frames — see :class:`FrameCache`. Inactive against a
    runtime that doesn't advertise ``capabilities.frame_dedup``.

    ``ping_jitter`` decorrelates keepalive phases across a fleet: the websockets library
    pings every 20 s *from the moment of connect*, so N sessions dialed together ping in
    the same tick forever — an N-packet burst every 20 s. A positive jitter draws this
    connection's ping interval once, at dial time, as ``20.0 + uniform(0, ping_jitter)``
    (distinct periods drift the phases apart permanently). ``0.0`` (default) keeps the
    library default untouched. Recommended ~5–10 s for fleets of 256+ — see
    docs/engineering/many-sandbox-concurrency.md §3.2."""
    ws_kwargs: dict[str, Any] = {"max_size": _MAX_WS_MESSAGE}
    if ping_jitter > 0:
        ws_kwargs["ping_interval"] = _PING_INTERVAL + random.uniform(0.0, ping_jitter)
    try:
        ws = await _ws_connect(_to_uri(addr), **ws_kwargs)
    except (OSError, _WsInvalidHandshake, _WsInvalidURI, asyncio.TimeoutError, TimeoutError) as exc:
        # One typed error for the whole dial-failure zoo (dead addr →
        # ConnectionRefusedError, half-open port → InvalidMessage, bad scheme →
        # InvalidURI, …), still a ConnectionError subclass for legacy handlers.
        raise ConnectError(f"could not connect to shinkend at {addr}: {exc}") from exc
    hello: dict = {"type": "hello", "v": 0, "client": _CLIENT}
    if binary_frames:
        hello["accept"] = {"binary_frames": True}
    if token:
        hello["token"] = token
    try:
        await ws.send(json.dumps(hello))
        # Bound the welcome read so a server that accepts the socket but never replies
        # can't hang aconnect() forever.
        welcome = json.loads(await asyncio.wait_for(ws.recv(), _HANDSHAKE_TIMEOUT))
        capabilities, platform = _parse_welcome(welcome)
    except Exception as exc:
        with contextlib.suppress(Exception):
            await ws.close()
        # Transport-shaped handshake failures classify as ConnectError too; a server
        # that REJECTED the handshake (bad token, version mismatch — _parse_welcome's
        # RuntimeError) keeps its actionable message and type.
        if isinstance(exc, asyncio.TimeoutError | TimeoutError):
            raise ConnectError(f"handshake with {addr} timed out: {exc}") from exc
        if isinstance(exc, _WsConnectionClosed | ConnectionError):
            raise ConnectError(f"connection lost during handshake with {addr}: {exc}") from exc
        raise
    sandbox = AsyncSandbox(
        ws,
        capabilities,
        platform,
        sandbox_capabilities=sandbox_capabilities,
        enforce_capabilities=enforce_capabilities,
        artifact_root=artifact_root,
        on_ask=on_ask,
        frame_cache=frame_cache,
        screenshot_dedup=screenshot_dedup,
    )
    sandbox._start_reader()  # the reader owns recv() from here on
    return sandbox


class _BackgroundLoop:
    """A dedicated asyncio loop on a daemon thread, powering the sync facade."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="shinken-loop", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro: Any, timeout: float | None = None) -> Any:
        # Use-after-close guard: scheduling onto a stopped loop would park the caller
        # on a future that can never resolve (the historical deadlock — any method on
        # a closed Sandbox blocked forever). Raise the typed SessionClosed instead.
        if self._closed:
            if asyncio.iscoroutine(coro):
                coro.close()  # suppress the 'coroutine was never awaited' warning
            raise SessionClosed(
                "the session loop is stopped (Sandbox.close()/SharedLoop.close() was "
                "called); open a new session via connect()"
            )
        # No outer ceiling by default: liveness comes from the inner per-RPC timeout
        # (#142) and connection-close fan-out (_fail_pending), so legitimately long
        # operations (multi-`wait` batches, docker-commit checkpoint/fork/resume) are
        # not severed mid-flight while the coroutine keeps running detached.
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self._closed = True

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
            self._sb._run(
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
            self._sb._run(
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
        frame = self._sb._run(self._sb._inner.next_frame(self._timeout), timeout=outer)
        if frame is None:
            raise StopIteration
        self._count += 1
        return frame

    def __exit__(self, *_exc: object) -> None:
        if self._started:
            with contextlib.suppress(Exception):
                self._sb._run(self._sb._inner.astop_screencast())


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
        # Whether the last structured observation came from the GUEST engine: its refs
        # are runtime-side ids, so element actions dispatch as element_ref targets
        # (resolved guest-side against the live tree) instead of local bbox math.
        self._elements_from_guest = False

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        """Run a session coroutine on the background loop, raising the typed
        :class:`~shinken.errors.SessionClosed` IMMEDIATELY when this Sandbox is closed
        — never scheduling onto a (possibly stopped) loop, which used to deadlock."""
        if self._closed:
            if asyncio.iscoroutine(coro):
                coro.close()  # suppress the 'coroutine was never awaited' warning
            raise SessionClosed(
                "Sandbox is closed (close() was already called); open a new session "
                "via connect()/provider.connect()"
            )
        return self._loop.run(coro, timeout)

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
        return self._run(self._inner.ping())

    def screen_size(self) -> dict:
        return self._run(self._inner.screen_size())

    def query(self, q: str) -> Any:
        """Ask the runtime for a property (e.g. ``platform``, ``screen_size``). Used by
        eval verifiers to read observed environment state."""
        return self._run(self._inner.query(q))

    @property
    def actions_dispatched(self) -> int:
        """Number of ACI actions successfully dispatched this session (eval step count)."""
        return self._inner.actions_dispatched

    def act(
        self, verb: str, target: dict | None = None, observe: Any = None, **kwargs: Any
    ) -> dict:
        return self._run(self._inner.act(verb, target, observe=observe, **kwargs))

    def act_batch(
        self, actions: list[dict], *, stop_on_error: bool = True, batch_id: str | None = None
    ) -> dict:
        """Execute an ordered batch of ACI actions serially (#73)."""
        return self._run(
            self._inner.act_batch(actions, stop_on_error=stop_on_error, batch_id=batch_id)
        )

    def step(
        self, actions: list[dict], *, observe: dict | None = None, step_id: str | None = None
    ) -> dict:
        """One agent step in **~ONE round-trip**: pipeline k actions (plus an optional
        trailing observation, fused into the same exchange) without awaiting between
        sends — the WAN step-latency lever (many-sandbox-concurrency.md §5)::

            res = env.step(
                [{"verb": "click", "target": pt}, {"verb": "type_text", "text": "hi"}],
                observe={"format": "jpeg", "quality": 80, "max_long_edge": 1024},
            )
            res["results"]      # per-action #56-taxonomy rows (no `skipped` — see below)
            res["observation"]  # the post-step frame, same shape as screenshot()

        See :meth:`AsyncSandbox.step` for the full contract — in particular the honest
        failure semantics: actions already on the wire after a failure execute
        server-side regardless, so each row reports its real outcome and the observation
        still returns after a mid-step per-action error."""
        return self._run(self._inner.step(actions, observe=observe, step_id=step_id))

    def click(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        """Click. With ``observe`` (act-returns-observation), the runtime follows the
        ack with a fresh observation in the same round trip and the OBSERVATION dict
        (the :meth:`screenshot` shape) is returned — ``env.click(x=1, y=2,
        observe=True)["bytes"]``. ``observe`` may also be a dict of the capture levers
        (``format``/``quality``/``max_long_edge``/``scope``). Same on every other
        mutating verb below."""
        return self._run(self._inner.click(target, x=x, y=y, observe=observe))

    def double_click(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return self._run(self._inner.double_click(target, x=x, y=y, observe=observe))

    def right_click(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return self._run(self._inner.right_click(target, x=x, y=y, observe=observe))

    def move(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        observe: Any = None,
    ):
        return self._run(self._inner.move(target, x=x, y=y, observe=observe))

    def drag(
        self,
        target: Any = None,
        to: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        to_x: float | None = None,
        to_y: float | None = None,
        duration_ms: int | None = None,
        button: str | None = None,
        observe: Any = None,
    ):
        """Drag from a source to a destination (pointer down → interpolated moves →
        up); see :meth:`AsyncSandbox.drag`."""
        return self._run(
            self._inner.drag(
                target,
                to,
                x=x,
                y=y,
                to_x=to_x,
                to_y=to_y,
                duration_ms=duration_ms,
                button=button,
                observe=observe,
            )
        )

    def mouse_down(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str | None = None,
        observe: Any = None,
    ):
        """Press (and hold) a pointer button — see :meth:`AsyncSandbox.mouse_down`."""
        return self._run(self._inner.mouse_down(target, x=x, y=y, button=button, observe=observe))

    def mouse_up(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        button: str | None = None,
        observe: Any = None,
    ):
        """Release a pointer button — see :meth:`AsyncSandbox.mouse_up`."""
        return self._run(self._inner.mouse_up(target, x=x, y=y, button=button, observe=observe))

    def scroll(
        self,
        target: Any = None,
        *,
        x: float | None = None,
        y: float | None = None,
        dy: float = 0.0,
        observe: Any = None,
    ):
        return self._run(self._inner.scroll(target, x=x, y=y, dy=dy, observe=observe))

    def list_windows(self) -> list[dict]:
        """Enumerate visible top-level windows (``{id, title, pid, x, y, w, h,
        focused}`` each; ``id`` is usable as the ``window:<id>`` capture scope) — see
        :meth:`AsyncSandbox.list_windows`."""
        return self._run(self._inner.list_windows())

    def exec(
        self,
        argv: list[str] | None = None,
        *,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ) -> dict:
        """Run one command inside the Sandbox over the ACI and return its typed
        result — ``env.exec(["ls", "-la"])`` → ``{'exit_code', 'stdout', 'stderr',
        'timed_out', …}``. See :meth:`AsyncSandbox.exec`: argv is the default
        (no-shell) form, ``shell=`` the explicit opt-in; a nonzero exit code is
        returned, not raised; timeouts kill the guest process group and report
        ``timed_out=True``."""
        return self._run(
            self._inner.exec(argv, shell=shell, cwd=cwd, env=env, timeout=timeout, stdin=stdin)
        )

    def exec_stream(
        self,
        argv: list[str] | None = None,
        *,
        shell: str | None = None,
        cwd: str | None = None,
        env: dict | None = None,
        timeout: float | None = None,
        stdin: str | None = None,
    ):
        """Streamed in-guest exec as a sync iterator of chunk dicts (``{'channel',
        'data', 'seq'}``; final item ``channel='exit'`` with the typed exit) — see
        :meth:`AsyncSandbox.exec_stream`."""
        agen = self._inner.exec_stream(
            argv, shell=shell, cwd=cwd, env=env, timeout=timeout, stdin=stdin
        )
        try:
            while True:
                try:
                    yield self._run(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            with contextlib.suppress(Exception):
                self._run(agen.aclose())

    def clipboard_get(self) -> str:
        """Read the guest clipboard as text — see :meth:`AsyncSandbox.clipboard_get`."""
        return self._run(self._inner.clipboard_get())

    def clipboard_set(self, text: str, *, observe: Any = None):
        """Set the guest clipboard text — see :meth:`AsyncSandbox.clipboard_set`."""
        return self._run(self._inner.clipboard_set(text, observe=observe))

    def launch_app(self, app: str, args: list[str] | None = None, *, observe: Any = None):
        """Start an app on the sandbox desktop — see :meth:`AsyncSandbox.launch_app`."""
        return self._run(self._inner.launch_app(app, args, observe=observe))

    def activate_window(
        self, window_id: int | None = None, *, app: str | None = None, observe: Any = None
    ):
        """Raise + focus a window by id or app/title — see
        :meth:`AsyncSandbox.activate_window`."""
        return self._run(self._inner.activate_window(window_id, app=app, observe=observe))

    def screenshot(
        self,
        scope: str = "screen",
        *,
        format: str | None = None,
        quality: int | None = None,
        max_long_edge: int | None = None,
        dedup: bool | None = None,
    ) -> dict:
        """Capture pixels on demand and return the screenshot dict.

        ``scope`` selects the capture region (``screen``, ``active_window``, or
        ``window:<id>``); ``format`` (``png`` default / ``jpeg``) + ``quality`` pick
        the wire codec; ``max_long_edge`` caps the frame's longer edge in pixels (the
        downscale bandwidth lever — same as on :meth:`screencast`); ``dedup`` turns on
        content-negotiated observation (see :meth:`AsyncSandbox.screenshot`).

        Returns ``{'bytes', 'format', 'w', 'h', 'wire_len', 'deduped', …}`` —
        **``bytes`` is the canonical key** (the raw encoded image; ``format`` says
        which codec). ``'png'`` is a **deprecated back-compat alias** of ``bytes``,
        present ONLY when the codec really is PNG, so legacy code blindly reading
        ``shot['png']`` fails loudly rather than mislabeling JPEG bytes as PNG.

        Raises :class:`ValueError` before sending if ``format`` is not among the
        runtime's advertised ``capabilities.image_formats``."""
        return self._run(
            self._inner.screenshot(
                scope,
                format=format,
                quality=quality,
                max_long_edge=max_long_edge,
                dedup=dedup,
            )
        )

    def observe(
        self,
        structured: bool = False,
        source: Any = None,
        *,
        diff: bool = False,
        settle_ms: int | None = None,
    ) -> dict:
        """Observe the desktop. ``structured=True`` captures the accessibility tree as
        ACI ``Element``s with stable refs; otherwise returns a screenshot observation.

        The structured path **prefers the guest engine**: when the runtime advertises
        ``capabilities.structured_observation``, the capture runs inside the Sandbox
        (`observe` verb) and the reply carries ``tree_text`` (the legible serialization),
        ``elements`` (stable ``e<N>`` refs minted runtime-side), ``revision``, ``focus``,
        ``node_count`` and ``capture_ms``; ``diff`` renders ``tree_text`` against the
        previous revision and ``settle_ms`` debounces a11y events before the walk.
        Without the capability (or with an explicit ``source``), the co-located /
        local-reference AT-SPI capture is used as the fallback, returning
        ``available=False`` gracefully when no tree is reachable."""
        if structured:
            if source is None and self._guest_observe_available():
                return self._observe_guest(diff=diff, settle_ms=settle_ms)
            return self._capture_structured(source)
        shot = self.screenshot()
        return {
            "type": "observation",
            "tree": "full",
            "image": {"w": shot["w"], "h": shot["h"], "scope": "screen"},
            "png": shot["png"],
        }

    def _guest_observe_available(self) -> bool:
        caps = self._inner.capabilities
        return bool(caps.structured_observation) or "observe" in (caps.verbs or [])

    def _observe_guest(self, *, diff: bool = False, settle_ms: int | None = None) -> dict:
        """Structured observation through the GUEST engine (the `observe` verb): the
        runtime settles, walks AT-SPI, mints stable ids, and renders ``tree_text``.
        Refreshes the ``ref → Element`` map; refs are resolved guest-side on actions."""
        kwargs: dict[str, Any] = {"structured": True}
        if diff:
            kwargs["diff"] = True
        if settle_ms is not None:
            kwargs["settle_ms"] = settle_ms
        obs = self.act("observe", **kwargs)
        self._elements = {e["ref"]: e for e in obs.get("elements", [])}
        self._elements_from_guest = True
        obs.setdefault("available", True)
        return obs

    def _capture_structured(self, source: Any = None) -> dict:
        """Gate the ``a11y`` capability (#145), capture a structured observation, and
        retain its ``ref → Element`` map (#78). Co-located fallback path."""
        from .a11y import AtspiSource, observe_structured

        self._inner.gate_capability("a11y", "observe_structured")
        obs = observe_structured(source or AtspiSource())
        self._elements = {e["ref"]: e for e in obs.get("elements", [])}
        self._elements_from_guest = False
        return obs

    def observe_diff(self, source: Any = None, *, settle_ms: int | None = None) -> dict:
        """Structured observation as a **diff** from the previous one (#4 / D3).

        Preferring the guest engine: returns the guest observation with
        ``tree="diff"`` and ``tree_text`` holding the ``~`` changed / ``+`` added /
        ``-`` removed lines against this session's previous revision (``diff_of``),
        with ``elements`` still the full live list (an explicit no-change notice when
        nothing moved; the full tree when the diff exceeds the runtime's line budget).

        On the local fallback (no guest engine, or an explicit ``source``): returns
        ``{added, removed, changed, unchanged, size, available}`` — only what changed
        since the last ``observe_diff`` / ``observe(structured=True)``, plus the
        serialized diff-vs-full byte sizes. The first call has no baseline, so every
        element is ``added``; ``available=False`` when the source is unreachable.
        Either way the ``element_ref`` map is refreshed, so ``resolve``/``act_on``
        keep working."""
        if source is None and self._guest_observe_available():
            return self._observe_guest(diff=True, settle_ms=settle_ms)
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
        """Semantic action routing (no raw coordinates from the caller).

        When the last structured observation came from the **guest engine**, the
        action ships an ``element_ref`` target and the runtime resolves it against
        the live tree (bbox centre, physical XTEST event) — a stale id answers a
        machine-readable ``stale_element_ref:`` error meaning re-observe. On the
        local fallback, the SDK resolves the point from the last observation and
        dispatches a typed pixel action."""
        if self._elements_from_guest:
            return self.act(verb, {"kind": "element_ref", "ref": ref}, **kwargs)
        point = self.resolve(ref)
        return self.act(verb, {"kind": "point_px", "x": point["x"], "y": point["y"]}, **kwargs)

    def invoke_action(self, ref: str, action: str | None = None) -> dict:
        """Invoke an element's accessibility action by name (the AX-path fallback for
        elements without usable geometry): ``action=None`` triggers the element's
        first advertised action (see ``Element.actions`` on the observation). Needs a
        runtime with ``capabilities.structured_observation``."""
        kwargs: dict[str, Any] = {"text": action} if action is not None else {}
        return self.act("invoke_action", {"kind": "element_ref", "ref": ref}, **kwargs)

    def set_value(self, ref: str, value: str) -> dict:
        """Set an element's value through its accessibility interface (numeric Value
        or EditableText), e.g. filling an entry without synthesizing keystrokes.
        Needs a runtime with ``capabilities.structured_observation``."""
        return self.act("set_value", {"kind": "element_ref", "ref": ref}, text=value)

    def type_text(self, text: str, *, observe: Any = None):
        return self._run(self._inner.type_text(text, observe=observe))

    def key(self, keys: str, *, observe: Any = None):
        return self._run(self._inner.key(keys, observe=observe))

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

    @property
    def handle(self) -> Any:
        """The provider :class:`~shinken.providers.base.SandboxHandle` managing this
        session (attached by ``provider.connect()``), or ``None`` on a bare
        ``shinken.connect()`` with no provider behind it."""
        return self._inner._handle

    def checkpoint(
        self, name: str | None = None, *, agent_state_ref: str | None = None
    ) -> Checkpoint:
        """Create a runtime checkpoint and return a first-class :class:`Checkpoint`.

        The returned :class:`Checkpoint` is a ``str`` subclass equal to the checkpoint
        id (today's str-consumers keep working verbatim) carrying the provider, the
        optional ``name``, and spec-compat info — with ``spawn()`` (restore + connect),
        ``spawn_many(n)`` (a :class:`SandboxFleet`), and ``delete()``.

        Requires a provider's ``connect()``; raises the typed
        :class:`~shinken.errors.ProviderRequired` otherwise."""
        return self._run(self._inner.checkpoint(name, agent_state_ref=agent_state_ref))

    def fork(self) -> Any:
        """Fork the current provider-managed sandbox and return the new provider
        HANDLE (not a session) — see :meth:`spawn` for the fork-and-connect one-liner."""
        return self._run(self._inner.fork())

    def spawn(self, **connect_kwargs: Any) -> Sandbox:
        """Fork this provider-managed sandbox and return a CONNECTED sibling
        :class:`Sandbox` — the one-liner for ``provider.connect(provider.fork(handle))``.
        Extra keyword arguments pass through to the provider's ``connect()`` (e.g.
        ``frame_cache=`` to dedup observations across the forked fleet). Raises the
        typed :class:`~shinken.errors.ProviderRequired` without a provider context and
        :class:`~shinken.errors.SessionClosed` after ``close()``."""
        provider = self._inner._provider
        if provider is None or self._inner._handle is None:
            raise ProviderRequired(
                "spawn() needs a provider-managed session; open it via a provider's "
                "connect()/session()"
            )
        return provider.connect(self.fork(), **connect_kwargs)

    def resume(self, handle_or_checkpoint: Any) -> Any:
        """**Deprecated alias of the provider's** ``restore`` **verb** — kept for
        back-compat. RESTORE semantics: materializes a NEW live sandbox from a
        snapshot/checkpoint id and returns its provider handle. It does **not**
        un-pause this session — calling it while the source sandbox is alive mints a
        SIBLING, not the same sandbox. Prefer ``Checkpoint.spawn()`` /
        ``provider.restore()``."""
        return self._run(self._inner.resume(handle_or_checkpoint))

    def destroy(self) -> None:
        """Tear the sandbox down for real: close the WS session, then ask the managing
        provider to destroy the substrate (``provider.destroy(handle)``). ``close()``
        alone only ends the session — the sandbox keeps running. Raises the typed
        :class:`~shinken.errors.ProviderRequired` when no provider context is attached
        (a bare ``shinken.connect()``), leaving the session untouched. Safe after
        ``close()`` (the close half is idempotent)."""
        provider, handle = self._inner._provider, self._inner._handle
        if provider is None or handle is None:
            raise ProviderRequired(
                "destroy() needs a provider-managed session; close() only ends the "
                "WS session — open the session via a provider's connect()/session()"
            )
        self.close()
        provider.destroy(handle)

    def act_model(self, adapter, tool_call: Any) -> dict:
        """One model tool-call, end to end, in ~one round trip: translate it through
        the adapter (``adapter.to_aci_action(tool_call)``; adapters exposing the plural
        ``to_aci_actions`` work too), execute via the pipelined :meth:`step` with a
        fused post-step observation, and render the observation back into the model's
        tool-result shape (``adapter.to_tool_result(observation)``)::

            result = env.act_model(AnthropicComputerUseAdapter(), tool_use["input"])

        Raises the adapter's :class:`~shinken.adapters.base.AdapterError` on an
        untranslatable call, and :class:`~shinken.errors.ShinkenError` when an action
        fails or the post-step observation is unavailable."""
        to_action = getattr(adapter, "to_aci_action", None)
        if to_action is not None:
            actions = [to_action(tool_call)]
        else:
            actions = list(adapter.to_aci_actions(tool_call))
        res = self.step(actions, observe={})
        bad = next((r for r in res["results"] if r is not None and not r["ok"]), None)
        if bad is not None:
            raise ShinkenError(f"act_model: action {bad.get('verb')!r} failed: {bad.get('error')}")
        observation = res.get("observation")
        if observation is None:
            raise ShinkenError(
                f"act_model: no post-step observation "
                f"({res.get('observation_error') or 'observation missing'})"
            )
        return adapter.to_tool_result(observation)

    def put_file(self, local_path: str, sandbox_path: str) -> dict:
        """Upload a host file into the sandbox; return its content-addressed ref (#85)."""
        return self._inner.put_file(local_path, sandbox_path)

    def get_file(
        self, sandbox_path: str, local_path: str, *, expect_sha256: str | None = None
    ) -> dict:
        """Download a sandbox file to the host, verifying its content hash (#85)."""
        return self._inner.get_file(sandbox_path, local_path, expect_sha256=expect_sha256)

    def close(self) -> None:
        """End the session and (when this Sandbox owns its loop) stop the background
        loop thread. Idempotent — a second close returns immediately. Any OTHER method
        called after close raises the typed :class:`~shinken.errors.SessionClosed`
        instead of deadlocking against the stopped loop. The substrate keeps running;
        :meth:`destroy` is the teardown that also reclaims it."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            # direct loop call (not _run): _closed is already set, but the loop is
            # still alive here — the inner session must really close before the stop.
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
    ping_jitter: float = 0.0,
    binary_frames: bool = True,
    frame_cache: FrameCache | None = None,
    screenshot_dedup: bool | None = None,
) -> Sandbox:
    """Connect to a running ``shinkend`` and complete the ACI handshake (blocking).

    ``binary_frames`` (default on) negotiates binary media framing — see
    :func:`aconnect`; the returned frames/screenshots keep the exact same dict shape.

    ``frame_cache`` / ``screenshot_dedup`` configure content-negotiated screenshots
    (``if_none_match`` dedup) — see :func:`aconnect` and :class:`FrameCache`; share
    one cache across the sessions of a forked fleet to dedup across replicas.

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
    surface: ``aconnect()`` + ``asyncio.gather`` on your own event loop.

    ``ping_jitter`` decorrelates keepalive phases across a fleet dialed together (see
    :func:`aconnect` and docs/engineering/many-sandbox-concurrency.md §3.2)."""
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
                ping_jitter=ping_jitter,
                binary_frames=binary_frames,
                frame_cache=frame_cache,
                screenshot_dedup=screenshot_dedup,
            )
        )
    except Exception:
        if owns_loop:
            bg.stop()  # never tear down a caller-owned shared loop on one failed dial
        raise
    return Sandbox(inner, bg, owns_loop=owns_loop)


# --- first-class checkpoint + fleet (API v2) -------------------------------------------


class Checkpoint(str):
    """A first-class checkpoint handle — **a** ``str`` **subclass equal to its id**, so
    every legacy str-consumer (``provider.resume(ckpt)``, dict keys, ``startswith``,
    JSON serialization, ``==`` against the raw id) keeps working verbatim, while the
    object also carries the managing ``provider``, the optional human ``name``, and
    spec-compat info (``spec`` — the originating
    :class:`~shinken.providers.base.SandboxSpec` when the provider recorded one, so a
    spawn boots at the golden geometry/limits, not provider defaults).

    The runtime-state one-liners::

        ckpt = env.checkpoint("golden")
        sibling = ckpt.spawn()                 # restore + connect, provider-managed
        with ckpt.spawn_many(8) as fleet:      # N replicas as a SandboxFleet
            fleet.map(lambda env: env.click(x=1, y=2))
        ckpt.delete()                          # reclaim the snapshot image
    """

    provider: Any
    name: str | None
    spec: Any

    def __new__(
        cls,
        id: str,
        *,
        provider: Any = None,
        name: str | None = None,
        spec: Any = None,
    ) -> Checkpoint:
        obj = super().__new__(cls, id)
        obj.provider = provider
        obj.name = name
        obj.spec = spec
        return obj

    @property
    def id(self) -> str:
        """The checkpoint id (== ``str(self)``)."""
        return str(self)

    def __repr__(self) -> str:  # the plain str repr would hide that this is typed
        return f"Checkpoint({str(self)!r}, name={self.name!r}, provider={_provider_name(self)!r})"

    def _require_provider(self) -> Any:
        if self.provider is None:
            raise ProviderRequired(
                "this Checkpoint has no managing provider attached; restore it through "
                "the provider that took it (provider.restore(checkpoint_id))"
            )
        return self.provider

    def spawn(self, **connect_kwargs: Any) -> Sandbox:
        """Materialize ONE new live sandbox from this checkpoint and connect to it
        (provider-managed): ``provider.connect(provider.restore(ckpt))``. RESTORE
        semantics — every spawn is a fresh sibling; spawning never mutates the
        checkpoint or any live session. Extra keyword arguments pass through to the
        provider's ``connect()``."""
        provider = self._require_provider()
        return provider.connect(provider.restore(str(self)), **connect_kwargs)

    def spawn_many(self, n: int, **connect_kwargs: Any) -> SandboxFleet:
        """Materialize ``n`` replicas from this checkpoint **concurrently** and return
        them as a :class:`SandboxFleet` (a context manager that destroys all of them on
        exit). The fleet's sessions share ONE :class:`SharedLoop` (a single client IO
        thread for the whole fleet) unless ``connect_kwargs`` carries an explicit
        ``loop=``. On a partial failure every already-spawned replica is destroyed
        before the error propagates — no half-fleet leaks."""
        if n < 1:
            raise ValueError("spawn_many needs n >= 1")
        provider = self._require_provider()
        loop: SharedLoop | None = None
        if "loop" not in connect_kwargs:
            loop = SharedLoop()
            connect_kwargs = {**connect_kwargs, "loop": loop}

        def _one(_i: int) -> Sandbox:
            return provider.connect(provider.restore(str(self)), **connect_kwargs)

        envs: list[Sandbox] = []
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="shinken-spawn") as pool:
            for fut in [pool.submit(_one, i) for i in range(n)]:
                try:
                    envs.append(fut.result())
                except BaseException as exc:  # noqa: BLE001 — collected, re-raised below
                    errors.append(exc)
        if errors:
            for env in envs:
                with contextlib.suppress(Exception):
                    env.destroy()
            if loop is not None:
                loop.close()
            raise errors[0]
        return SandboxFleet(envs, loop=loop)

    def delete(self) -> None:
        """Reclaim this checkpoint's snapshot image (``provider.delete_snapshot``).
        Idempotent and best-effort, like the provider call underneath."""
        provider = self._require_provider()
        delete = getattr(provider, "delete_snapshot", None)
        if delete is None:
            raise ProviderRequired(f"provider {_provider_name(self)!r} has no delete_snapshot()")
        delete(str(self))


def _provider_name(ckpt: Checkpoint) -> str | None:
    caps = getattr(ckpt.provider, "capabilities", None)
    return getattr(caps, "name", None) if caps is not None else None


class SandboxFleet:
    """A context manager OWNING ``n`` connected sandboxes (the shape
    :meth:`Checkpoint.spawn_many` returns) — destroy-all on exit, concurrent
    :meth:`map` over the members::

        with ckpt.spawn_many(8) as fleet:
            shots = fleet.map(lambda env: env.screenshot())

    ``map`` fans the callable out on a worker thread per member (the session IO
    itself stays multiplexed on the fleet's single :class:`SharedLoop`) — it is a
    real concurrent fan-out, NOT a serial comprehension. Exit destroys every member
    (session closed + substrate reclaimed via ``env.destroy()``) and stops the
    fleet-owned loop."""

    def __init__(self, envs: list[Sandbox], *, loop: SharedLoop | None = None) -> None:
        #: The member sandboxes, in spawn order.
        self.envs = list(envs)
        self._loop = loop  # fleet-owned SharedLoop (None when the caller supplied one)
        self._closed = False

    def __len__(self) -> int:
        return len(self.envs)

    def __iter__(self):
        return iter(self.envs)

    def map(self, fn: Callable[[Sandbox], Any]) -> list[Any]:
        """Run ``fn(env)`` over every member **concurrently** (one worker thread per
        member; session IO multiplexed on the shared loop) and return the results in
        member order. An exception from any member propagates after all workers
        finish their submission round."""
        if not self.envs:
            return []
        with ThreadPoolExecutor(
            max_workers=len(self.envs), thread_name_prefix="shinken-fleet"
        ) as pool:
            return list(pool.map(fn, self.envs))

    def close(self) -> None:
        """Destroy every member (close + provider destroy; falls back to plain close
        for members without a provider context) and stop the fleet-owned loop.
        Idempotent."""
        if self._closed:
            return
        self._closed = True
        for env in self.envs:
            try:
                env.destroy()
            except ProviderRequired:
                with contextlib.suppress(Exception):
                    env.close()
            except Exception:  # noqa: BLE001 — teardown is best-effort, never raises
                _log.debug("fleet member destroy failed", exc_info=True)
        if self._loop is not None:
            self._loop.close()

    def __enter__(self) -> SandboxFleet:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
