# Streaming bandwidth — first-party measurements (B-bucket)

The streaming/bandwidth advantage (D3/D4) was previously asserted from vendor-published
figures. This note records **first-party** measurements taken through the Shinken SDK against
a real GUI desktop on a remote sandbox substrate reached over an intercontinental WAN
(~0.28 s round-trip), so the claims behind the wedge are now measured, not cited.

Three questions: (1) how small can one observation get without losing the screen, (2) how
small can a continuous **lossless** stream get when only part of the screen changes, and
(3) what is the network/host envelope when one local process drives many sandboxes — the
"control plane in a trainer process pulling N sandboxes" case.

(The B2 numbers in §2 were measured on the local Docker sandbox rather than the WAN
substrate — bytes/frame is substrate-independent; the B1/B3 latency rows are not.)

## 1. Observation codec ladder (B1)

shinkend encodes each captured frame as PNG (default, lossless) or JPEG, chosen per-action
(`format`/`quality` on `screenshot`/`start_screencast`); the observation reports the codec it
used. One real 1920×1080 desktop frame, encoded server-side and pulled over the SDK:

| codec / scale        | bytes    | vs PNG | encode + transfer |
|----------------------|----------|--------|-------------------|
| PNG 1920×1080 (base) | 1804 KiB | 1.0×   | 0.85 s            |
| JPEG q80 full        | 87 KiB   | 20.7×  | 0.22 s            |
| JPEG q50 full        | 61 KiB   | 29.4×  | 0.22 s            |
| JPEG q80 @1280       | 48 KiB   | 37.4×  | 0.20 s            |
| JPEG q80 @768        | 24 KiB   | 73.2×  | 0.19 s            |

JPEG is both ~20× smaller **and** ~4× faster end-to-end than PNG (PNG's deflate dominates the
0.85 s). Downscaling to the model's real input resolution compounds it. Lossless PNG stays the
default; JPEG is the opt-in bandwidth lever for high-fps or many-sandbox use.

## 2. Dirty-tile delta screencast (B2)

`start_screencast` with `delta: true` keeps ONE previous RGB frame per stream (~6 MB at
1080p — the deliberate memory bound), splits each capture into 64px tiles, and pushes only
the changed tiles (`tiles: [{x, y, w, h, ref}]` on the observation, each tile encoded in the
stream's format/quality — PNG tiles use the fast compression preset so per-tile encode cost
stays below the full-frame path). A full keyframe goes out first, after a resume, and every
30th delivered frame (a constant: it bounds how long a dropped tile frame leaves a client
stale, and bounds lossy compositing drift for JPEG tiles). Unchanged frames are
idle-suppressed as before. Compositing is the consumer's job — the SDK passes tiles through
raw.

Measured on the local Docker sandbox (1280×800 Xvfb desktop, typing in an xterm at
~12 chars/s, fps=10, ~8 s window, 81 frames delivered per mode; decoded payload bytes —
base64+JSON framing adds ~33% on the wire):

| mode           | mean bytes/frame | vs full-PNG | composition                                |
|----------------|------------------|-------------|--------------------------------------------|
| full-PNG       | 28.1 KB          | 1.0×        | 81 full frames                             |
| delta-PNG      | 2.3 KB           | **12.1×**   | 3 keyframes (~30 KB) + 78 tile frames (mean 1.2 KB, ~2 tiles/frame) |
| delta-JPEG q80 | 3.0 KB           | 9.5×        | 3 keyframes (~34 KB) + 78 tile frames (mean 1.8 KB) |

**Reads:**
- **The lossless lever works: ~12× under live typing with zero quality loss.** A keystroke
  dirties ~2 tiles (~1 KB); the periodic keyframe dominates the residual mean.
- **For text/terminal content, delta-PNG beats delta-JPEG** — flat-background glyphs
  compress better losslessly than as DCT blocks, and the JPEG keyframe is larger too. JPEG
  (B1) stays the lever for photographic/full-frame content; delta-PNG is the default lever
  for UI work, and the two compound only when tiles are photographic.
- This compounds with B1's downscale: `max_long_edge` applies BEFORE tiling, so tiles align
  with the delivered resolution.

## 3. Fan-out envelope (B3)

