# Spike — software video codec vs delta-PNG tiles for motion content (REPORT)

**Question:** can a *software* video codec, in-process inside `shinkend`'s constraints
(single core, zero-latency, Apache-2.0-distributable, arm64), give a **step-function
bandwidth win over the B2 dirty-tile delta path for MOTION content** — the regime
(scrolling, animation, video) where most 64px tiles dirty every frame and delta-PNG's
typing-workload win inverts?
**Decision it informs:** [D4](../../docs/design/tech-decisions.md) (streaming: the designed
answer is dual-transport WebRTC, hardware-encoded where available — not built). This spike asks
whether a *software* encoder tier is worth building **before** that, as a bridge.
**Companion numbers:** [`docs/engineering/benchmarks.md`](../../docs/engineering/benchmarks.md)
(delta-PNG's typing win), [`docs/engineering/streaming-bandwidth.md`](../../docs/engineering/streaming-bandwidth.md).
**Harness:** [`run.sh`](run.sh) → [`gen_frames.py`](gen_frames.py) +
[`harness/`](harness/) (`vcspike`) + [`run_matrix.py`](run_matrix.py); raw machine output
in [`evidence.json`](evidence.json) (66 rows).

**Verdict up front: BUILD — but as an optional, negotiated *motion tier*, not a
replacement.** On motion content the software-H.264 candidate beats the runtime's own
delta-PNG tiles by **60–295×** and beats the quality-matched JPEG paths by **15–39×** —
far past the >5× bar — at 4–7 ms/frame on one arm64 core with zero-latency settings.
On typing/UI it offers nothing the tile path doesn't already do better (lossless, 0.3
ms/frame, true idle silence), so delta-PNG stays the agent-loop default.

## Method

- **Content:** three deterministic 10 s, 1280×800 sequences ([`gen_frames.py`](gen_frames.py)),
  each at 10 and 30 fps:
  - **typing** — sparse desktop, ~12 chars/s into a white terminal + 1 Hz cursor blink (the
    workload shape of `benchmarks/bench_delta_screencast.py`): delta-PNG's home turf.
  - **scroll** — full-width text page scrolling at 120 px/s: every tile in the text area
    dirties every frame.
  - **photo-pan** — procedural photographic frame (vendored
    `benchmarks/_common.synth_photo_ppm`, natural-image statistics) panning at 240 px/s:
    the worst case (all tiles dirty, photo entropy).
- **Incumbent (delta-PNG tiles):** `shinkend` is a binary-only crate, so the spike could not
  link it; instead [`harness/src/b2.rs`](harness/src/b2.rs) is a **faithful replication —
  verbatim copies** of `executor.rs`'s `TILE_SIZE=64`, `KEYFRAME_INTERVAL=30`, `diff_tiles`,
  `tile_rgb`, `encode_png_fast` (fast preset + adaptive filter for tiles), `encode_png`
  (default preset for keyframes), `encode_jpeg`, and the `DeltaState` tick/commit semantics
  (idle suppression, baseline-advance-on-delivery), built against the **same crate versions**
  (`png 0.17`, `jpeg-encoder 0.6`) — payload bytes are byte-identical to the runtime's.
