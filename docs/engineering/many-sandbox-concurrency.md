# Many-sandbox concurrency — one local process, up to ~1024 sandboxes

**Status:** design (this doc) + two small shipped client primitives (§2.3, §3.2). Everything
else here is **designed-only** — see the Built-vs-designed table in §8 and
[`status.md`](status.md) for the authoritative map.

This doc designs the **client-side concurrency envelope**: one local process (an RL trainer,
an eval harness, a task factory) driving up to ~1024 sandboxes on a **remote sandbox substrate
over WAN**. It builds directly on the first-party measurements in
[`streaming-bandwidth.md`](streaming-bandwidth.md) (JPEG 20–73× vs PNG; the N=4/8/16 fan-out
envelope; the ~0.28–0.38 s WAN round-trip; the thread-per-connection ceiling and its
`SharedLoop` resolution) and respects that doc's design philosophy:

> **The async core IS the native fleet surface.** Fan-out, batching, and failure policy belong
> to the caller or the eval/train consumer layers; server-side fleet management is the Control
> Plane's Fleet Manager ([D9](../design/tech-decisions.md#d9--control-plane-fleet-manager--action-gateway))
> — deliberately NOT an SDK class.

So this doc proposes **no orchestration classes**. It governs the resources one process must
manage when `asyncio.gather` over N `AsyncSandbox` sessions is the API: memory, connections,
file descriptors, observation scheduling, RTT amortization, and failure isolation.

## 1. The model at N — what the measurements already say

From [`streaming-bandwidth.md`](streaming-bandwidth.md):

| fact (measured) | value | consequence at N=1024 |
|---|---|---|
| JPEG q80 @1280 frame | 48 KiB | 1 Hz pull ≈ **~405 Mbps** aggregate — feasible on a datacenter NIC |
| JPEG q80 full 1080p frame | 87 KiB (~100 KB class) | the "worst-case queued frame" used for memory math below |
| PNG 1080p frame | 1804 KiB | any N-scale PNG plan is dead on arrival (~15 Gbps at 1024 × 1 Hz) |
| WAN round-trip | ~0.28–0.38 s | per-step latency is RTT-bound, not bandwidth-bound (§5) |
| thread-per-conn marginal RSS | ~7 MB + 2 threads/sandbox | N=1024 ≈ ~7 GB + ~2000 threads — the sync facade does not scale; the async core / `SharedLoop` does |

The two scaling walls are therefore **client memory** (queued frames, §2) and **connection
lifecycle behavior** (§3) — not network bandwidth, provided JPEG (B1) is the fleet codec.

## 2. Memory governance: a global frame-queue byte budget

### 2.1 The problem: per-connection bounds are N-unbounded in aggregate

Each `AsyncSandbox` bounds its inbound frame queue to `_FRAME_QUEUE_MAX = 32` frames with
drop-oldest semantics (`client.py:_push_frame`). Right for one sandbox; in aggregate the bound
scales linearly with N and with **frame size, which the client does not control** (the server
picks the encoded size):

| codec | per-conn worst case (32 frames) | × 1024 conns |
|---|---|---|
| JPEG q80 @1280 (48 KiB) | 1.5 MiB | **~1.5 GiB** |
| JPEG q80 full (~100 KB) | ~3.2 MB | **~3.2 GB** |
| PNG 1080p (1804 KiB) | ~56 MiB | **~59 GB** — and queued frames hold **base64** (×4/3 resident), so closer to ~75 GB |

A fleet that stalls consuming (a slow policy step, a GC pause, one straggler round) fills
queues everywhere at once. A count bound cannot govern this; only a **byte** budget shared
across connections can.

### 2.2 Design: per-event-loop byte budget, drop-oldest-anywhere

- **Scope: per event loop.** All `AsyncSandbox` sessions on one loop share one budget. The
  loop is the deployment unit — one fleet = one loop (the async core directly, or one
  `SharedLoop` for sync callers) — so "per loop" *is* "per fleet", and the single-threaded
  loop guards the shared accounting **without locks on the hot path**: a shared counter +
  deque mutated only from that loop's thread. Two loops = two independent budgets.
- **Accounting:** every queued frame is accounted at its resident size (the base64 payload
  length — what actually sits in memory). Consuming or dropping a frame releases its bytes.
  Stream sentinels (`_STREAM_END` / `_STREAM_LOST`) are never accounted and never evicted by
  the budget — a dead sandbox's loss signal must survive (§6).