One local process drove N sandboxes concurrently, each: inject shinkend → local `ws→wss`
proxy → SDK connect; then synchronized rounds of {observe JPEG q80 @1280 (~48 KiB) + click}.

| N  | round wall-clock | observe p95 | KiB/sandbox | process RSS | marginal RAM/sandbox |
|----|------------------|-------------|-------------|-------------|----------------------|
| 4  | 0.43 s           | 0.29 s      | 48.2        | 116 MB      | —                    |
| 8  | 0.42 s           | 0.30 s      | 48.2        | 151 MB      | 8.8 MB               |
| 16 | 0.51 s           | 0.38 s      | 48.2        | 208 MB      | 7.1 MB               |

**Reads:**
- **Bandwidth is feasible because of B1.** At 48 KiB/sandbox/round, 1024 sandboxes at 1 Hz ≈
  **~405 Mbps** — a datacenter NIC, not a heroic number. With full-res PNG (1804 KiB/frame) it
  would be ~15 Gbps, infeasible anywhere. JPEG is load-bearing for scale-out, not a nicety.
- **Per-step latency is WAN-bound** (~0.28–0.38 s round-trip) and begins creeping at N=16 as the
  thread pool contends — observation throughput per worker is dominated by RTT, so batching
  (`act_batch`) and observe/act coalescing matter at WAN distance.
- **The local model is the ceiling, not the network.** Each `Sandbox` currently owns its own
  background event-loop thread (`_BackgroundLoop`), and this harness adds a second loop per
  sandbox for the proxy. Marginal RSS is ~7 MB/sandbox; extrapolated, **N=1024 ≈ ~7 GB RAM and
  ~2000 threads** in one process — the thread-per-connection model breaks long before bandwidth
  does.

## Recommendation

1. **Default observation to JPEG (q80, downscaled to model input res) for eval/RL fleets;** keep
   PNG for pixel-exact needs. (B1 — shipped.)
2. **Many sandboxes from one process — the async core IS the native fleet surface;
   `SharedLoop` for sync callers.** The thread-per-connection cost is an artifact of the sync
   facade, not the architecture:

   ```python
   sandboxes = await asyncio.gather(*(shinken.aconnect(a, token=t) for a, t in endpoints))
   shots = await asyncio.gather(*(sb.screenshot(format="jpeg") for sb in sandboxes))
   ```

   — N connections on the caller's own loop, zero extra threads, with fan-out/batching/failure
   policy where they belong: in the caller or the eval/train consumer layers (server-side fleet
   management is the Control Plane's Fleet Manager, D9 — deliberately NOT an SDK class). For
   sync callers (e.g. a trainer's step loop), `shinken.SharedLoop` multiplexes N
   `connect(..., loop=shared)` facades onto ONE background thread instead of one each —
   measured at N=16: 16 loop threads → **1**. It is a resource handle only; it adds no
   orchestration surface. (The remaining per-connection cost on a WAN substrate is the local
   ws→wss proxy, which the same single-loop pattern can absorb.)
3. **For continuous lossless streaming, use the dirty-tile delta** (B2 — shipped:
   `screencast(delta=True)`): only changed tiles + periodic keyframes travel, measured ~12×
   under live typing with PNG (lossless). Prefer delta-PNG over delta-JPEG for UI/text
   content (see §2).
4. **Batch at WAN distance:** prefer `act_batch` and observe/act coalescing to amortize RTT.

Reproduce with the local dev-test harness (not committed): the bandwidth probe and the
fan-out probe under `devtest/`.

The full many-sandbox concurrency design (memory governance, lifecycle at scale, pull-vs-push
scheduling, RTT amortization, N=64/256/1024 gates) builds on these numbers:
[`many-sandbox-concurrency.md`](many-sandbox-concurrency.md).

The dense, scripted LOCAL counterpart of B1/B2/B3 — full codec sweep over three content
scenarios (including a procedurally generated photographic frame that reproduces this
section's ~20× headline locally: 19.3× at q80), per-frame delta traces, latency CDFs,
checkpoint/fork/resume timings, and a SharedLoop fan-out envelope, with tracked raw
datapoints and rerunnable suites under `benchmarks/` — is [`benchmarks.md`](benchmarks.md).
