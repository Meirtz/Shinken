# Streaming & Bandwidth

> **Status:** drafted · **Owns:** D4 (streaming), the bandwidth half of D3 (observation), and the optional encode tier of D11 (GPU).
> **Date assumptions:** 2026-05-30. **Evidence rule:** every speed / density / cost number that comes from a vendor or third-party blog is tagged **(vendor-published, unverified)**. None of these are first-party Shinken measurements yet; see [open-questions.md](open-questions.md) for the measurement spikes that must close them.

This note is the deep dive behind Shinken's headline streaming claim: **most agent time is near-static UI, so the *meaning* of the screen is far cheaper to ship than its *pixels*.** We send structured operations by default and spin up hardware-encoded video only on demand. It covers the WebRTC dual-transport internals, the codec/encoder facts that decide where video can physically run, the bandwidth-and-dollars math, the dirty-rect / ROI / Set-of-Marks techniques that keep the pixel tiers cheap and crisp, the tuning recipes from the closest open-source analog, the record-while-stream pattern, and the recommendation reconciled to **D4**.

Sibling docs: [../docs/02-architecture.md](../docs/02-architecture.md) (the three planes), [../docs/05-tech-decisions.md](../docs/05-tech-decisions.md) (D4/D11 as ADRs), [../docs/09-economics-and-build-vs-buy.md](../docs/09-economics-and-build-vs-buy.md) (egress cost model), [replay.md](replay.md) (the event stream as the `.skn` log), and [ai-native-interface.md](ai-native-interface.md) (D3 observation rungs that feed Tier 0).

---

## 1. The thesis in one diagram

Shinken runs three planes over **one** WebRTC `PeerConnection` per session:

```
                       ┌─────────────────────── one DTLS-secured PeerConnection ───────────────────────┐
                       │                                                                                │
  Guest Runtime        │   CONTROL plane     EVENT plane (reliable+ordered)     MEDIA plane (on-demand) │   Browser
 (in-Sandbox) ─vsock─► │   lifecycle/        actions + observation diffs        NVENC H.264/AV1 video   │ ◄─ Control
                       │   signaling         + permissions  = THE REPLAY LOG    track, screen-tuned     │    Panel
                       │   (WHIP/WHEP)       (RTCDataChannel/SCTP)               (SRTP media track)      │
                       └────────────────────────────────────────────────────────────────────────────────┘
                                                   │ ingest once (WHIP)
                                                   ▼
                                             ┌───────────┐  fan out (WHEP/SVC), encode-once
                                             │    SFU    │ ───► N human reviewers, per-viewer quality
                                             └───────────┘
```

The **event plane** is primary and always on. It is the agent's actions plus structured observation diffs, and it *is* the append-only replay log (D5). The **media plane** is a fallback that lights up only when a human is watching or when content is pixel-only (canvas/WebGL/video/games/non-instrumented apps). Both share one ICE/DTLS connection, so there is exactly one NAT traversal and one set of ports per session. Host↔guest is **virtio-vsock**, never HTTP screenshot polling — polling is the OSWorld baseline we explicitly reject (see [osworld-teardown.md](osworld-teardown.md)).

The payoff, made quantitative in §4: a structured-default blend at ~20 kbps is roughly **150×** cheaper than 1080p office video at 3 Mbps, and the gap *compounds* with concurrency until egress dollars are the whole argument.

---

## 2. WebRTC dual-transport internals

WebRTC gives you two transports on one connection, and the entire D4 design is "put the right traffic on the right one."

### 2.1 The two transports

| Transport | Wire | Delivery semantics | Shinken use |
|---|---|---|---|
| **SRTP media track** | RTP over DTLS over UDP | Lossy, real-time; GCC/TWCC rate control, NACK/RTX retransmit, optional FEC/RED, receiver jitter buffer | On-demand pixel video (the NVENC H.264/AV1 track) |
| **RTCDataChannel** | SCTP over DTLS over UDP | Configurable: reliable+ordered (TCP-like, default) **or** partial-reliability via `maxRetransmits` / `maxPacketLifeTime` | Primary structured channel + telemetry |

The media-track machinery — congestion control, jitter buffering, retransmit, A/V sync — is battle-tested and *free* if you take the SRTP path; this is the single biggest reason v1 should ride a WebRTC media track rather than a hand-rolled WebCodecs+WebTransport stack (see §7). The DataChannel's per-stream reliability knobs are defined in [RFC 8831](https://datatracker.ietf.org/doc/html/rfc8831); the partial-reliability mechanics are explained well in [WebRTC for the Curious](https://webrtcforthecurious.com/docs/07-data-communication/).

### 2.2 DataChannel reliability tiers

We split the SCTP association into **multiple independent streams**, each with its own ordering, to avoid head-of-line blocking *within* the data channel:

- **Reliable + ordered** stream → agent actions (click/type/key/scroll) and a11y/DOM observation diffs. This is a **correctness** requirement, not an optimization: dropping or reordering an action is a bug, and this stream is the replay log. (D2, D5.)
- **Partial-reliability** stream (`maxRetransmits=0` or short `maxPacketLifeTime`) → high-rate lossy telemetry like cursor position and scroll, where freshness beats completeness.

> **Pitfall:** one SCTP stream for everything. Reliable mode reintroduces head-of-line blocking, so a big a11y-tree snapshot can stall an urgent action ack. Split concerns across separate streams, and budget for the browser's SCTP send-buffer limits with backpressure on chatty observation snapshots.

> **Pitfall:** putting actions on the media track or on a lossy/unordered channel to "save bytes." That can reorder or drop the agent's actions. Only observations and pixel patches may be lossy.

### 2.3 Latency budget — the codec is *not* the lever

The decisive interactivity lever is the **jitter buffer**, not the codec. A clean WebRTC path on a framebuffer source (no camera/USB acquisition penalty) breaks down roughly as: H.264 encode ~10 ms, decode ~10 ms, transport ~10–30 ms on regional paths, with the receiver jitter buffer the largest *controllable* term ([WebRTC latency breakdown](https://transitiverobotics.com/blog/webrtc-latency-breakdown/), vendor-published, unverified).

By default WebRTC's video jitter buffer **over-buffers screen content** — it is tuned for camera video and silently adds tens of milliseconds. The fixes:

1. Set the **playout-delay RTP header extension** to `min=max=0` for lowest latency (or a small `min~20ms / max~80ms` if you see stutter). Targets of 100/150/200 ms are cited for gaming/remoting/interactive respectively ([playout-delay README](https://webrtc.googlesource.com/src/+/refs/heads/main/docs/native-code/rtp-hdrext/playout-delay/README.md); [experiment notes](https://webrtc.github.io/webrtc-org/experiments/rtp-hdrext/playout-delay/)).
2. Set [`RTCRtpReceiver.jitterBufferTarget`](https://developer.mozilla.org/en-US/docs/Web/API/RTCRtpReceiver/jitterBufferTarget) low on the browser side.
3. Tag the track as screen content (`contentHint='detail'` or `'text'`, RTP `content-type=screensharing`) so the pacer drops its camera heuristics.

**SLO targets (D4, to be validated):** same-region reviewer glass-to-glass **~50–120 ms**; cross-region **<200 ms** via PoP placement. `RTT/2` is a hard floor no encoder tuning can beat — cross-region latency is solved with relay-mesh geography, not codec settings. Treat the DataChannel **action round-trip** as a *separate, tighter* SLO, since it gates agent interactivity even when no human is watching.

### 2.4 Congestion control and loss recovery

Prefer **TWCC (transport-cc)** over GCC/REMB: the receiver only timestamps arrivals and the sender/SFU owns the estimate across all streams in one transport ([TWCC primer](https://bloggeek.me/webrtcglossary/transport-cc/); [server-side adaptive simulcast](https://flussonic.com/blog/news/transport-cc)). Probe for a fast ramp on idle→active. On low-RTT regional paths NACK-triggered **RTX** retransmission suffices; add **FEC/RED** only when RTT is too high for a retransmit to arrive before playout ([media resilience](https://getstream.io/resources/projects/webrtc/advanced/media-resilience/)). Keep RTP payloads near **1200 bytes** to avoid loss-amplifying IP fragmentation ([why 1200](https://groups.google.com/g/discuss-webrtc/c/gH5ysR3SoZI)); a keyframe fragments across many packets, so keyframe loss without RTX/FEC is a visible freeze.

### 2.5 Topology: SFU, never P2P

| Topology | Publisher cost | Verdict |
|---|---|---|
| **P2P (mesh)** | N viewers = N encodes + N × bitrate egress | Dev/debug single-reviewer only; encode sessions and ICE/TURN failures compound |
| **SFU** | Encode **once**, server forwards per-subscriber layer, no transcode | **The D4 choice** for the control panel |
| **MCU/transcode** | Decode + re-encode per viewer | Avoid; only as an edge transcode for clients that can't decode the publisher codec |

One agent desktop is watched by many human reviewers/approvers, so the publisher must upload once and let the **SFU** fan out, selecting the layer each subscriber can afford from TWCC estimates *without* re-encoding ([LiveKit SFU internals](https://docs.livekit.io/reference/internals/livekit-sfu/)). Heterogeneous viewers are served either by **simulcast** (publisher encodes the same video at e.g. 720p/360p/180p — works with any codec, costs extra publisher encode; [intro](https://blog.livekit.io/an-introduction-to-webrtc-simulcast-6c5f1f6402eb/)) or by **SVC** (a single VP9/AV1 bitstream with temporal+spatial layers the SFU drops per-subscriber — more efficient, but needs SVC-capable decode; [W3C webrtc-svc](https://www.w3.org/TR/webrtc-svc/)). A 50-participant *mesh* at 360p/500 kbps each-to-all is ~1.23 Gbps outbound — near single-server limits — which is the math that motivates the SFU + relay mesh ([distributed mesh](https://livekit.com/blog/scaling-webrtc-with-distributed-mesh/), vendor-published, unverified).

> **Pitfall:** simulcast/SVC layer switching without keyframe coordination causes freezes and bitrate spikes. This is exactly the logic a mature SFU's forwarder already implements — a strong argument for adopting one rather than under-building it on raw Pion.

### 2.6 Signaling: WHIP / WHEP

Replace bespoke WebSocket signaling with a single stateless HTTP POST exchange: **WHIP** ([RFC 9725](https://datatracker.ietf.org/doc/rfc9725/), March 2025) for sandbox→SFU **ingest**, **WHEP** ([draft](https://datatracker.ietf.org/doc/html/draft-ietf-wish-whep-01)) for SFU→browser **egress**. Both are load-balancer-friendly and CDN-proxyable. The one caveat: vanilla WHIP/WHEP are broadcast-shaped (unidirectional media); Shinken's bidirectional input + DataChannel requires **extending the SDP negotiation** to include the data channel and a back-channel — a known, bounded extension, not a fork ([WHIP/WHEP/MoQ overview](https://webrtc.ventures/2024/08/understanding-whip-whep-and-media-over-quic/)).

### 2.7 The hidden cost center: TURN

STUN is cheap and stateless; **TURN relay is the per-packet CPU + egress-bandwidth tax** that quietly dominates the bill. Expect ~18–35% of connections (especially cellular/restrictive networks) to relay ([NAT traversal guide](https://www.videosdk.live/developer-hub/webrtc/turn-server-for-webrtc)). Self-hosted TURN egress runs ~$0.005–0.02/GB; managed runs ~$0.04–0.06/GB; add ~20% for retransmit/FEC overhead. A worked example: 10,000 streams × 1.2 Mbps continuous ≈ 1,036 TB/month ≈ **~$95k/month** at cloud egress rates ([scaling WebRTC to 10k](https://amsiot.com/blog/scaling-webrtc-to-10000-devices/), vendor-published, unverified). This is itself a second-order argument for keeping the default channel structured: Tier-0 traffic is tiny and rarely needs TURN-relayed media, so a structured default minimizes the most expensive part of the system.

---

## 3. NVENC, codecs, and where video can physically run (D11)

The optional GPU tier exists so that *when pixels are needed*, encode is hardware-offloaded. Public NVIDIA product facts make the GPU selection non-obvious in a decision-changing way.

### 3.1 The headline constraint: the AI flagships have no NVENC

NVIDIA's flagship AI/training GPUs — **A100, H100, H200, B200** — ship **zero NVENC encode engines.** They have NVDEC (decode) and NVJPEG/OFA only ([NVENC Application Note, Codec SDK 13.0](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html); [NVENC overview](https://en.wikipedia.org/wiki/Nvidia_NVENC)). **A streaming encode tier therefore cannot run on the same GPUs an AI fleet typically uses.** Pairing them with streaming silently forces software encode (x264/SVT-AV1) — high CPU, high latency, low density.

And **MIG does not rescue this.** On A100/H100, most MIG profiles expose **0 media engines**; only the `+me` (media-extension) profile gets one of each *available* engine — and since NVENC isn't present, MIG gives you decode, not encode ([Supported MIG Profiles](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html)). For the encode tier, **MIG is the wrong partitioning tool.**

### 3.2 Where encode *does* live

| GPU | NVENC | AV1 encode | Encode role |
|---|---|---|---|
| **Ada L40 / L40S** | 3× 8th-gen | ✅ (to 8K60 10-bit) | **Premium tier** — render + encode together, 4K/AV1, crisp text |
| **Ada L4** | Ada NVENC | ✅ | **Density tier** — many concurrent panel streams |
| **T4 (Turing) / A10 (Ampere)** | 1× | ❌ (decode only on A10) | Cost-optimized H.264/HEVC fallback where AV1 isn't required |
| **A100 / H100 / H200 / B200** | **0** | — | **Never** the encode tier |

- **L40S** carries three independent 8th-gen NVENC engines with AV1, plus enough compute/VRAM (48 GB) to *co-host the guest rendering and the encode* — the default where each tenant needs both a rendered desktop and high-quality encode ([L4 vs L40S](https://acecloud.ai/blog/nvidia-l4-vs-l40s-gpu/)).
- **L4** is the density king: NVIDIA cites **~1,040 concurrent AV1 720p30 low-latency streams across 8× L4** (≈130/L4) at the fastest (P1) preset ([L4 product page](https://www.nvidia.com/en-us/data-center/l4/), vendor-published, unverified). 72 W, single-slot, cheap to rack.
- **T4** does ~32–38 simultaneous 1080p30 H.264/HEVC transcodes on its single encoder — fine as a cost fallback, but no AV1, so it forfeits the ~40% bandwidth win.

> **Decision (D11):** decouple the encode tier from the AI/render fleet. Stand it up on **L4 (density)** and **L40S (premium 4K/AV1 + render)**, with **T4/A10 only** as a cost-optimized H.264/HEVC fallback. **No MIG for encode.** Multi-tenant the encoders by packing many sessions onto one qualified GPU (no session cap, below) or via **time-sliced vGPU**, where all vGPUs share the card's encode engines ([vGPU features](https://docs.nvidia.com/vgpu/knowledge-base/latest/vgpu-features.html); [time-sliced vs MIG-backed](https://research.colfax-intl.com/sharing-nvidia-gpus-at-the-system-level-time-sliced-and-mig-backed-vgpus/)).

### 3.3 The "8-session cap" is a consumer-only myth

The infamous ~8 concurrent NVENC sessions/system cap applies **only to non-qualified consumer GeForce** cards (raised over time 2→3→5→8; [history](https://www.tomshardware.com/news/nvidia-increases-concurrent-nvenc-sessions-on-consumer-gpus)). **Qualified datacenter/pro GPUs (T4/A10/L4/L40S/RTX PRO) have no artificial cap** — concurrency is bounded by encoder throughput, VRAM, and memory bandwidth ([NVIDIA dev forum](https://forums.developer.nvidia.com/t/how-to-increase-number-of-nvenc-concurrent-sessions/169367)). The inverse trap is assuming datacenter GPUs are *infinite*: size against real encoder bandwidth (resolution × fps × codec), not session count, and leave VRAM/bandwidth headroom.

### 3.4 Codec selection: AV1-first with strict capability negotiation

AV1 on Ada NVENC delivers **~40% bitrate savings vs H.264** at equal quality (NVIDIA-measured: 42 dB PSNR at 7 Mbps for AV1 vs 11 Mbps for H.264 at 1080p60, +1.5–2 dB PSNR, ~500 fps single-stream vs ~56 fps x264) ([AV1 on Ada](https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/), vendor-published, unverified). AV1's **screen-content tools** (palette mode + intra-block-copy) make it especially good for crisp UI text, not just bandwidth.

But the win evaporates if the viewer can't hardware-decode AV1. Browser reality (2025/2026): **H.264 is universal**; **HEVC-in-WebRTC is default in Chrome 136+** (HW decode ~99% macOS / ~75% Windows); **AV1 HW decode is still limited** to recent Intel/AMD/Apple/RTX silicon ([Chromium HEVC intent](https://groups.google.com/a/chromium.org/g/blink-dev/c/3h8lL8a377c)). So:

> **Negotiate AV1 → HEVC → H.264 by client decode capability.** Always ship the H.264 fallback so any browser works; light up AV1 only where HW-decodable, banking the ~40% win without breaking older clients or trading bandwidth for CPU-decode jank.

A defining NVENC property: **encode latency is near-invariant across quality presets P1–P7**, so you can run high quality (P5–P7) *at* low latency. Ultra-low-latency tuning hits ~83 ms / 5 frames glass-to-glass for 4K ([arXiv 2511.18688](https://arxiv.org/abs/2511.18688), academic, unverified); consumer cloud gaming demonstrates <40 ms click-to-pixel with NVENC + Reflex-style pipelining + AV1 + ABR ([GeForce NOW AV1](https://clouddosage.com/geforce-now-av1/), vendor-published, unverified) — the latency north star.

### 3.5 Zero-copy capture → encode, per OS

Keep frames **GPU-resident** from capture to RTP — host memory copies kill latency and density.

- **Linux:** NVFBC framebuffer grab (supported on Linux; zero-copy into GPU memory) ([Capture SDK](https://developer.nvidia.com/capture-sdk)).
- **Windows:** **DXGI Desktop Duplication (DDA)** — NVFBC is **deprecated/frozen on Windows** (broke at Win10 2004; [deprecation bulletin](https://developer.download.nvidia.com/designworks/capture-sdk/docs/NVFBC_Win10_Deprecation_Tech_Bulletin.pdf)). DDA is dirty-rect aware and delivers GPU-memory surfaces straight to NVENC.
- **macOS:** **no NVENC at all** — use VideoToolbox (Apple HW H.264/HEVC). A cross-OS guest fleet needs this per-OS capture/encode front-end, all normalizing into the *same* WebRTC egress.

> **Pitfall:** NVENC defaults to **4:2:0 chroma**, which blurs sharp UI text and thin lines — degrading both human review and agent OCR. Use AV1/HEVC screen-content modes, higher bitrate on text regions, or 4:4:4/AVC444 where supported. See §5 for the ROI approach.

### 3.6 Build-vs-buy: NICE / Amazon DCV

**NICE / Amazon DCV** is the publicly available remote-display product (NVENC H.264/HEVC, QUIC/UDP default since 2024, browser-native client, automatic bitrate/framerate adaptation, proven 4K60 over WAN) ([What Is Amazon DCV](https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html); [DCV over QUIC](https://aws.amazon.com/blogs/gametech/stream-remote-environment-nice-dcv-quic-udp-4k-monitor-60-fps/)). It runs on exactly the qualified encode GPUs and is the **build-vs-buy fork** for the pixel channel: adopt it wholesale if wire-format control isn't required, **or** replicate its design in a GStreamer/WebRTC stack so the *same connection* can also carry Shinken's structured event channel — which DCV does not. That structured-channel requirement tips D4 toward the custom pipeline as the default, with DCV the documented alternative (D11, [../docs/09-economics-and-build-vs-buy.md](../docs/09-economics-and-build-vs-buy.md)).

---

## 4. Structured-vs-pixel bandwidth math and $/mo

This is the economic core of D3+D4. The claim is not "structured is free" — it is "structured, *diffed*, is one to two orders of magnitude cheaper, and the difference is the whole bill at scale."

### 4.1 Per-stream bandwidth (24×7, one desktop)

| Representation | Bitrate | GB/month | Notes |
|---|---|---|---|
| Generic H.264 1080p video | ~5 Mbps | ~1,620 | Camera-tuned baseline |
| H.264 1080p **office** content | ~3 Mbps | ~972 | The thing to beat |
| NVENC screen-tuned H.264 | ~1.5 Mbps | ~486 | |
| AV1-SCC, busy screen | ~0.5 Mbps | ~162 | Screen-content coding |
| AV1-SCC, **normal** screen | ~0.1 Mbps | ~32 | ~100 kbps |
| Structured, **active** | ~0.05 Mbps | ~16 | actions + a11y/DOM diffs |
| Structured, **idle** | ~5 kbps | ~1.6 | event-triggered, diffed |

Sources: [Visionular AV1-SCC](https://visionular.ai/av1-screen-content-coding/) (~100 kbps normal, rarely >500 kbps under motion, >80% below x264, ~27% from intra-block-copy alone); [rrweb / PostHog session replay](https://posthog.com/blog/session-recording-performance) (a 5-minute DOM session compresses to ~100–500 KB, ~5–13 kbps); [A11y-CUA](https://arxiv.org/html/2602.09310) (a11y-tree diffs at agent cadence ~16–80 kbps). All **vendor-published / academic, unverified.**

**Reduction ratios:** a structured **blend** (~20 kbps) is **~150×** less than H.264 office (3 Mbps) and **~250×** less than generic H.264 video (5 Mbps). AV1-SCC normal (100 kbps) is **~30×** less than H.264 office. This is the canon's ~150× anchor.

### 4.2 The $/mo case (egress only, AWS-tiered)

Using AWS tiered egress (~$0.09/GB first 10 TB → ~$0.05/GB beyond 150 TB; [overview](https://aws.amazon.com/blogs/architecture/overview-of-data-transfer-costs-for-common-architectures/)), egress-only, excluding compute/TURN/SFU CPU:

| Concurrent desktops (24×7) | H.264 office (3 Mbps) | AV1-SCC normal (0.1 Mbps) | Structured blend (20 kbps) |
|---|---|---|---|
| **1,000** | ~$52.5k/mo | ~$2.8k/mo | ~$0.58k/mo |
| **10,000** | ~$490k/mo | ~$20k/mo | ~$5.4k/mo |
| **100,000** | ~$4.86M/mo | ~$166k/mo | ~$36k/mo |

Annualized at 100k concurrent: H.264 office ≈ **$58.4M/yr** vs structured blend ≈ **$0.44M/yr** → **~$58M/yr of egress saved** by structured-default — *before* counting the TURN/SFU CPU that video also incurs and structured largely avoids. (All figures vendor-pricing-derived, **unverified**; the blend rate is the load-bearing assumption — see §4.4.)

```
$/mo at 100k concurrent (24×7), bar-ish scale:
 H.264 office  ████████████████████████████████  $4.86M
 AV1-SCC norm  ██                                 $166k
 Structured    ▏                                  $36k
```

### 4.3 One representation, four products

The structured event stream is not *just* the cheap channel — it is simultaneously the **live view feed**, the **scrubbable/forkable replay timeline** (the `.skn` log, D5), the **audit trail**, and the **permission-gating queue** (D6). Collapsing streaming + record + replay + approval into a single append-only timestamped event model is the highest-ROI architectural decision here: bandwidth optimization becomes a *consequence* of the data model rather than a separate feature. (See [replay.md](replay.md) and [permissions.md](permissions.md).)

### 4.4 Where the win actually comes from — and the caveat

The order-of-magnitude win comes from **diff discipline**, not merely from choosing JSON over pixels. Naive full a11y/DOM snapshots every tick on a 4K screen can *rival* video. The mechanics that collapse idle cost toward zero:

- Diff against the prior snapshot; never snapshot on a fixed clock.
- Trigger on **focus-change** or **change-after-cooldown** (A11y-CUA uses a 15 s cooldown, depth-4 trees).
- Coalesce noisy mutations to final value (rrweb model).
- Prune to interactive/changed nodes; mask sensitive text (structured streams leak field values *more* legibly than blurry video).

> **Carry into [open-questions.md](open-questions.md):** the entire economic case rests on the blend being ~10–30 kbps with infrequent pixel escalation. This **must be measured on representative agent workloads** before density/cost targets are committed. The related load-bearing risk is **a11y coverage** on Electron/Qt/canvas/games — the silent failure mode where structure goes blind exactly when it matters. The mitigation is automatic escalation to pixels (§5).

---

## 5. Dirty-rect, ROI, and Set-of-Marks: keeping the pixel tiers cheap and sharp

The pixel path never disappears — humans want real pixels, and canvas/WebGL/video/non-instrumented apps have no usable a11y/DOM tree. The resolution is an explicit **three-tier** protocol with automatic escalation:

```
 Tier 0  ALWAYS ON   structured events: actions + a11y/DOM diffs + Set-of-Marks IDs   ~5–80 kbps   (= the replay log)
 Tier 1  ON DEMAND   dirty-rect ROI lossless/Tight tiles for changed regions          variable
 Tier 2  RARE        full NVENC H.264/AV1-SCC video                                    ~0.1–0.5 Mbps
                          ▲
  escalate when: (a) a human attaches to watch, OR
                 (b) an a11y/DOM coverage gap (canvas/WebGL/video/non-instrumented), OR
                 (c) dirty-rect churn structure can't summarize.
  auto-tear-down on detach.
```

### 5.1 Dirty-rect metadata as a near-free routing signal

RFB's native model sends only changed rectangles; even when you *don't* send the pixels, the **"which rectangles changed, where"** signal is a cheap structured hint ([RFC 6143](https://www.rfc-editor.org/rfc/rfc6143.html)). Capture it at the compositor/hypervisor boundary (DDA on Windows gives this natively) and use it to (1) drive ROI tiling, (2) decide *when* to escalate tiers, and (3) cross-check that structured observations actually reflect screen changes — catching a11y coverage gaps. An idle desktop costs near-zero in this model, matching the agent-idle reality. CopyRect makes scroll/window-move essentially free.

### 5.2 ROI + content-aware tiling + build-to-lossless

Classify regions by content and encode each with the right tool, driven by a11y bounding boxes + dirty-rects:

- **Lossless / palette / 4:4:4 (AVC444) tiles** for text the agent or human must read — avoids the 4:2:0 chroma blur that ruins UI text and OCR.
- **Lossy AV1/H.264** only for genuinely animated regions.
- **PCoIP-style progressive build-to-lossless:** a fast lossy preview, refined to pixel-perfect once a region goes idle or a reviewer pauses on it — eventually-perfect text without paying continuous full-frame lossless cost.

This is how Tier 1 stays cheap and Tier 2 stays sharp. (KasmVNC's per-rectangle adaptive quality and QOI/WebP lossless tiles are a useful image-codec reference for the no-GPU fallback; [KasmVNC](https://kasm.com/kasmvnc).)

### 5.3 Set-of-Marks: the connective tissue

Assign stable IDs to interactive elements and stream just the **ID → bounding-box + role** map. The agent references elements by ID (`click [id=14]`) instead of raw pixels, which makes actions **resolution- and theme-independent and replayable**, reduces click-grounding error, and gives reviewers a legible "what did it click" overlay on any Tier-1 thumbnail or Tier-2 frame. This is the OSWorld observation pattern (screenshots + a11y tree + Set-of-Marks) and is part of the D3 observation rungs ([ai-native-interface.md](ai-native-interface.md)). It is an integer + a rect per element — tiny on the wire, large in leverage.

### 5.4 Make pixels a metered premium

Default sessions run **headless on Tier 0**; a reviewer "attaching" lights up Tier 2 for *that session only*. Expose per-session **pixel-seconds and bytes** in the Control Panel so operators see the cost of watching. This is what turns the dominant cost from a 24×7 baseline into a rare, intentional action — and it is precisely the "unlock advanced image features" framing that ties bandwidth optimization to the permission panel (D6). It also dovetails with the D9 auto-suspend-to-snapshot-on-idle, since idle dominates cost.

---

## 6. neko tuning recipes (the closest open-source analog)

[neko](https://github.com/m1k1o/neko) is the closest open analog to Shinken's browser-delivered real-time streaming: a Go server captures an X11 display via GStreamer, encodes to VP8/VP9/AV1/H.264 + Opus, sends media over WebRTC tracks, and carries low-latency input + cursor over a binary DataChannel — multi-user with one "host" holding control. It is a near-direct teardown target for our pixel path; [Selkies](https://github.com/selkies-project/selkies) is its architectural sibling with first-class NVENC and is the likely Linux starting codebase. What to **adopt** vs **beat**:

**Adopt (proven patterns):**

- **Shared-encoder fan-out.** Many WebRTC peers attach as *listeners* to the SAME encoder pipeline per quality; the pipeline is created lazily on first listener and destroyed on last, so **idle qualities cost nothing**. CPU scales with #qualities, not #viewers — the encode-once-per-desktop discipline D4 demands.
- **Runtime source-swap, not renegotiation.** Quality changes re-point a stable track's sample source — instant, no SDP glare.
- **Keyframe lobby.** A new viewer waits for (and triggers exactly one) keyframe rather than forcing a global IDR that spikes everyone's bandwidth. New SFU subscribers and recording starts must begin on an IDR; this is the mechanism.
- **Clean media/control split** with a compact `{opcode, len}` binary input protocol, plus a PING/PONG with split timestamps to measure RTT cheaply.
- **GCC + hysteresis ABR.** neko pairs a Pion GCC send-side estimator + TWCC with a trend detector and hysteresis (stable/unstable/stalled timers, up/down backoffs, a diff threshold) so it doesn't flap on noisy estimates.
- **Input hygiene + solved edge cases.** Debounce/timed auto-release of keys so a dropped "up" never wedges the session; ICE-Lite, UDP+TCP mux, trickle ICE, NAT1To1 auto-IP, and a server-pinned DTLS role (avoids an iOS renegotiation failure) all already handled.

**Encoder-tuning cheat sheet (copy and re-validate):**

| Codec | Realtime params |
|---|---|
| x264 (sw fallback) | `tune=zerolatency speed-preset=veryfast bframes=0 key-int-max=…` |
| VP8 | `deadline=1 cpu-used=4 end-usage=cbr` + explicit buffer-size/initial/optimal |
| **NVENC** | `rc-mode=cbr (low-latency) preset=low-latency-hq tune=low-latency zerolatency=true bframes=0 rc-lookahead=0`, long/infinite GOP + intra-refresh |

The NVENC properties align with the GStreamer `nvcodec` low-latency set: `tune=low-latency` (1.24+), `zerolatency=true`, `bframes=0`, `rc-lookahead=0`, `rc-mode=cbr`, `gop-size 30–60` or infinite with intra-refresh ([nvautogpuh264enc](https://gstreamer.freedesktop.org/documentation/nvcodec/nvautogpuh264enc.html); [nvh264enc](https://gstreamer.freedesktop.org/documentation/nvcodec/nvh264enc.html)). Use `nvautogpuh264enc` for the universal H.264 fallback and `nvav1enc`/`nvh265enc` for capable clients — same graph, swap the encoder element.

**Beat / differentiate (neko's gaps):**

- **Coarse, slow ABR:** discrete qualities, multi-second hysteresis (neko defaults: stable 12 s, stalled 24 s, downgrade backoff 10 s), no SVC, no audio adaptation. Shinken wants finer/faster steps or true SVC.
- **Full-frame capture regardless of motion** (`ximagesrc use-damage=false`) wastes encode/bandwidth on static screens — Shinken adds **damage-driven / idle-suppressed capture** (the dirty-rect signal of §5.1).
- **No structured observation channel and no interactive replay** — neko has only RTMP broadcast and a JPEG screencast fallback. This is Shinken's clearest differentiation: record the encoded media **and** the DataChannel control/observation timeline *together* (§7).
- **cgo/copy cost:** neko does a per-encoded-frame copy across the cgo boundary; at high concurrency consider zero-copy handoff.
- **Per-peer-only control loop:** no global admission control or cross-session bandwidth budgeting — Shinken's Fleet Manager / Action Gateway (D9) is where that belongs.

---

## 7. Record-while-stream (replay for free)

Recording must not cost a second encode, and a viewer dropping must never corrupt the record.

### 7.1 Encode once, split the encoded stream

The double-encode trap only happens if you use `MediaRecorder`/composite/transcoding egress. The cheap, bit-exact pattern is to **split the already-encoded elementary stream**:

- **GStreamer `tee`** after NVENC: one branch → `webrtcbin` (live), one branch → `h264parse ! mp4mux ! filesink` (record). The record branch costs ~zero extra GPU because it muxes already-compressed NAL units ([tee + mp4mux recipe](https://github.com/crearo/gstreamer-cookbook/blob/master/README.md)).
- **LiveKit Track Egress** writes the H.264 RTP track to MP4 *"as is, without transcoding"* (VP8→WebM, Opus→Ogg) — the managed alternative ([Track Egress](https://docs.livekit.io/transport/media/ingress-egress/egress/track/)).

Use **fragmented MP4 / CMAF** segmented on **IDR boundaries** so the recording is crash-safe (no moov-at-end) and immediately replayable, and force periodic keyframes (e.g. every 1–2 s) so recording-start and SFU late-join both decode immediately at the cost of a small bitrate bump ([fMP4](https://gstreamer.freedesktop.org/documentation/isomp4/mp4mux.html)).

> **Pitfall:** recording started off a non-keyframe = an undecodable segment. IDR-aligned segmentation + forced periodic keyframes are mandatory. Also handle mid-session resolution/DPI changes (desktop resize) as a new segment/track, or a single MP4 track breaks.

### 7.2 The event stream is canonical; MP4 is the pixel re-render track

Critically: the **structured event/a11y-diff DataChannel is the canonical replay log** (the `.skn` `events.jsonl`, D5); the recorded fMP4 is a **synchronized pixel re-render track**, not the source of truth. This keeps storage tiny for idle desktops and makes replay scrubbable/forkable/greppable. **Decouple recording from viewer liveness** — record at the encoder/SFU, never in the browser — so a viewer drop never corrupts the record ([client-vs-server recording](https://bloggeek.me/recording-webrtc-sessions/)). For the *asynchronous* replay/scrub viewer (seconds of latency fine), feed the recorded CMAF to **MSE** — but never for the *live* path; MSE is a buffered segment-playback API multiple segments behind real-time. (Replay UX detail in [replay.md](replay.md).)

### 7.3 Reconnection

WebRTC recovers from NAT rebinding / Wi-Fi→cellular via **ICE restart** (`restartIce()`, [RFC 8445](https://bloggeek.me/webrtcglossary/ice-restart/)) — re-gather candidates in place with only a brief media freeze, saving hundreds of ms vs full renegotiation. Monitor `iceConnectionState`, restart on `disconnected`/`failed` with a full re-offer fallback (restart reliability is implementation-dependent), and request a **PLI keyframe on every reconnect** so the viewer paints immediately. A future WebTransport/MoQ path (§8) gets QUIC connection migration + 0-RTT resume natively, which is materially simpler.

---

## 8. Recommendation, reconciled to D4

**D4 stands.** Concrete build:

1. **One PeerConnection, dual-transport.** Reliable-ordered DataChannel (actions + a11y/DOM diffs = the replay log) + a partial-reliability sub-channel (cursor/scroll) + an **on-demand** NVENC SRTP video track. Host↔guest is virtio-vsock, not HTTP polling.
2. **Three tiers, automatic escalation.** Tier 0 structured (~5–80 kbps, always on) → Tier 1 dirty-rect/ROI lossless tiles → Tier 2 NVENC H.264/AV1-SCC. Escalate on human-attach, a11y gap, or dirty-rect churn; tear down on detach. Meter pixel-seconds in the Control Panel.
3. **SFU topology, never P2P.** Encode once per desktop; fan out via WHEP/SVC, per-viewer layer selection on TWCC. Build a Pion-based SFU fused with the event/replay model, or adopt LiveKit; signal with WHIP+WHEP extended to negotiate the DataChannel.
4. **Minimize the jitter buffer** (playout-delay `min=max=0` / low `jitterBufferTarget`) and **tag screen content** — the biggest controllable latency win. TWCC; NACK/RTX on regional paths, FEC only when RTT forces it; RTP payload ~1200 B.
5. **Encode tier on L4/L40S, never on A100/H100/H200/B200.** No MIG for encode; pack sessions or use time-sliced vGPU. AV1 → HEVC → H.264 by client decode capability; H.264 always available. Per-OS zero-copy capture (NVFBC Linux / DDA Windows / VideoToolbox macOS). **NICE DCV** is the documented build-vs-buy alternative.
6. **Record-while-stream via a GStreamer `tee` (or Track Egress) to fragmented MP4**, IDR-aligned, server-side, independent of viewers. The event stream is canonical; the MP4 is the synchronized pixel layer.
7. **Borrow neko/Selkies patterns** (shared-encoder fan-out, lazy pipelines, runtime source-swap, keyframe lobby, GCC+hysteresis ABR, binary input protocol). **Beat** them on damage-driven capture, finer ABR/SVC, cross-session budgeting, and the structured channel + integrated replay they lack.
8. **v2 fast path (after v1):** WebCodecs → Canvas/WebGPU over WebTransport/MoQ for per-frame AV1 control, QUIC migration + 0-RTT, and edge-cached replay. Not the sole path while WebTransport support is uneven and you'd re-implement the congestion control/jitter/FEC WebRTC gives free ([WebCodecs/WebTransport/WebRTC](https://webrtchacks.com/webcodecs-webtransport-and-webrtc/); [MoQ](https://blog.cloudflare.com/moq/)).

**Open risks carried to [open-questions.md](open-questions.md):** (a) the ~10–30 kbps structured blend rate and pixel-escalation frequency are unmeasured; (b) a11y coverage on Electron/Qt/canvas/games is *the* load-bearing assumption and needs a measurement spike; (c) all NVENC density, AV1-savings, latency, and egress figures here are **vendor-published and unverified** — a first-party measurement plan is required before any density/cost commitment.

---

*Citations consolidated in [sources.md](sources.md) §3 (Streaming, WebRTC & NVENC). All vendor speed/density/cost figures are unverified pending first-party measurement.*
