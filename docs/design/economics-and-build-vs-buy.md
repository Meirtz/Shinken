# 09 — Economics & Build-vs-Buy

> Status: drafting · Last updated 2026-05-30 · Owner: economics workstream
>
> This doc puts dollars on the architecture. It models egress, host density, and per-sandbox cost
> across the streaming and isolation tiers fixed in [`05-tech-decisions.md`](tech-decisions.md)
> (decisions **D1**, **D4**, **D9**, **D11**), works the **build-vs-buy** call against the public
> open-source and commercial options, sizes the warm pools the Fleet Manager (**D9**) must hold, and
> — most importantly — specifies the **first-party measurement plan** that must replace every
> vendor-published number below before any figure here is allowed into a capacity plan or an SLA.
> Cross-links: [00 Vision](vision.md) · [01 PRD](prd.md) · [02 Architecture](architecture.md) ·
> [04 Landscape](landscape.md) · [05 Tech decisions](tech-decisions.md) ·
> [06 Roadmap](../engineering/roadmap.md) · [08 Isolation & capability note](threat-model.md). Sources:
> [`../../notes/sources.md`](../../notes/sources.md).

**Summary.** Shinken's economic thesis fits in one sentence: *the steady-state cost of running an
agent desktop is bandwidth and idle compute — not encode, not boot.* The architecture is engineered
to attack exactly those two costs. The structured observation channel (**D3**/**D4**) — the *target*
default, gated on the §5 measurement plan and the a11y-coverage spike, not yet a committed scale
default — is modeled at roughly **150× cheaper** than H.264 office video, and the copy-on-write fork
model (**D1**) makes
host density a function of *private dirty RSS*, not snapshot size, so hundreds of idle agents can
share a single warm parent. On build-vs-buy the call is to **buy the substrate, broker, and pixel
codec as commodities** — they are well-served by mature open-source and public products — and **build
only the layers nobody ships**: the typed ACI, the structured-default streaming protocol, the
event-sourced forkable replay, the Sandbox Capability Manager, and the eval layer. Every
speed, density, and cost number below that we did not generate ourselves is marked
**(vendor-published, unverified)**. Section 5 is the plan to retire those labels.

---

## 1. The concurrency / cost model

We model the dominant cost lines for a fleet of always-on agent desktops at three scales —
**1k / 10k / 100k concurrent** — across the three streaming regimes the protocol supports (**D4**).
The point of the tables is not their precision (they are wrong; that is what §5 fixes) but their
*shape*: the ranking between regimes is robust even if every absolute number moves ±50%.

### 1.1 Egress: the line that dominates

All egress dollars use the public tiered cloud data-transfer schedule that the streaming research
cites ([AWS data-transfer overview](https://aws.amazon.com/blogs/architecture/overview-of-data-transfer-costs-for-common-architectures/)):
`$0.09/GB` for the first 10 TB/mo, `~$0.085/GB` to 40 TB, `~$0.07/GB` to 100 TB, and `~$0.05/GB`
beyond ~150 TB; the first 100 GB/account/mo is free. These are list prices and are
**(vendor-published, unverified)** — any real deployment is governed by committed-use or
private-pricing discounts we have not modeled. Per-stream bitrates are likewise vendor figures: a
generic camera-tuned H.264 1080p stream is ~5 Mbps; **H.264 office content ~3 Mbps**; NVENC
screen-tuned ~1.5 Mbps; **AV1 screen-content-coded (AV1-SCC) ~0.1 Mbps normal / ~0.5 Mbps busy**
([AV1-SCC analysis](https://visionular.ai/av1-screen-content-coding/)); and a **structured blend
~0.02 Mbps (20 kbps)**, collapsing toward ~5 kbps when the desktop is idle (structured streaming is
diff-driven, and an agent desktop is idle most of the time —
[rrweb session-replay measurements](https://posthog.com/blog/session-recording-performance)).

A 24×7 stream at bitrate `b` (Mbps) moves `b × 0.3285` TB/month. The table applies the tiered
schedule to the full fleet at each scale.

**Egress $/month, 24×7, by streaming regime and concurrency** *(vendor-published, unverified)*

| Regime (avg bitrate)             | 1k concurrent | 10k concurrent | 100k concurrent |
|----------------------------------|--------------:|---------------:|----------------:|
| H.264 generic video (5 Mbps)     |     ~$84,900  |      ~$814,000 |       ~$8.1 M   |
| **H.264 office (3 Mbps)**        |     ~$52,500  |  **~$490,000** |    **~$4.86 M** |
| NVENC screen-tuned (1.5 Mbps)    |     ~$26,500  |      ~$250,000 |       ~$2.5 M   |
| **per-step screenshot polling — incumbent (≈0.2–1 Mbps avg-equiv)** *(estimate)* | ~$5,500–28,000 | ~$45,000–250,000 | ~$330k–2.5 M |
| **AV1-SCC busy (0.5 Mbps)**      |     ~$12,000  |       ~$85,000 |   **~$814,000** |
| **AV1-SCC normal (0.1 Mbps)**    |      ~$2,800  |       ~$20,000 |       ~$166,000 |
| structured active (0.05 Mbps)    |      ~$1,400  |       ~$12,000 |        ~$85,000 |
| **structured blend (0.02 Mbps)** |     **~$580** |    **~$5,400** |    **~$36,000** |

```
egress $/mo at 100k concurrent (log scale, vendor-published, unverified)

 H.264 video 5Mbps   ████████████████████████████████████  ~$8.1M
 H.264 office 3Mbps  ██████████████████████████            ~$4.86M
 screenshot poll     ██████–██████████████   (est.)        ~$330k–2.5M  ← incumbent baseline
 AV1-SCC busy 0.5    ████████                              ~$814k
 AV1-SCC normal 0.1  ███                                   ~$166k
 structured active   █▌                                    ~$85k
 structured blend    ▌                                     ~$36k   ← Shinken target default (gated, D3/D4)
```

Two facts jump out, and the win is best read against *two* baselines — the actual incumbent and the
video strawman — not just one. **Versus the incumbent (per-step screenshot polling, the loop OSWorld
and most CUA stacks run today):** at 100k concurrent a structured blend is **~$36k/mo vs an estimated
~$330k–2.5M/mo — roughly a 10–60× egress reduction**, with the wide band reflecting that the
incumbent's cost is itself an estimate (~0.2–1 Mbps avg-equiv at agent cadence, screenshot size ×
step rate), not a measured number. **Versus the video strawman (24×7 H.264 office):** the gap is
**~$4.86M/mo vs ~$36k/mo — about $58M/yr** in egress alone, before the second-order WebRTC relay cost
that video also incurs and that structured traffic largely avoids; that ~150× figure measures
structured against always-on video, *not* against the incumbent. Either way, the **regime choice
swamps everything else**, and **AV1-SCC is the right *fallback* codec, not the steady state**: even at
its cheap "normal screen" point it is ~5× the structured blend, and AV1-SCC gives ~40% bitrate savings
vs H.264 at equal quality (vendor-published). The architecture therefore **targets structured as the
default** — gated on the §5 measurement plan and the a11y-coverage spike before any scale/cost
commitment — with pixels a metered, human-triggered escalation (**D3**/**D4**).