- **Eviction: approximately-oldest-anywhere.** A global FIFO of push order spans all
  connections on the loop; when a push exceeds the budget, the globally-oldest queued frames
  are evicted — *whoever's queue they sit in* — until the budget is satisfied. Stale-entry
  cleanup is lazy + amortized (head-drain on push, periodic compaction), so the hot path stays
  O(1) amortized.
- **Default: off (`None`)** — current per-connection behavior, single-sandbox callers pay
  nothing. **Recommended fleet setting: 256 MiB** (`GLOBAL_FRAME_BUDGET_BYTES = 256 << 20`):
  - 256 MiB / 48 KiB ≈ **~5,400 buffered frames** ≈ 5 frames/sandbox headroom at N=1024 —
    plenty for pull-based training (§4), where steady-state queue depth is ~0–1;
  - it only bites below N ≈ 170 × full 32-deep JPEG queues — smaller fleets are unaffected;
  - it is a small, predictable slice of the process budget (vs the unbounded ~3.2 GB/~59 GB above).
  - Floor: set the budget ≥ a few × the largest expected frame; a budget smaller than one
    frame evicts the frame just pushed (a hard budget admits nothing oversized).

**Alternative considered — per-conn quota derated by N** (e.g. ⌈32/N⌉ frames or bytes/N per
conn): simpler, but idle connections strand quota while hot ones starve, and N changes as
sandboxes come and go. The global FIFO matches the actual intent — *drop the stalest pixels in
the process, wherever they are* — and costs the same O(1) amortized.

**Known edges (accepted, documented):** (1) the budget governs *queued* frames, not frames in
flight inside the websockets library buffer (bounded separately by `_MAX_WS_MESSAGE` = 16 MiB
per message and the per-message read model); (2) enabling the knob mid-session governs frames
pushed from then on — frames already queued drain on consumption; set the budget before
connecting the fleet; (3) in the rare race where a clean-stop sentinel precedes late in-flight
frames in one queue, eviction can consume the sentinel — the consumer then ends via its
`next_frame` timeout instead of promptly (the pre-existing count bound has the same property).

### 2.3 Shipped primitive

`shinken.client.GLOBAL_FRAME_BUDGET_BYTES: int | None = None` — the module-level knob, plus
the per-loop accounting/eviction inside `_push_frame`/`next_frame`/`_clear_frames`. No new
classes, no locks, no API surface beyond the knob. Tests:
`sdk/python/tests/test_frame_budget.py`.

## 3. Connection lifecycle at scale

### 3.1 Mass connect: thundering herd → jittered, bounded dialing

1024 simultaneous dials are a SYN + TLS-handshake burst (TLS 1.3 ≈ +1 RTT ≈ +0.3 s each at
WAN distance, plus handshake CPU). Two caller-side levers, both plain `asyncio` (no
orchestration class):

```python
sem = asyncio.Semaphore(64)                      # ≤64 dials in flight
async def dial(addr, token):
    await asyncio.sleep(random.uniform(0, 0.25)) # decorrelate the burst edge
    async with sem:
        return await shinken.aconnect(addr, token=token, ping_jitter=10.0)

sandboxes = await asyncio.gather(*(dial(a, t) for a, t in endpoints))
```

Bounded concurrency (16–64) spreads handshake CPU and SYN bursts; the per-dial jitter
decorrelates retry edges. Expected connect wall-clock for N=1024 at 64-way concurrency and
~0.6 s/dial (TCP+TLS+ACI handshake at 0.3 s RTT): **~10 s** — acceptable as a one-time cost.

### 3.2 Keepalive phase alignment: jittered ping_interval (shipped primitive)

The websockets library pings every `ping_interval` (default 20 s) **from the moment of
connect** — so a fleet dialed together pings in the *same tick*, forever: N=1024 means
1024 ping+pong packets every 20 s in one burst, synchronized with nothing else the loop is
doing. The fix is a per-connection jitter applied **once, at dial time**:

`aconnect(..., ping_jitter=10.0)` → `ping_interval = 20.0 + uniform(0, ping_jitter)`.

Distinct periods drift the phases apart permanently (stronger than a one-time phase offset).
Default `0.0` = the library default, unchanged. Recommended `ping_jitter ≈ 5–10 s` for
N ≥ 256. Tests: `sdk/python/tests/test_ping_jitter.py`.

### 3.3 Reconnect storms: full-jitter exponential backoff + resume

A substrate or path blip drops many connections at once; naive immediate reconnection is a
thundering herd squared (every survivor of attempt k retries in phase at attempt k+1).
Recommended caller policy (again: a loop, not a class):

