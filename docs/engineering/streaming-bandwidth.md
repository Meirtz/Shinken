# Streaming bandwidth — first-party measurements (B-bucket)

The streaming/bandwidth advantage (D3/D4) was previously asserted from vendor-published
figures. This note records **first-party** measurements taken through the Shinken SDK against
a real GUI desktop on a remote sandbox substrate reached over an intercontinental WAN
(~0.28 s round-trip), so the claims behind the wedge are now measured, not cited.

Two questions: (1) how small can one observation get without losing the screen, and (2) what
is the network/host envelope when one local process drives many sandboxes — the
"control plane in a trainer process pulling N sandboxes" case.

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

## 2. Fan-out envelope (B3)

One local process drove N sandboxes concurrently, each: inject shinkend → local `ws→wss`
proxy → SDK connect; then synchronized rounds of {observe JPEG q80 @1280 (~48 KiB) + click}.

| N  | round wall-clock | observe p95 | KiB/sandbox | process RSS | marginal RAM/sandbox |
|----|------------------|-------------|-------------|-------------|----------------------|
| 4  | 0.43 s           | 0.29 s      | 48.2        | 116 MB      | —                    |
| 8  | 0.42 s           | 0.30 s      | 48.2        | 151 MB      | 8.8 MB               |
| 16 | 0.51 s           | 0.38 s      | 48.2        | 208 MB      | 7.1 MB               |

**Reads:**
- **Bandwidth is feasible because of B1.** At 48 KiB/sandbox/round, 1024 sandboxes at 1 Hz ≈
  **~405 Mbps** — a datacenter NIC, not a heroic number. With PNG it would be ~8.4 Gbps,
  infeasible anywhere. JPEG is load-bearing for scale-out, not a nicety.
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
3. **Reduce per-step bytes further with a dirty-tile diff** (B2, planned): send only changed
   tiles + periodic keyframes, since a single click/keystroke changes a small screen region.
4. **Batch at WAN distance:** prefer `act_batch` and observe/act coalescing to amortize RTT.

Reproduce with the local dev-test harness (not committed): the bandwidth probe and the
fan-out probe under `devtest/`.