This is *why* the dual-channel protocol exists. The reliable-ordered data channel carrying the
structured event stream — the actions, accessibility-tree diffs, and Set-of-Marks element refs that
*are* the replay log — costs ~150× less than pushing H.264 office video, and ~250× less than naive
camera-tuned video. The on-demand NVENC media track lights up only when a human attaches to watch or
when content is pixel-only (canvas/WebGL/video/non-instrumented apps). Pixel-seconds and pixel-bytes
must be **first-class, per-session metered quantities** in the Control Panel so operators can see the
cost of watching. The entire structured win evaporates if Tier-2 video is left on for every session;
that behavioral assumption is the one the §5 plan must validate.

### 1.2 The hidden egress cost: WebRTC relay, not the codec

The codec is rarely the bottleneck at scale; **TURN relay and SFU fan-out egress are**. The research
puts a TURN bill near **~$95k/mo at 10k relayed streams** and budgets **~1.5 GB/participant-hour at
720p** (alert above ~2.5 GB/ph); untuned simulcast can triple receiver cost
([scaling WebRTC](https://amsiot.com/blog/scaling-webrtc-to-10000-devices/)) — all vendor-published,
unverified. The structural lever is the same as §1.1: keep the default channel structured (~20
kbps, rarely needs a TURN-relayed media path), fan out the rare Tier-2 video via an SFU with
simulcast/SVC, pin low-bandwidth reviewers to lower layers, and treat any sustained media stream as a
billable event. A structured-default fleet simply never generates the relay traffic that produces a
six-figure TURN bill, so the SFU is sized to the *peak number of attached human reviewers*, not to
the fleet size.

### 1.3 Host density: bounded by private dirty RSS, not snapshot size

The fork-from-snapshot model (**D1**) makes density a memory-sharing problem. A golden microVM boots
once with the agent stack warm and is snapshotted to an immutable `memory.bin` (the RAM image) plus a
small `vmstate` file. Every session is a *new* KVM microVM whose guest RAM is a `MAP_PRIVATE` view of
that one shared file: clean pages are shared read-only across all forks, and only **written (dirty)
pages** cost real host RAM. The published anchors *(vendor-published, unverified)*:

- CoW `mmap` itself ~4 µs; **end-to-end fork P99 ~1.3 ms** at 1000 concurrent forks — the cost is KVM
  VM-create (~99.5%), not the memory mapping ([Morph Infinibranch](https://www.morph.so/blog/infinibranch/)).
- **~93% of pages stay shared.** Pre-execution private overhead ~265 KB/fork; a `print`-only workload
  ~3.5 MB; a numpy-warm workload ~1.75 MB private/dirty per fork, while a heavier workload runs ~27 MB
  — so 100 numpy forks share one read-only ~2.4 GB warm-parent RAM image and add only ~1.75 MB each
  (~175 MB total of private pages).
- The OSS reference implementation reports spawning 100 children in ~101 ms (~1 ms/child), live
  BRANCH ~150 ms, ~0.12 MiB CoW metadata/child, and **~50 idle-pooled agents per 8 GB host**
  ([forkd](https://github.com/deeplethe/forkd)).
- A heavier per-sandbox baseline (no CoW-fork page sharing) is ~128 MB/sandbox with ~150 ms restore
  ([E2B persistence](https://e2b.dev/docs/sandbox/persistence)).

The scheduler must therefore **bin-pack on measured private RSS per fork, not on snapshot or image
size**. The ~93% shared-page advantage erodes as a workload writes memory, so density projections
that assume it are optimistic. We model three density bands and let §5 measure the truth:

| Density band (private RSS/fork) | Forks per 256 GB host (½ RAM for OS/cache/headroom) | Basis |
|---------------------------------|---------------------------------------------------:|-------|
| Aggressive (~16 MB, numpy-class) | ~8,000 | CoW-fork extrapolation *(unverified)* |
| **Plausible desktop (~128 MB)**  | **~1,000** | per-sandbox baseline *(unverified)*, no CoW credit |
| Conservative (~256 MB, browser-heavy) | ~500 | safe planning floor |

> **Planning rule (Shinken-set, not vendor):** until §5 measures it, **bin-pack and admission-control
> on the conservative band (~500 desktop forks / 256 GB host)** and treat anything better as upside.

The honest reading: the sub-millisecond-fork and single-digit-MB-per-fork numbers come from
short-lived, single-vCPU, compute-only serverless workloads. A **headful agent desktop** with a
browser, an X server, and an accessibility-tree extractor running is a different animal — its working
set is larger and drifts. **Realistic per-fork private RSS is the single most important unmeasured
number in this document**, because every density and `$/sandbox-hour` figure derives from it.

A second hard constraint: **fast-fork does not extend to the GPU, Windows, or macOS tiers.** VFIO/vGPU
device state is non-migratable and non-snapshottable
([VFIO snapshot blocked](https://forum.proxmox.com/threads/cannot-snapshot-vm-vfio-migration-not-supported.179190/)),
so GPU guests are longer-lived, recycled on a TTL, and reset at the filesystem/application layer.
Windows and macOS lack a Firecracker/CRIU-class live-fork equivalent and use disk-snapshot +
deterministic event-replay instead. The economics of those tiers (§3) are dominated by *standing*
capacity, not elastic forking.

### 1.4 $/sandbox-hour

`$/sandbox-hour` is the unit operators and the eval team actually feel. It decomposes into four
lines; only the first is fork-tier-specific:

```
$/sandbox-hour = compute_share + egress_share + storage_share + control_plane_share
```

- **compute_share** = (host $/hr) ÷ (forks/host). With a ~$3.50/hr 256 GB CPU host *(vendor list
  price, unverified)* and the conservative 500-fork band: `$3.50 / 500 ≈ $0.007/sandbox-hr`. At the
  plausible 1,000-fork band: `~$0.0035/sandbox-hr`. Idle-suspend (**D9**) drops idle sandboxes to
  ~storage-only cost.
- **egress_share** = from §1.1: structured blend ~6.5 GB/mo ÷ 730 hr ≈ 9 MB/hr → **<$0.001/hr** at
  list egress; a Tier-2 video escalation adds ~1.5 GB/hr → **~$0.10–0.14/hr while a human watches**.
  *Watching is the expensive verb.*
- **storage_share** = snapshot/`.skn` replay storage, amortized; content-addressed + CoW-deduped
  (**D5**). See §1.5.
- **control_plane_share** = Action Gateway + Fleet Manager + telemetry, amortized per active sandbox;
  small at scale, fixed-cost-heavy at low scale.

The headline: **a CPU-only Linux fork desktop, running structured-default and idle-suspended, costs
on the order of low-single-digit cents per sandbox-hour** in compute+egress — and the dominant
controllable levers are *not letting it idle uncompressed* (**D9**) and *not lighting Tier-2 video
unless a human is attached* (**D4**). A **GPU** sandbox is 1–2 orders of magnitude more expensive per
hour (§3) and is therefore opt-in by design (**D11**).

### 1.5 Replay GB/hr

The `.skn` event stream is the replay log (**D5**), and it is small. Its cost tracks the *streaming*
cost because **the structured event stream IS the replay log** — we are not paying twice.

| Replay component | rate | basis *(vendor-published, unverified)* |
|------------------|------|----------------------------------------|
| Structured events (Tier-0) active | ~16–80 kbps → **~7–36 MB/hr** | a11y-tree diffs at agent cadence |
| Structured events idle | ~5 kbps → **~2.2 MB/hr** | diff + change-trigger + 15 s cooldown |
| Browser DOM-mutation track (rrweb) | ~5–13 kbps → **~2–6 MB/hr** | web targets only |
| Recorded Tier-2 video (fMP4) | AV1-SCC normal ~45 MB/hr → H.264 office ~1.35 GB/hr | only when a human watched |
| Checkpoint-DAG snapshot refs | pointers only; bytes live in the dedup'd DAG | CoW, near-zero marginal |

So a **structured-only** replay hour is **~2–36 MB/hr** — a year of 100k concurrent sessions stored
at the structured rate is plausible-to-cheap. A replay hour **with full video captured** is 5–60×
larger. This is the storage-side mirror of the egress argument: record structure always, record
pixels only on escalation, and let the checkpoint DAG dedup the snapshot bytes (4 KiB-page diff
dedup, so a branch costs only its divergent pages — the same CoW economics as the live fork).
Retention *policy*, not capture rate, governs replay storage cost: a 30-day-hot / cold-archive tier
on structured-only sessions is negligible; a keep-all-video policy is not, and must be a per-tenant
budget knob. **All rates above are vendor-extrapolated and must be measured (§5).**

---

## 2. Build-vs-buy: substrate, broker, pixel channel

The discipline here is to **buy commodity, build differentiator** (**D12**). Three layers are
commodities with strong public options; three layers are the product and must be built.

### 2.1 Substrate (Linux fast-fork + container fast-path)

The substrate is a *strategy*, not a single binary (**D1**): the SDK exposes one
snapshot/fork/branch/restore contract and the per-OS, per-tier backend swaps beneath it. The
build-vs-buy question is whether to run that fleet in-house on open VMMs or rent it from a sandbox
vendor.

**Public substrate options compared** *(all timings vendor-published, unverified)*

| Option | What it is | Fork / branch | Self-host | Cross-OS | GPU | Fit for Shinken |
|--------|-----------|--------------:|:---------:|:--------:|:---:|-----------------|
| **In-house Firecracker fleet** | Own VMM-per-microVM on KVM + control plane | sub-ms fork; restore 5–30 ms (VMM only) | yes (you build it) | Linux only | none (no PCIe/VFIO) | **Core of the v1 Linux fork tier.** Max density (thousands/host), tiny device-model footprint, full control of the fork primitive. Cost = you operate it. |
| **In-house Cloud Hypervisor fleet** | rust-vmm VMM with display/VFIO/Windows | snapshot exists but excludes VFIO state (fragile) | yes | Linux + Windows | VFIO passthrough | **The desktop/Windows/GPU tier.** Firecracker has no display/GPU; CLH/QEMU fill the gap. No reliable GPU-VM fast-fork. |
| **E2B** | OSS Firecracker SaaS + open infra | restore ~28–150 ms; pause ~4 s/GiB | yes (run the OSS infra) | Linux/X11 | none | **The blueprint to copy, not necessarily rent.** Clean `create/connect/auto-pause` lifecycle, UFFD lazy memory, 4 KiB diff dedup. Streaming is raw VNC — the gap we beat. |
| **Morph** | Commercial CoW live-fork ("Infinibranch") | fork P99 ~1.3 ms; snapshot/branch/restore <250 ms | no (hosted) | Linux | none | **The fork numbers to match.** Owns the live-branch primitive; smaller vendor, less hyperscale-proven. Rent to prototype branching UX; not the self-host answer. |
| **Daytona** | Commercial sandbox + CoW fork tree | create ~90 ms; CoW fork with lineage tree | partial | Linux | varies | Fork-tree lineage is a good reference for the checkpoint DAG (**D5**). |
| **trycua/cua** | OSS cross-platform CUA runtime | per-backend | yes | Linux/macOS/Windows/Android | per-backend | **Closest analog; the layering to adopt** (`Image→Runtime→Transport→Interfaces→Sandbox`). Its macOS path uses Apple Virtualization.framework — the model for our macOS tier. |
| **`kubernetes-sigs/agent-sandbox` CRD** | OSS K8s CRD: Sandbox / Template / Claim / WarmPool on gVisor/Kata | container-class, pre-warmed pools | yes | Linux | via Kata/MIG | **The container fast-path and Fleet-Manager shape (D9).** Standard K8s agent-sandbox pattern with pre-warmed pools. |
| **Kata + Firecracker** | OCI/CRI runtime wrapping a guest kernel | follows backend VMM | yes | Linux | one-GPU-per-pod VFIO | The bridge: K8s-native isolation with a real microVM boundary and clean GPU passthrough. |

**The call.** Build the **in-house Firecracker fork tier** as the v1 default — it is the only way to
own the sub-millisecond fork primitive that is the product's headline, and the OSS control-plane
shape (E2B-style orchestrator over per-host VMM agents, Dockerfile→rootfs+memory-snapshot templates,
UFFD lazy restore) is well-trodden. Run the **container fast-path on the OSS
`kubernetes-sigs/agent-sandbox` CRD** (gVisor/Kata runtime classes, pre-warmed pools) for the Fleet
Manager shape (**D9**). Use **Cloud Hypervisor/QEMU** for the desktop/Windows/GPU tier, and **Kata +
Firecracker** where K8s-native isolation with a real microVM boundary is wanted. Treat **Morph and
Daytona as prototyping rentals and as the benchmark bar**, not as the production substrate — renting
the substrate forfeits control of the fork primitive and the cross-platform federation that is a core
differentiator. Adopt **trycua/cua's layering** and its Apple Virtualization.framework pattern for
macOS.

Why not just rent E2B / Morph / Daytona wholesale? Because they are Linux-only sandboxes that expose
snapshot/restore as a low-level API, not as a forkable replay *product*, and they leave the
cross-platform story (Windows/macOS/GPU), the structured streaming, the Capability Manager, and the
replay timeline entirely to the consumer. Renting them would buy table stakes and forfeit every
differentiator. The honest counter-argument: operating an in-house microVM fleet is real, ongoing SRE
cost, and a small team should start by *renting* to validate product-market fit before committing to
self-hosting. The roadmap (06) phases exactly that way — local single-VM → rented fork tier →
self-hosted fleet.

### 2.2 Secret broker

The model must never see plaintext credentials (**D6**). The broker injects secrets at the egress
proxy (header injection) or hands the guest a scoped, short-lived token; the agent loop, running
outside the sandbox behind the `tool_runner` policy boundary, references secrets by handle.

| Option | What it is | Fit |
|--------|-----------|-----|
| **HashiCorp Vault** | Secrets engine + dynamic short-lived creds + audit log | **Primary.** Mature, self-hostable; dynamic-secret lease model fits per-session credential issuance; recent releases add SPIFFE-based workload auth. |
| **Cloud KMS / Secrets Manager** | Managed key/secret store | Acceptable substitute where a cloud is already committed; couples you to that cloud. |
| **SPIFFE/SPIRE** | Workload identity (SVIDs) | Pairs with Vault for per-sandbox workload identity; the issuer of the handle the broker honors. |

**The call:** **HashiCorp Vault** (or any cloud KMS / SPIFFE-SPIRE) as the broker, wired to the
out-of-VM egress proxy for header-injection so the credential is added to the outbound request
*after* it leaves the sandbox. This is pure buy — there is no reason to build a secrets store.

### 2.3 Pixel channel: NICE DCV vs custom WebRTC+NVENC

The on-demand pixel channel (**D4**, **D11**) is the one build-vs-buy where the answer is "build, but
keep buy as a credible fallback."

| Dimension | **NICE DCV (buy)** | **Custom WebRTC + NVENC (build)** |
|-----------|--------------------|-----------------------------------|
| What it is | Public NVIDIA/AWS remote-display product: NVENC + QUIC/UDP, browser client, auto-adaptive bitrate | Single-PeerConnection WebRTC, media track = NVENC H.264/AV1-SCC, reliable data channel = structured stream |
| Maturity | Production, battle-tested at 4K/60 | Must build; OSS GStreamer `nvcodec`→`webrtcbin` patterns exist |
| Integration with structured channel | Separate product; the data channel and replay log are *ours* either way | **Native** — the same PeerConnection carries the replay-log data channel and the media track |
| Codec | NVENC H.264/HEVC/AV1 on qualified GPUs | NVENC H.264/AV1-SCC, screen-content tuned |
| Control | Vendor roadmap | Full control of tiering, ROI, SoM overlay, dirty-rect routing |
| Time-to-first-pixel | Fast (adopt wholesale) | Slower (build) |

**The call:** build the **custom WebRTC+NVENC** path as the primary, because the entire economic
argument depends on a *single* PeerConnection where the structured replay-log data channel and the
on-demand media track are negotiated together and metered together — a separate pixel product cannot
own the tier-escalation logic, the Set-of-Marks overlay, or the dirty-rect routing that keeps Tier-1
cheap and Tier-2 sharp. Keep **NICE DCV as the credible buy-fallback** for the high-fidelity
4K/premium pixel path and for the optional GPU tier, since it is a public product that already
implements the NVENC+QUIC pattern on qualified GPUs
([NICE DCV](https://docs.aws.amazon.com/dcv/latest/userguide/using-streaming.html)). The encode
hardware constraint is fixed in **D11**: the encode tier runs on **Ada L4** (density) / **L40S**
(4K/AV1+render), **never on A100/H100/H200/B200, which have zero NVENC engines** (public NVIDIA fact —
[NVENC application note](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html));
the 8-session consumer cap does not apply to qualified datacenter GPUs, so encode density is bounded
by encoder throughput, not licensing.

### 2.4 What we build (the non-commodity layers)

The substrate, broker, and (optionally) the pixel codec are commodities. **The product is the layers
none of the options above ship:** the typed versioned **ACI** (**D2**), the **structured-default
streaming protocol** and tier-escalation logic (**D3**/**D4**), the **event-sourced forkable replay**
and `.skn` bundle (**D5**), the **Sandbox Capability Manager** (**D6**), and the **eval
layer** on the same runtime (**D7**). Every buy-vs-build above resolves to "buy the substrate so we
can spend our engineering on these."

### 2.5 Decision summary

| Layer | Decision | Why |
|-------|----------|-----|
| Linux fork substrate | **BUILD** (in-house Firecracker) on the OSS control-plane shape | Owns the sub-ms fork primitive — the headline differentiator; rent (Morph/Daytona) only to prototype |
| Container fast-path | **BUY/ADOPT** (`kubernetes-sigs/agent-sandbox` CRD on gVisor/Kata) | Standard K8s agent-sandbox pattern; the Fleet-Manager CRD shape (D9) |
| Desktop/Windows/GPU VMM | **BUY/ADOPT** (Cloud Hypervisor/QEMU; Kata+Firecracker) | Firecracker has no display/GPU; CLH/QEMU/Kata fill the gap |
| macOS tier | **BUY/ADOPT** (Apple Virtualization.framework, cua/lume pattern) | Legally bound to Apple silicon; 2-VM/host cap |
| Secret broker | **BUY** (HashiCorp Vault / cloud KMS / SPIFFE-SPIRE) | No reason to build a secrets store; dynamic-lease model fits per-session creds |
| Pixel channel | **BUILD** custom WebRTC+NVENC; **BUY** NICE DCV as fallback | Single-PeerConnection co-negotiation of replay-log + media is ours; DCV is the public 4K/premium fallback |
| ACI / streaming / replay / permission / eval | **BUILD** | The product. No option above ships these |

---

## 3. Tier economics: where the money actually goes

The fleet is not one tier; the cost mix differs sharply by isolation tier (**D1**/**D11**).

```
                         cost driver           density       fast-fork
  Linux fork tier  ──►   warm-pool RAM         100s/host     YES (sub-ms)
  Container tier   ──►   pod scheduling        100s/node     warm-pool
  GPU tier         ──►   GPU $ + vGPU license  ~24–32/card   NO
  Windows tier     ──►   OS licensing          10s/host      NO (snapshot-light)
  macOS tier       ──►   Apple-HW scarcity     2/host        NO (clone-only)
```

- **Linux fork tier** is cheap and elastic: dominated by warm-pool carrying cost (§5) and the
  structured-egress floor. This is where 80%+ of CPU-only agent/browser/code tasks should run.
- **GPU tier** is the expensive, non-elastic minority. The fork-density economics of §1.3 **do not
  apply** because VFIO/vGPU device state cannot be snapshotted (**D1**/**D11**). Density is bounded by
  **frame buffer first, then a context-switch ceiling**: a 48 GB card holds ~24–48 light desktops by
  VRAM but a 48 GB card is documented to cap near **~32 concurrent vGPU users** by context-switching,
  not memory ([A40](https://www.nvidia.com/en-us/data-center/a40/)). vGPU is a **paid
  per-concurrent-user license** that can dominate GPU TCO. Run two pools (**D11**): **time-sliced vGPU
  on L4/L40S** for density, **MIG-backed / Confidential Containers on A100/H100/B200** for
  isolation-sensitive/trusted tenants. A GPU desktop's $/sandbox-hour ≈ (card $/hr) ÷ (~24–32
  sessions) with **no idle-suspend discount** (can't snapshot device state) — 1–2 orders of magnitude
  above the CPU fork tier, which is why GPU is opt-in.
- **Windows tier** is licensing-gated and snapshot-light: cost is per-physical-core OS licensing plus
  longer-lived instances. Plan as standing capacity, not elastic forking.
- **macOS tier** is a scarce premium: **2 VMs per physical Mac**, Apple-hardware-only, no fast reset.
  Price it as a low-density standing pool and pass the scarcity through.

The takeaway for capacity planning: **route by need.** GPU/Windows/macOS are premium tiers billed
near their marginal cost; the Linux fork tier is where the platform's cheap, high-concurrency
economics live, and the architecture pushes everything there by default.

---

## 4. Warm-pool sizing math

Cold-fork latency, even at sub-second, is too slow for a session-create on the request path under
load, and forks must be pre-staged from a *warm* parent. The Fleet Manager (**D9**) holds three
nested pools per (image × region × tier): a **warm-parent pool** (the snapshotted golden VMs that
forks map against), a **ready-fork pool** (forks pre-spawned and waiting to be claimed), and a
**cold-replenish budget** (the rate at which the ready pool is topped up).

### 4.1 Ready-fork pool depth

Size the ready pool as a queue served by fork creation against an arrival process. With Poisson
arrivals at rate `λ` (sessions/s), mean ready-fork hold time `T_provision` (time to spawn and ready a
fresh fork: fork + post-fork uniqueness hook + readiness probe), and target stockout probability `ε`,
the steady-state ready depth is governed by Little's law plus a safety buffer:

```
ready_depth ≈ λ × T_provision  +  z(ε) × sqrt(λ × T_provision)
```

where `z(ε)` is the normal quantile for the desired no-stockout confidence (`z(0.99) ≈ 2.33`). Worked
example, vendor-published fork numbers:

- `λ = 50` sessions/s, `T_provision = 0.3 s` (fork ~1 ms + uniqueness reseed + readiness probe,
  conservatively rounded up), `ε = 1%`:
  `ready_depth = 50 × 0.3 + 2.33 × sqrt(50 × 0.3) ≈ 15 + 9 ≈ 24` ready forks.

So even at 50 new sessions/s, a **ready pool of ~24 forks** per (image×region) absorbs the burst with
99% no-stockout. Because each idle fork costs only its private RSS (single-digit MB for light
workloads; *measure* for desktops — §1.3), the ready pool is cheap RAM, and **over-provisioning the
ready pool is the right default**: it converts a tail-latency risk into a small, predictable RAM line.

### 4.2 Warm-parent pool depth and the per-tier asymmetry

The warm-parent pool is sized by *fan-out limits and image diversity*, not arrival rate: each parent
backs many forks (CoW), but a single parent is a single-point-of-failure and a snapshot-chain-depth concern
(repeated branching regresses without compaction — the OSS reference saw branching go from ~150 ms to
~2.7 s by the 6th un-compacted branch). Hold **≥2 warm parents per (image×region)** for availability,
scale parents with the number of distinct golden images, and **flatten/compact CoW chains
periodically** so deep replay trees do not degrade fork latency.

The asymmetry between tiers is the whole point of the economics:

| Tier | λ (sess/s) | T_provision | warm-held target | Why |
|------|-----------:|------------:|------------------|-----|
| **Linux fork (D1)** | 50 | ~0.3–1 s | **thin ready pool (~24) + ≥2 parents** | fork is ms-cheap → hold few, replenish fast |
| **Windows (D1)** | ~1 | ~60 s (no fast fork) | **~standing pool sized to concurrency + burst** | snapshot-light, slow boot → the pool *is* the capacity |
| **macOS (D1)** | ~0.05 | ~120 s; **2 VMs/host cap** | **standing pool by host count** | Apple-HW-only, hard density cap |
| **GPU vGPU (D11)** | ~0.5 | ~90 s (no device snapshot) | **~standing reservation** | can't fast-fork → pay for standing idle |

**Linux fork tier:** because `T_provision` is small and CoW makes each warm parent cheap, you hold a
*thin* warm pool and lean on fast replenishment — warm cost is small. **Windows/macOS/GPU tiers:**
because there is no fast fork, the pool *is* the capacity, so you pay for standing idle instances and
the sizing collapses to "expected concurrent sessions, plus burst." This is the cost reason the canon
(**D1**/**D10**) treats Linux as first-class and Windows/macOS/GPU as heavier, scarcer tiers.

### 4.3 Idle economics dominate — auto-suspend is the lever

The dual-timer session model (**D9**) is the real cost control: idle sessions reset on ~15 min of
inactivity and **auto-suspend to snapshot** on idle, with a ~4–8 h max lifetime. Idle dominates cost
in any always-on fleet, so suspended-to-snapshot sessions should cost ~zero compute and only snapshot
storage; resume is fast (resume-from-standby on the order of tens of ms to ~1 s, vendor-published).
The warm-pool target is therefore: *enough ready forks to never block a claim, parents flattened to
keep fork latency flat, and aggressive suspend-to-snapshot so the active footprint tracks actual
demand, not provisioned ceiling.* **Idle suppression on the wire (Tier-0 diffs + cooldown, D4) and
idle suspension of compute (D9) are the same insight applied to bandwidth and to RAM.**

```
   request ──► [ ready-fork pool ] ──claim──► active session
                     ▲                              │ idle 15m
            fork from │                             ▼
          [ warm-parent pool ] ◄─snapshot─ [ suspended-to-snapshot ]
                     ▲                         (≈ zero compute)
        boot+snapshot│ (cold replenish: λ × T_provision/s)
          [ golden image / template ]
```

---

## 5. First-party measurement plan

Every speed, density, and cost figure above is **(vendor-published, unverified)** unless we generated
it. The platform's entire economic case rests on a handful of load-bearing assumptions that *we have
not measured*. No figure in this doc may enter a capacity plan, a pricing sheet, or a customer SLA
until it has been measured on Shinken's own substrate. This section is that plan — a deliverable, not
an aspiration, and it gates the v1 capacity plan.

### 5.1 Principle: measure on representative agent workloads, not microbenchmarks

The load-bearing risk is that the **blend rate** (how much of a real session is structured vs
forced-to-pixels) and the **a11y coverage** on Electron/Qt/canvas/games are unknown. A synthetic
"send a JSON diff" benchmark will look great and tell us nothing. The measurement corpus must be
**real agent trajectories** on the conformance suites Shinken already targets (**D7**):
OSWorld-Verified, WindowsAgentArena, WebArena/VisualWebArena, plus a deliberately a11y-poor set
(Electron apps, `<canvas>`/WebGL, a game). Capture rides the `.skn` replay bundle (**D5**) so
measurement is a byproduct of normal operation, not a separate harness.

### 5.2 Metrics, method, and targets

| # | Metric | Method | Target (retires which `(unverified)` claim) |
|---|--------|--------|---------------------------------------------|
| **M1** | **Tier-0 structured bitrate** (active & idle), p50/p95 | Bytes/s of `events.jsonl` over real OSWorld/WebArena sessions, by app class | Active ≤ **80 kbps**, idle ≤ **5 kbps**, blend ≈ **10–30 kbps** → validates the ~150× / $36k-vs-$4.86M egress claim (§1.1) |
| **M2** | **Pixel-escalation rate (the blend)** | Fraction of session wall-time + screen-area forced to Tier-1/Tier-2 because a11y/DOM coverage failed or churn was too high; per app class | Pixel-time ≤ **10–20%** on instrumented apps; record the worst-case a11y-poor rate explicitly → validates §1.1/§1.5 |
| **M3** | **a11y/DOM coverage** | % of interactive elements with usable role/name/bbox vs ground-truth, per toolkit (GTK/Qt/Electron/Chromium/Win UIA/macOS AX) | Coverage ≥ **90%** on standard toolkits; quantify the Electron/canvas gap as a number, not a hand-wave |
| **M4** | **Fork latency & time-to-first-action** | Wall-clock fork request → ready → first ACI ack, p50/p95/p99, with/without working-set prefetch | Fork p99 ≤ **30 ms** VMM; **time-to-first-action ≤ 1 s** → validates §4 `T_provision` and the D1 sub-second target |
| **M5** | **Private dirty RSS per fork** | Measure CoW dirtied pages per desktop fork at t=0/1min/10min, for idle browser / active browser / office-doc | Establish the real density band (§1.3); if ≤128 MB the plausible band holds, if >256 MB revise packing down |
| **M6** | **Forks per host (effective density)** | Pack forks until p95 time-to-first-action SLA breaks or memory pressure triggers — the real bin-packing number | Beat the conservative **500 / 256 GB** floor (§1.3); set the production admission-control ceiling here |
| **M7** | **$/sandbox-hour (CPU fork)** | Sum measured compute (M6) + egress (M1) + storage (M8) + control-plane amortization over a fleet-week | Establish the real unit cost; replace the "low-single-digit cents" estimate (§1.4) |
| **M8** | **Replay GB/hr & dedup ratio** | Bytes to `.skn` per session-hour, structured-only vs with-media; checkpoint-DAG dedup ratio across a branch tree | Structured-only ≤ **36 MB/hr**; measure media multiplier; quantify CoW snapshot dedup → validates §1.5 |
| **M9** | **GPU session density & $/hr** | Pack light desktops onto L40S/L4 under Equal/Fixed-Share vGPU until interactive-latency SLA breaks; measure NVENC engine utilization | Confirm/refute the ~24–32/card ceiling and "NVENC not the bottleneck" (§3); set GPU premium pricing |
| **M10** | **TURN/SFU egress per attached reviewer** | GB/reviewer-hour + TURN-relay fraction when humans attach Tier-2 video; SFU vCPU per simulcast stream | Validate/replace ~1.5 GB/participant-hr + SFU-cost assumptions (§1.2) behind the "watching is expensive" rule |
| **M11** | **Idle-suspend economics** | Resume-from-suspend p50/p95; suspended-snapshot storage cost vs resident RSS; suspend/resume churn cost | Confirm resume < ~1 s and suspended cost ≈ storage-only → validates the D9 idle-suspend claim (§4.3) |

### 5.3 Method standards (so the numbers are trustworthy)

- **Telemetry native, not bolt-on.** All of M1–M11 derive from OTel-GenAI traces (**D5**/**D9**) and
  the `.skn` event log — measurement is the same data path as production observability.
- **Report distributions, not means.** p50/p95/p99 for every latency and bitrate metric; agent
  desktops are bursty and the mean lies.
- **Stratify by app class.** Blend rate and a11y coverage (M2/M3) vary wildly between a web form and an
  Electron app — a single fleet-wide average hides the failure mode that matters.
- **Date and version everything.** Tie each measurement to substrate version, guest image, and ACI
  version; re-measure on substrate upgrades.
- **Publish the comparison.** Record each measured number next to the vendor figure it replaces, so we
  can see whether reality beat or missed the vendor claim — and recalibrate every other estimate.

### 5.4 Exit criterion

This doc's §§1–4 are **provisional** until M1–M8 (the CPU-tier core) report. When they do: strike the
`(vendor-published, unverified)` labels on the lines they cover, replace the placeholder tables with
measured distributions, and only then promote any figure into a capacity plan or SLA. M9–M11 gate
GPU-tier and reviewer-egress pricing on the same terms. **Until then, every number here is a
hypothesis with a citation, not a commitment.** The measurement spike is a named milestone in
[`06-roadmap.md`](../engineering/roadmap.md).

---

## 6. How this reconciles to the decisions

| Decision | Where it lands in the economics |
|----------|---------------------------------|
| **D1** Tiered, substrate-pluggable, fork-from-snapshot | §1.3 density-by-private-RSS; §2.1 build in-house Firecracker; §3 per-tier cost mix |
| **D4** Structured-default, pixels-on-demand, dual-channel WebRTC | §1.1 egress table (~150× / ~$58M/yr win); §1.2 relay cost; §2.3 custom WebRTC+NVENC |
| **D9** Fleet Manager + dual-timer sessions + OTel | §4 warm-pool math; §4.3 auto-suspend as the cost lever; §5.3 OTel instrumentation |
| **D11** GPU opt-in; encode on Ada L4/L40S; vGPU + MIG | §2.3 NICE-DCV-vs-custom; §3 GPU tier density/license economics |
| **D12** Buy commodity, build differentiator | §2 and §2.5 — buy substrate/broker/codec, build ACI/streaming/replay/permission/eval |

The bottom line for anyone budgeting Shinken: **at the structured default, an agent desktop costs
fractions of a cent per hour in compute and egress; the real money is warm-pool RAM, replay retention
policy, and the premium GPU/Windows/macOS tiers.** That conclusion holds across a wide range of the
unverified inputs — but the *magnitude* of the win, and the achievable density, both hinge on the §5
measurements. Do those first.