- **full-jitter exponential backoff**: `delay = uniform(0, min(30.0, 0.5 * 2**attempt))`;
- classify first: `errors.is_connection_loss(exc)` / `SandboxDied` decides reconnect-vs-
  replace (§6) — never blind-retry a sandbox the provider says is dead;
- **stream continuity is already in the protocol (#56)**: reconnect with a fresh session and
  `resume_screencast(stream_id, <original params>)` — same logical stream id, `seq` continues;
  the seq gap counts the frames missed during the outage. No new protocol work is needed for
  storm recovery; this design only adds the *pacing*.

### 3.4 File descriptors and ulimits

Steady state is 1 socket per sandbox — but only if TLS is terminated **on the same loop**. The
fan-out measurement harness interposed a local `ws→wss` proxy per connection (a second event
loop + ~3 fds each); folding TLS into the SDK's own dial (`wss://` straight from
`aconnect`, which websockets supports natively) makes it 1 fd + 0 extra threads per sandbox.

Budget: N=1024 sockets + provider handles + logs + stdio ≈ plan **nofile ≥ 4×N (8192)**.
Defaults are too low: Linux soft default 1024 (exactly the fleet size — zero headroom),
macOS 256. The N=256 validation gate (§7) records actual fd high-water marks.

## 4. Observation scheduling for RL: pull, not push

Two observation modes exist today; they scale very differently:

- **Pull (`screenshot()` on demand) — recommended for training loops.** The policy requests a
  frame when it is *ready to consume one*. Backpressure is structural: outstanding pixels ≤
  N × one frame; queues sit at depth ~0–1; the §2 budget never engages. At N=1024 × 1 Hz ×
  48 KiB ≈ ~405 Mbps — feasible. A stalled learner stalls its *requests*, not a server that
  keeps pushing.
- **Push (`screencast`) — for human viewing** (Operator, Control Panel, debugging a handful
  of live sandboxes). Server-push with idle-suppression + downscale is built and right for
  low-N, latency-sensitive *watching*. At N=1024 × 5 fps it is ~5,120 frames/s through one
  loop — ~2 Gbps plus JSON-parse + base64 work per frame (~0.2–0.5 ms/frame, *estimate,
  unmeasured*) ≈ 1–2.5 cores — saturating the single-loop model. Push does not scale to the
  full fleet and does not need to.

**Per-sandbox rate limits** stay server-shaped and per-session: `fps` cap, `max_long_edge`
downscale, JPEG `quality` — all already in the ACI. The consumer paces pulls (its step loop is
the rate limiter); the §2 byte budget is the backstop when a push consumer stalls, not a
scheduler.

## 5. RTT amortization at WAN distance

The measured ~0.28–0.38 s round-trip dominates per-step latency; fan-out hides it across
sandboxes (N rounds proceed concurrently) but **within** one sandbox's step, serial RPCs
multiply it:

- **`act_batch` today is SDK-serial** — each action awaits its ack, so k actions ≈ k RTTs
  (a 5-action step ≈ 1.5–1.9 s).
- **Designed — pipelined dispatch:** the reader/demux already correlates replies by
  `call_id`, and shinkend processes a connection's messages in order, so the SDK can send k
  calls *then* await k futures: ~1 RTT for the batch (~0.3–0.4 s for the same 5-action step,
  ~5×). Failure semantics must be settled first (what "skipped" means when later actions are
  already on the wire) — this is the designed successor to today's serial `act_batch`, not a
  trivial change, so it ships as design only.
- **Designed — observe+act coalescing:** a step is act → fresh frame; today that is two
  round-trips. `screenshot` is already an action verb, so a batch whose final action is
  `screenshot` returns the post-action observation in the same exchange once batches are
  pipelined — one RTT per *step*, the natural unit for RL rollouts.

## 6. Failure isolation: no shared fate

One dead sandbox must never block a round, and nothing here introduces cross-sandbox coupling:

- **Typed per-sandbox errors (built, #56):** `SandboxDied` (with exit/signal detail),
  `classify_exception` → `ok | error | timeout | skipped | sandbox_died`,
  `is_connection_loss` covering the websockets `ConnectionClosed` family. The consumer
  branches infrastructure-death vs task-failure without string matching.
- **The round idiom** is `asyncio.gather(*(step(sb) for sb in sandboxes),
  return_exceptions=True)` — a raising sandbox yields its exception *as a result*; the round
  completes. Liveness per sandbox is already bounded: every RPC carries the 30 s
  `_rpc_timeout`, and connection death fails all of that session's pending futures at once
  (`_fail_pending`). Tighter rounds wrap the gather in `asyncio.wait_for` — caller policy.
- **Health probing:** after classifying a loss, confirm with `provider.check_alive(handle)` —
  the provider owns the substrate lifecycle and upgrades a coarse `sandbox_died` with exit
  code/OOM-signal detail (no-op for providers that cannot introspect). Probe **on the failure
  path**, not as a fleet-wide poll from the hot loop.
- **No shared fate via the §2 budget:** a dead sandbox's queued frames are released on
  close/clear, its `_STREAM_LOST` sentinel is never evicted, and eviction never blocks —
  one stalled or dead connection can shed *its* bytes but cannot wedge another's.

## 7. Staged validation plan

Each gate runs the pull-based round loop (observe JPEG q80 @1280 + click) from
[`streaming-bandwidth.md`](streaming-bandwidth.md) §2 on the async core (one loop), against
the remote sandbox substrate over WAN. **Metrics recorded at every gate:** round wall-clock
(p50/p95), per-observe latency (p50/p95/p99), event-loop scheduling lag (p99), process RSS +
marginal RSS/sandbox, fd high-water mark, frames dropped (count bound) vs evicted (byte
budget), aggregate NIC Mbps, reconnect-convergence time after an induced drop.

| gate | proves | specific additions | pass criteria |
|---|---|---|---|
| **N=64** | single-loop fan-out beyond the measured N=16 | marginal RSS/sandbox on one loop (vs ~7 MB thread-per-conn) | round wall-clock ≲ 2× RTT; marginal RSS ≤ ~1 MB/sandbox; zero budget evictions under pull |
| **N=256** | lifecycle behavior | ping-jitter on/off keepalive-burst comparison; reconnect-storm drill (kill 25% of conns, full-jitter backoff + `resume_screencast`); fd high-water | keepalive bursts ≤ ~N/10 packets/tick with jitter on; storm convergence < 60 s with zero impact on surviving sandboxes' round times |
| **N=1024** | the headline envelope | sustained 1 Hz rounds (~405 Mbps); stall drill: stop consuming 10% of sandboxes with the 256 MiB budget on | RSS within budgeted envelope (loop model, not ~7 GB threads); budget holds `used ≤ budget` with evictions confined to stalled conns; non-stalled round times unaffected; loop-lag p99 < 50 ms |

Gates are sequential; a failed gate stops the scale-up and feeds back into this design. The
harness stays a local dev-test probe (like the bandwidth/fan-out probes — not committed);
the *numbers* land back in this doc and [`streaming-bandwidth.md`](streaming-bandwidth.md).

## 8. Non-goals and built-vs-designed

**Non-goals (deliberate):**

- **No SDK orchestration classes.** No `FleetClient`, no `SandboxPool`, no `RetryPolicy`
  objects. `asyncio.gather` over the async core is the fleet API; `SharedLoop` remains a
  resource handle only. Fan-out, batching, retry, and scheduling policy live in the caller or
  the eval/train consumer layers, where they are visible and replaceable.
- **Server-side fleet management stays in the Control Plane** (D9): warm pools,
  fork-on-demand, dual-timer sessions, auto-suspend, admission control, tenant rate limits.
  This doc governs one client process's envelope and nothing across processes or tenants.

**Built vs designed in this doc:**

| item | status |
|---|---|
| global frame-queue byte budget knob (§2.3) | **built** (default off) |
| `ping_jitter` at dial (§3.2) | **built** (default off) |
| jittered/bounded dial pacing, full-jitter reconnect backoff (§3.1, §3.3) | designed — caller-side idiom, snippets above |
| pipelined `act_batch`, observe+act coalescing (§5) | designed |
| pull-vs-push scheduling guidance (§4) | design guidance over built verbs |
| N=64/256/1024 gates (§7) | planned, not run |

## References

- [`streaming-bandwidth.md`](streaming-bandwidth.md) — the measurements this design derives from
- [D4 — streaming](../design/tech-decisions.md#d4--streaming-single-peerconnection-webrtc-dual-transport),
  [D9 — control plane](../design/tech-decisions.md#d9--control-plane-fleet-manager--action-gateway)
- `sdk/python/src/shinken/client.py` (`AsyncSandbox`, `_push_frame`, `SharedLoop`),
  `sdk/python/src/shinken/errors.py` (#56 taxonomy)
- websockets keepalive (`ping_interval`): <https://websockets.readthedocs.io/en/stable/topics/keepalive.html>
- full-jitter exponential backoff: <https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/>