- **Candidates:** in-process **openh264** (`openh264 0.9` crate, `ScreenContentRealTime`,
  rate-control `Bitrate`, intra period 30 to match the B2 keyframe cadence, 1 thread, no
  B-frames/lookahead — zero-latency by construction) at 1/2/4 Mbps; **libvpx VP8/VP9** via
  ffmpeg CLI (`-deadline realtime -cpu-used 8 -lag-in-frames 0 -threads 1 -g 30`) at 1/4 Mbps;
  **x264** (`veryfast` + `zerolatency`, **GPL — reference only, not distributable**) at 4 Mbps.
  Plus the runtime's other levers as lossy baselines: **delta-JPEG q80 tiles** and
  **full-frame JPEG q80** (with the runtime's idle suppression).
- **Metrics:** raw payload bytes/s (pre-base64 — the +⅓ base64-in-JSON wire tax hits every
  contender equally), single-core encode CPU ms/frame, client-side decode ms/frame
  (PNG/JPEG decode + blit, or H.264 decode + YUV→RGB), RGB-domain PSNR (`lossless` = exact).
  ffmpeg rows use `-benchmark` utime (includes rawvideo demux + `rgb24→yuv420p` swscale;
  the in-process h264 rows include the same RGB→YUV step, reported split in evidence.json).
- **Host:** Apple M4 Pro (arm64 macOS), ffmpeg 8.1.1, rustc 1.96. The production target is a
  linux/arm64 guest core — absolute ms/frame will be slower there (the ratios are
  codec-intrinsic); see caveats.

## Results — bytes/s and CPU per content class (30 fps rows; 10 fps in evidence.json)

**typing @ 30 fps** (mostly static; 112 of 300 frames delivered after idle suppression):

| encoder | KB/s | enc ms/frame | dec ms/frame | quality |
|---|---:|---:|---:|---|
| **delta-PNG tiles (B2, incumbent)** | **30.0** | **0.28** | 0.07 | lossless |
| delta-JPEG q80 tiles | 32.0 | 0.33 | 0.18 | 51.1 dB |
| full-frame JPEG q80 | 289.2 | 3.92 | 3.19 | 51.1 dB |
| h264-openh264 @2M | 7.5 | 2.68 | 1.06 | 45.8 dB |
| vp8-libvpx @4M | 18.7 | 3.58 | 0.32 | 50.4 dB |
| vp9-libvpx @4M | 30.9 | 5.06 | 0.32 | 78.5 dB |
| x264-ref @4M (GPL) | 10.6 | 4.76 | 0.39 | 51.6 dB |

Video's bytes look competitive here, but it is lossy, costs ~10× the encode CPU, and emits
**every** frame — a fully idle desktop still burns ~3 ms/frame and trickle bytes, where the
tile path emits literally nothing. **No case for video on this class.**

**scroll @ 30 fps** (every text tile dirty every frame):

| encoder | KB/s | enc ms/frame | dec ms/frame | quality | vs delta-PNG | vs matched-quality JPEG path |
|---|---:|---:|---:|---|---:|---:|
| **delta-PNG tiles (B2, incumbent)** | **11 278.9** | **2.11** | 2.84 | lossless | — | — |
| delta-JPEG q80 tiles | 7 341.1 | 3.85 | 4.86 | 37.3 dB | 1.5× | — |
| full-frame JPEG q80 | 5 283.1 | 4.89 | 5.07 | 37.3 dB | 2.1× | — |
| h264-openh264 @4M | 188.3 | 7.35 | 1.33 | 35.0 dB | **59.9×** | **39.0×** (vs delta-JPEG) |
| vp8-libvpx @4M | 370.7 | 5.27 | 1.02 | 47.4 dB | 30.4× | 19.8× (at *better* PSNR) |
| vp9-libvpx @4M | 815.7 | 7.63 | 2.41 | 41.8 dB | 13.8× | 9.0× |
| x264-ref @4M (GPL) | 355.0 | 5.95 | 1.25 | 48.0 dB | 31.8× | 20.7× |

**photo-pan @ 30 fps** (all tiles dirty, photographic entropy — delta-PNG's catastrophe):

| encoder | KB/s | enc ms/frame | dec ms/frame | quality | vs delta-PNG | vs matched-quality JPEG path |
|---|---:|---:|---:|---|---:|---:|
| **delta-PNG tiles (B2, incumbent)** | **69 443.0** | **4.46** | 6.35 | lossless | — | — |
| delta-JPEG q80 tiles | 8 228.3 | 5.77 | 7.95 | 32.3 dB | 8.4× | — |
| full-frame JPEG q80 | 3 501.7 | 5.22 | 4.72 | 32.3 dB | 19.8× | — |
| h264-openh264 @2M | 235.3 | 4.99 | 1.61 | 31.7 dB | **295×** | **14.9×** (vs full-frame JPEG) |
| vp8-libvpx @4M | 479.8 | 6.26 | 1.53 | 32.7 dB | 145× | 7.3× |
| vp9-libvpx @4M | 627.6 | 13.96 | 3.42 | 32.7 dB | 111× | 5.6× |
| x264-ref @4M (GPL) | 658.8 | 7.10 | 1.85 | 36.2 dB | 105× | 5.3× |

At 10 fps the shape is identical (scroll: 3 781 KB/s delta-PNG vs 74.8 KB/s h264@4M = 50.6×;
photo-pan: 23 148 KB/s vs 79.8 KB/s h264@2M = 290×).

### The verdict the paper needs

1. **For which content classes does video beat delta-PNG by >5×?** Both motion classes,
   by an order of magnitude past the bar: **scroll 60× / photo-pan 295×** against the
   lossless incumbent, and — the honest, quality-matched comparison — **39× / 15×**
   against the runtime's own lossy JPEG levers at equal-or-better PSNR. On **typing it
   does not** (and the right default there remains delta-PNG: lossless text for
   VLM/OCR reading, 0.28 ms/frame, true idle silence).
2. **CPU cost vs the XDamage/tile path:** on motion content the B2 path itself already
   pays 2.1–4.5 ms/frame to produce its MB/s flood; in-process H.264 pays **4–7.5
   ms/frame total** (of which ~0.55 ms is RGB→YUV) — i.e. ~1.5–3× the incumbent's CPU
   for ~2 orders of magnitude less egress, well inside a 33 ms @30 fps single-core
   budget. Decode is *cheaper* than tiles on motion (1.3–1.6 ms vs 2.8–8 ms). A
   sandbox-guest core will be slower than an M4 Pro perf core (rule of thumb 2–4×):
   30 fps stays plausible, 10–15 fps is comfortable.
3. **Latency / agent-loop compatibility:** yes. OpenH264 has **no B-frames and no
   lookahead**; each `encode()` call returns the access unit synchronously, so
   per-frame pipeline latency *is* the encode time (2.7–7.5 ms measured). The one
   sharp edge is **rate-control frame skipping** when the budget is too small for the
   motion: scroll@30 fps at 1 Mbps skipped 104/300 frames (PSNR 18.5 dB — unusable
   judder). Provision ≥4 Mbps for 30 fps full-motion, or drop fps; the spike treats
   measured (bytes, PSNR) pairs as the comparison basis, not nominal targets
   (openh264's screen-content RC also undershoots large targets when quality saturates).
4. **Quality note:** delta-PNG is lossless; every video row is lossy 4:2:0. At 35 dB
   (h264@4M, scroll) text is readable but visibly softened, and chroma subsampling
   fringes colored text — fine for a human watching motion, wrong for an agent reading
   a screen. VP8@4M holds 47 dB on scrolling text (near-transparent). This is another
   reason video is a *tier*, not a default.

## Codec pick + licensing

**Pick: OpenH264 (Cisco) via the `openh264` 0.9 crate** — with one flagged caveat.

| candidate | code license | patents | rust binding / arm64-linux packaging | speed @1280×800 | verdict |
|---|---|---|---|---|---|
| **openh264** | BSD-2-Clause (Apache-2.0-compatible) | **AVC pool (Via-LA): Cisco's royalty coverage applies only to Cisco's own *binary* releases, not from-source builds like the crate's — must be feature-gated/opt-in** | best-in-class: crate vendors the C++ source, builds via `cc`, no system libs; built first-try on arm64 in 15 s | 2.7–7.5 ms/frame, zero-latency by construction | **chosen instrument + recommended tier codec** |
| libvpx VP8/VP9 | BSD-3-Clause | **clean** — WebM royalty-free patent grant | weaker: `env-libvpx-sys` (bindgen + system `libvpx-dev`); heavier for the static-musl story | VP8: 5.3–7.5 ms; VP9: 7.6–17 ms | **patent-clean fallback — its numbers also clear the bar (vp8@4M: 30×/145×)** |
| rav1e (AV1) | BSD-2 + AOM patent grant | clean | pure Rust (ideal) | **not measured**; realtime single-core priors at this resolution are marginal even at speed 10 | deferred candidate, worth a 1-day follow-up |
| x264 | **GPLv2 — incompatible with Apache-2.0 distribution** | AVC pool | n/a | measured via ffmpeg as the upper-bound reference only | excluded |

The pick optimizes for `shinkend`'s actual constraint set: in-process, no system
dependencies, single-core realtime, zero-latency. The H.264 patent posture is the price,
and it is manageable: ship the encoder behind an **off-by-default cargo feature**
(`codec-h264`), document the exposure, and keep **VP8 as the swap-in** if a downstream
distributor needs the royalty-free grant (the negotiation surface below is
codec-agnostic). Substantial parts of the AVC patent pool have expired or expire over the
next few years (jurisdiction-dependent — not legal advice; flag for review before any
binary release).

## Integration sketch (NOT built — where it would slot in)

- **Negotiation:** `start_screencast` grows `codec: "h264"` next to the existing
  `format: png|jpeg` / `delta` knobs (`schema/aci.schema.json`, `protocol.rs`,
  `connection.rs`). A runtime built without `codec-h264`, or whose backend lacks
  `supports_raw_capture()`, **nacks** — exactly the existing
  `delta && !exec.supports_raw_capture()` nack shape in `connection.rs` — and the SDK
  falls back to delta-PNG. Codec advertisement can ride the hello/result envelope later.
- **Loop placement:** `spawn_screencast` in `main.rs` already branches
  `if spec.delta → spawn_delta_screencast`; a third branch `spawn_video_screencast`
  reuses the same skeleton: `capture_raw` (downscale-before-encode) on
  `spawn_blocking`, the same `streams.touch`/`record` resume registry, the same
  bounded-writer `try_send` semantics. Two codec-statefulness deltas: a frame dropped
  on a full writer queue can't just vanish (the decoder needs every emitted AU) — on
  `TrySendError::Full` call `force_intra_frame()` so the next delivered frame re-keys
  (mirrors the B2 `KEYFRAME_INTERVAL` self-heal); and a `resume_stream` restart forces
  an IDR (mirrors the post-resume keyframe).
- **Idle suppression stays:** run the cheap unchanged-frame check (memcmp, ~0.3 ms)
  *before* encoding — a static screen must keep costing ~zero CPU and zero bytes, a
  property the encoder alone doesn't give (it would burn ~3 ms/frame and trickle bytes).
- **Wire:** one AU per observation message (`codec:"h264"`, `seq`, `is_idr`),
  base64-in-JSON like today (+⅓ tax, identical for all contenders). The binary media
  channel remains D4's job; this tier slots beneath it unchanged — and when D4's
  hardware path (e.g. NVENC, where available) arrives, it replaces the *encoder*, not
  the negotiation or the loop.
- **Packaging:** `openh264-sys2` compiles C++ — the injectable **static-musl** `shinkend`
  build would need static libstdc++ on aarch64-musl. Feature-gating keeps the default
  build pure-Rust/static as today; the video tier ships in the fatter image-resident build
  first.
- **Tier selection:** v1 is client-negotiated. (Design note: the delta path already
  computes the dirty-tile count per tick — a sustained high dirty-ratio is the natural
  automatic upgrade signal; not in scope.)

## Caveats (honesty section)

- **Host ≠ target:** measured on an arm64 macOS M4 Pro perf core, not a linux/arm64 guest
  vCPU. Bandwidth numbers are host-independent; CPU numbers scale down (2–4× rule of
  thumb). The production path is in-process (`openh264` crate), which is what the spike
  measured for the chosen codec; libvpx/x264 rows are ffmpeg-CLI instruments whose utime
  includes demux+swscale (slight overstatement of their encode cost).
- **Synthetic content:** full-page scroll and full-frame pan are *upper bounds* on
  dirtiness; real browser scrolling keeps chrome static, which helps delta-PNG somewhat
  but cannot close a 60–295× gap.
- **PSNR methodology** is RGB-domain (harness loop for in-process rows; ffmpeg `psnr`
  filter forced to rgb24 for CLI rows) — comparable, not identical, pipelines.
- The delta-PNG keyframe/tile byte counts were produced by a **replica** of the runtime
  encoder (same functions, same crate versions), not the runtime binary itself —
  byte-identical by construction, but stated for the record.
- VP9 realtime and rav1e were not tuned; VP9's poor showing here (slow + mid quality at
  these settings) reflects `-cpu-used 8` realtime mode, not the codec's ceiling.

## Reproduce

```bash
bash spikes/video-codec/run.sh        # ~5 min: builds vcspike, generates frames,
                                      # runs the 66-cell matrix, writes evidence.json
```

Requires: rust toolchain, python3 + numpy + Pillow, ffmpeg with libvpx/libx264. Scratch
lives in `runs/` (git-ignored, deleted as it goes; peak ~1 GB). No Docker, no network
after the first crates.io fetch.
