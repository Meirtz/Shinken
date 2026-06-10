# Shinken benchmarks — first-party measurements

Every number in this report is first-party (this repo's runtime + SDK). Each table is labeled
with one of three **evidence classes**, so a reader always knows what kind of claim they are
looking at:

- **[local — rerunnable]** measured by the tracked suites in [`benchmarks/`](../../benchmarks)
  on a local Docker sandbox; raw datapoints in `benchmarks/results/*.json`; rerun with
  `make benchmarks`. Methodology + caveats: [engineering/benchmarks.md](../engineering/benchmarks.md).
- **[remote WAN — one-off]** measured once against a generic remote Linux sandbox over an
  intercontinental WAN (~0.28 s RTT; substrate not identified — vendor-neutral). The raw data is
  tracked (`benchmarks/results/remote/*.csv`) but the driving harness is not published, so these
  tables are auditable, not rerunnable. Single-shot: **n=1 per cell**, no variance available.
- **[projection]** arithmetic from measured inputs, never presented as a measurement.

Figures live in [`docs/assets/bench/`](../assets/bench) and regenerate from tracked data only:
`python benchmarks/replot.py && python benchmarks/plot_remote.py` (no Docker needed).

| artifact | class | what it holds |
|---|---|---|
| `benchmarks/results/*.json` | local | 6 suites, ~93k individual datapoints, each JSON carries the host/image fingerprint of its run |
| `benchmarks/results/remote/codec_ladder.csv` | remote WAN | 45 cells: format × quality × downscale, one content-rich 1080p frame |
| `benchmarks/results/remote/fanout_remote.csv` | remote WAN | end-to-end fan-out envelope, N = 4/8/16 real remote sandboxes |
| `spikes/a11y-coverage/evidence.json` | local spike | accessibility-tree coverage by app surface (E5) |

---

## 1. Observation bandwidth

### 1a. Codec ladder, local — the honest lower bound `[local — rerunnable]`

`format` (PNG/JPEG) × quality {10…95} × `max_long_edge` {1280, 1024, 768, 512} × 5 reps ×
two content scenarios = 440 datapoints (S1, `bench_codec_ladder.py`). Condensed (mean KiB):

| scenario | PNG @1280 | JPEG q80 | JPEG q50 | JPEG q50 @512 |
|---|---:|---:|---:|---:|
| dense text (~95% coverage) | 747 | 553 (1.4×) | 339 (2.2×) | 61 (**12.3×**) |
| sparse desktop (~15% coverage) | 65 | 86 (**PNG wins**) | 64 (1.0×) | 11 (5.9×) |

![codec ladder](../assets/bench/codec_ladder.png)

The codec is a **content-dependent lever, not a constant multiplier**: on flat/sparse UI, PNG's
lossless run-length encoding already wins; on dense text JPEG only pays below ~q50; JPEG q95 on
text is *larger* than lossless PNG. This is why PNG is the default and JPEG/downscale are
explicit per-action knobs. Downscale is the strongest single lever on every content type.

### 1b. Codec ladder, remote — the content-rich upper bound `[remote WAN — one-off]`

One live 1920×1080 content-rich browser-desktop frame, 45 single-shot cells
(`results/remote/codec_ladder.csv` — the table below is generated from that CSV):

| codec | KiB | × vs PNG | encode+transfer (s) |
|---|---:|---:|---:|
| PNG (lossless) | 1804.5 | 1.0× | 0.89 |
| JPEG q95 | 276.7 | 6.5× | 0.27 |
| JPEG q80 | 87.3 | **20.7×** | 0.24 |
| JPEG q50 | 61.3 | 29.4× | 0.23 |

JPEG q80, by downscale (longer edge): 1280 → **48.3 KiB (37.4×)**, 960 → 33.3 (54.2×),
768 → 24.7 (**73.1×**), 512 → **13.8 (130.8×)**.

![remote codec ladder](../assets/bench/remote_codec_ladder.png)

On this frame JPEG q80 is both ~21× smaller and ~3.8× faster end-to-end than PNG (PNG's deflate
dominates its 0.89 s). Taken with §1a: **the JPEG lever spans ~1–21× by content (PNG can win),
and stacking downscale-to-model-input-resolution reaches ~131× vs full-res PNG on content-rich
frames.** Single-shot caveat applies to every cell.

![bandwidth bars](../assets/bench/bandwidth_bars.png)

### 1c. Lossless dirty-tile delta — the robust stream win `[local — rerunnable]`

For a *stream*, `shinkend` sends only changed 64-px tiles + periodic keyframes (B2). Typing into
a terminal at ~12.5 chars/s, fps=10, 80 frames/mode, 324 frame-level datapoints (S2):

| mode (typing) | mean KiB/frame | p50 KiB/frame | vs full-PNG | lossless |
|---|---:|---:|---:|---|
| full-PNG | 27.4 | 27.4 | 1.0× | ✓ |
| **delta-PNG** | **2.43** | **1.07** | **11.3×** | ✓ |
| delta-JPEG q80 | 3.55 | 1.87 | 7.7× | ✗ |

Lossless delta-PNG **beats lossy JPEG** on text/UI content, and an idle window delivers exactly
one keyframe then zero bytes — parked sandboxes cost ~nothing to watch.

![delta screencast](../assets/bench/delta_screencast.png)

---

## 2. Concurrency — the measured ladder to 1024

The "manage 1024 sandboxes" question splits into a guest-resource half (how many sandboxes a
host can *run*) and a client-plane half (how many live sessions one process can *drive*). Each
rung below is measured; only the egress chart at the end is a projection, and is labeled as one.

### 2a. Real sandboxes, one process — N ≤ 64 `[local — rerunnable]`

S5 (`bench_fanout.py`): N ∈ {1…64} real Docker sandboxes, all sync sessions multiplexed on one
`SharedLoop`, 10 rounds of {observe JPEG q80 @1024 + click} per tier — 1,347 datapoints:

| N | observe round wall p50 | per-sandbox observe p50 | click round wall p50 | guest RSS / sandbox |
|---:|---:|---:|---:|---:|
| 1 | 7.5 ms | 7.2 ms | 1.1 ms | —¹ |
| 8 | 7.9 ms | 7.3 ms | 1.9 ms | 40.2 MiB |
| 16 | 14.0 ms | 10.4 ms | 2.2 ms | 41.4 MiB |
| 32 | 24.6 ms | 20.9 ms | 4.7 ms | 39.3 MiB |
| 64 | **50.7 ms** | 42.6 ms | 5.5 ms | 36.2 MiB |

¹ the N=1 guest-RSS reading (157 MiB) is a first-boot settling artifact; steady state is ~36–41 MiB.

One process drives **64 real desktops at ~1,260 observations/s aggregate on 2 OS threads**
(main + one `SharedLoop`); latency degrades gracefully as 64 Xvfb guests contend for 14 cores.
At ~40 MiB/sandbox the binding constraint above N≈64 on this host is guest RAM in the Docker
VM — exactly the boundary the client-plane rung isolates next.

![local fan-out](../assets/bench/local_fanout.png)

### 2b. Client plane to N=1024, realistic payloads `[local — rerunnable]`

S6 (`bench_client_scale.py`): one client process, N ∈ {16, 64, 256, **1024**} concurrent
sessions (`aconnect` + `asyncio.gather`, one event loop, `ping_jitter` engaged at N≥256),
against mock `shinkend` servers in **separate processes** speaking the real ACI over real
loopback WebSockets. Frame payloads are synthetic bytes sized to three measured operating
points (13 / 48 / 87 KiB ≈ JPEG q50@512 / q80@1024 / q80@1080p). 88,928 measured observations:

| N | payload | observe p50 / p99 | round wall p50 | aggregate ingest | RSS | threads |
|---:|---|---:|---:|---:|---:|---:|
| 256 | 48 KiB | 130 / 255 ms | 263 ms | 366 Mbps | 353 MiB | 1 |
| 1024 | 13 KiB | 148 / 177 ms | 182 ms | 609 Mbps | 687 MiB | 1 |
| 1024 | 48 KiB | 246 / 412 ms | 374 ms | **997 Mbps** | 1.2 GiB | 1 |
| 1024 | 87 KiB | 395 / 697 ms | 685 ms | 1,015 Mbps | 1.6 GiB | 1 |

**Sustained** (not a burst): 20.4 s of back-to-back rounds at N=1024 × 48 KiB —
**2,356 frames/s ≈ 884 Mbps of decoded frames through one event-loop thread at 0.99 CPU
cores**, observe p99 476 ms, 47 consecutive rounds. The client plane saturates at ~1 Gbps
decoded ingest per core on this host; it is not the scaling bottleneck.

Thread-model contrast, same mocks: the sync facade spends **one OS thread per session**
(N=256 → 256 threads; 1024 was deliberately not run — that thread model is what this design
retires), while `SharedLoop` holds 1024 sync sessions and the async core holds 1024 concurrent
sessions on **one** thread each.

![client scale](../assets/bench/client_scale.png)

### 2c. Real remote sandboxes over WAN — N ≤ 16 `[remote WAN — one-off]`

One process driving N real remote sandboxes (boot → inject `shinkend` → ws proxy → SDK),
JPEG q80 @1280 observe + click per round (`results/remote/fanout_remote.csv`):

| N | round wall (s) | observe p95 (s) | KiB/sandbox |
|---:|---:|---:|---:|
| 4 | 0.43 | 0.29 | 48.2 |
| 8 | 0.42 | 0.30 | 48.2 |
| 16 | 0.51 | 0.38 | 48.2 |

Round wall stays flat 4→16: WAN-RTT-bound, not contended. Real N>16 was gated by substrate
cost/quota, not the client — which is what §2a/§2b establish independently.

### 2d. Aggregate egress at 1024 `[projection]`

If N sandboxes each emit one 48.3 KiB JPEG q80 @1280 frame per second: **~405 Mbps at
N=1024** — a datacenter NIC. The same workload in full-res PNG (1804.5 KiB) projects to
**~15 Gbps**, infeasible anywhere. These are decoded payload bytes; base64+JSON wire framing
adds ~33% until binary frames land. The chart marks the one *measured* point at N=1024 — the
§2b sustained client-plane ingest — alongside the projected curves.

![aggregate egress projection](../assets/bench/aggregate_egress.png)

---

## 3. Runtime state — checkpoint / fork / resume `[local — rerunnable]`

The differentiating loop — reach a state once, checkpoint it, mint N live replicas — measured
on the Docker disk tier (S4, `bench_fork.py`), with **every replica verified to inherit the
golden state** (a timing row only counts if the fork was real):

| leg | p50 | n |
|---|---:|---:|
| cold boot → usable (create + connect + first obs) | 7.80 s | 16 |
| checkpoint (`docker commit`, sandbox stays live) | **0.57 s** | 16 |
| fork → usable (resume + connect + first obs) | 8.57 s | 63 |

| fan-out from ONE checkpoint | wall-clock | wall / replica | verified |
|---:|---:|---:|---|
| N=1 | 9.60 s | 9.60 s | 1/1 |
| N=8 | 8.55 s | 1.07 s | 8/8 |
| N=32 | 11.92 s | **0.37 s** | 32/32 |

Checkpointing a live sandbox is sub-second and non-disruptive. On the disk tier one fork costs
about one cold boot (both dominated by `docker run` + desktop readiness) — the fork's value
here is **state inheritance** (skipping the setup/replay to re-reach a mid-task state), and
**fan-out wall-clock is nearly flat in N**: 32 verified replicas cost ~11.9 s vs ~9.6 s for
one — **~26× cheaper per replica**. This is the shape `eval.run_eval_forked` exploits
(golden → fork-N → score). The designed CRIU/CoW fast tiers (D5, not built) attack the boot
constant itself.

![fork and fan-out](../assets/bench/fork_resume.png)

---

## 4. Accessibility-tree coverage (spike E5) `[local spike]`

What fraction of each app surface is *addressable* (roled + on-screen bbox + actionable) via
the OS accessibility tree — the load-bearing assumption behind a structured-default fast path:

| surface | tree | pct addressable |
|---|---|---|
| Qt calculator | AT-SPI | **0.87** |
| chromium page | CDP | 0.23 of all nodes — but **1.00 of labeled controls** resolved |
| zenity / gnome-text-editor (GTK) | AT-SPI | 0.10 / 0.09 |
| xterm | AT-SPI | 0.00 (no tree) |
| canvas / games / Electron | — | **unmeasured** |

Tree-diff bandwidth while typing: diff **2.0 KiB** vs full tree 10.4 KiB vs screenshot
76.5 KiB. **Verdict: the data supports a *hybrid* per-window structured + pixel fallback, not
structured-by-default** — strong for Qt and for browser *controls* via CDP, weak for GTK,
absent for terminals — so D3's structured-default stays Provisional.

![a11y coverage](../assets/bench/a11y_coverage.png)

---

## 5. Functional gates

| gate | result |
|---|---|
| **M5 OSWorld alpha gate** | **single-task** end-to-end validation (1 task of the 369-task suite): task `e0df059f` (os/dir-rename), Kimi K2.6 over `shinkend` on a remote sandbox, **official OSWorld evaluator score 1.0**, 6 steps, 110 s. A multi-task conformance sweep has **not** been run |
| E1 scripted real-GUI task | ✓ live (6 ACI actions into xterm, file read back) |
| E6 off-the-shelf model | ✓ live (K2.6 drives a Docker desktop via one adapter) |
| E8 runtime state | ✓ live (Docker disk-tier checkpoint → fork → screenshot) |
| E9 forked eval | ✓ live (3/3 replicas inherit a golden checkpoint) |
| automated tests | 74 Rust + 472 Python, 9-job CI on every PR |

## Reproducing

```sh
# build the sandbox image FROM THE CHECKOUT UNDER TEST, then run all local suites
docker build -f images/linux/Dockerfile -t shinken/sandbox-linux .
make benchmarks                       # ~20-30 min; needs Docker + python3 + matplotlib + websockets

# regenerate every figure from the tracked raw data (no Docker)
python3 benchmarks/replot.py && python3 benchmarks/plot_remote.py
```

The remote-WAN tables (§1b, §2c) are one-off measurements: raw CSVs are tracked for audit, but
the harness that drove them is not published, so they are not rerunnable from this repo.
