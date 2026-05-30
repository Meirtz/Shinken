# Shinken — Technical Decisions (ADRs)

> The keystone document. Each Architecture Decision Record (ADR) below corresponds to one
> of the twelve decisions D1–D12 that define Shinken. Every ADR follows one template:
> **Title · Status · Context · Decision · Alternatives (and why rejected) ·
> Consequences (positive / negative / risks) · Evidence**.
>
> Shinken is a public, vendor-neutral open-source project: an AI-native, cross-platform
> **sandbox runtime + control plane + control panel** for computer-use agents — a
> streaming-first successor to OSWorld. NVIDIA GPUs are a *supported, optimized acceleration
> option*, never a dependency.
>
> **Status legend:** *Accepted* = committed for v1; *Accepted (phased)* = committed but
> staged across the roadmap; *Provisional* = the right call on today's evidence, gated on a
> first-party measurement spike. **Every speed / density / cost number sourced from a vendor
> blog or spec sheet is marked "(vendor-published, unverified)"; a first-party measurement
> plan is required before any of them anchor an SLA.** Today's date: **2026-05-30**.
>
> Companion documents: [`02-architecture.md`](02-architecture.md),
> [`04-landscape.md`](04-landscape.md), [`08-threat-model.md`](08-threat-model.md),
> [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md). Full source list:
> [`../notes/sources.md`](../notes/sources.md).

---

## Decision map

| ADR | Decision | Status | One-line stance |
|-----|----------|--------|-----------------|
| **D1** | Isolation = tiered, substrate-pluggable, routed by `(OS × needs-GPU × needs-fast-fork)` | Accepted (phased) | Firecracker for headless Linux fork; QEMU-microvm/crosvm for Linux desktop; CLH/QEMU+VFIO for GPU/Windows; Apple VZ for macOS |
| **D2** | ACI action schema = one typed tagged-union with version-pinned adapters | Accepted | ~16 verbs, `target` oneof, code-as-action off by default behind the policy boundary |
| **D3** | Observation = structured-first, layered escalation | Accepted | a11y/DOM diff → Set-of-Marks → region pixels → full frame; act on refs by default |
| **D4** | Streaming = single-PeerConnection WebRTC, dual-transport | Accepted | Reliable data channel = event stream (= the replay log); on-demand media track |
| **D5** | Replay = event stream + bisected snapshots; `.skn` bundle | Accepted | Append-only `events.jsonl` + immutable checkpoint DAG; reset and branch are one primitive |
| **D6** | Sandbox capabilities = entitlement provisioning + boundary enforcement (Cedar + ocap + OS) | Accepted | Sandboxes can do real work inside the boundary; Cedar/ocap/OS control the capabilities and boundary crossings |
| **D7** | Eval layer = thin orchestration on the runtime, inverting OSWorld | Accepted (phased) | Typed verifier DAG, golden snapshot per task, N≥5 forked replicas, readiness probes |
| **D8** | Interfaces = native streaming SDK core + optional MCP facade | Accepted | One IDL → py/ts SDKs; MCP facade at two altitudes; never the hot loop over MCP |
| **D9** | Control plane = Fleet Manager + Action Gateway | Accepted | Warm pools + fork-on-demand; single auth→rate→budget→policy chokepoint; dual-timer sessions |
| **D10** | Cross-platform = one control plane, one Guest Runtime contract, one ACI | Accepted (phased) | Linux first-class v1; Windows + macOS heavier v1 tiers; Android roadmap |
| **D11** | GPU = optional acceleration tier | Provisional | Opt-in; encode **never on A100/H100**; vGPU (density) + MIG/CoCo (trusted); NICE DCV is the build-vs-buy fork |
| **D12** | Business = open self-hostable core + optional hosted commercial layer | Accepted | No lock-in; replay-as-training-data wedge; optimized for NVIDIA where present |

---

## D1 — Isolation: tiered, substrate-pluggable, routed by `(OS × needs-GPU × needs-fast-fork)`

**Status:** Accepted (phased). The Linux fork tier is v1; the GPU and cross-OS tiers ship as heavier, longer-lived tiers and grow across the roadmap. The "no single VMM" finding is firm; per-tier VMM selection (notably crosvm vs QEMU-microvm for the desktop tier) is gated on a first-party PoC.

### Context

The defining capability of an agent/eval platform versus OSWorld's VMware/VirtualBox full snapshot-revert (seconds-to-minutes, I/O-bound on disk-delta size) is **fast fork/clone via copy-on-write of both memory and disk**. But the substrate that delivers sub-millisecond forks does not also give you a GPU, a desktop display, Windows, or macOS. The research is unambiguous on the trade space:

- **Firecracker** ships exactly five virtio devices (net, block, vsock, serial console, i8042 keyboard controller) — **no virtio-gpu, no PCIe, no VFIO**. Its community GPU/PCIe initiative (launched Oct 2024) was **paused Feb 2026** for lack of resources, and even the planned MVP excluded GPU snapshotting. Firecracker is the headless-Linux king (~125 ms boot, <5 MiB overhead, 28–33 ms snapshot restore — vendor-published, unverified) and nothing else.
- **Cloud Hypervisor (CLH)** has production VFIO GPU passthrough and Windows guests, but **no mainline virtio-gpu** and a snapshot/restore path that is experimental **and mutually exclusive with VFIO devices**. A GPU-passthrough VM cannot be snapshotted or CoW-forked.
- **QEMU** (incl. the Firecracker-inspired `microvm` machine type) is the only VMM with a first-class in-tree virtio-gpu *and* full VFIO + vGPU — the right base for a snapshot-friendly Linux *desktop*. **crosvm** is the dark horse: a real "microVM with a GPU" (virtio-gpu + gfxstream + Wayland), already the substrate under Cuttlefish and ChromeOS.
- **macOS** can only legally run on Apple-branded hardware via **Apple Virtualization.framework**, hard-capped at **2 concurrent macOS VMs per physical Mac** (`VZErrorDomain` code 6 on the third), arm64-only, no nested virt, no Apple-ID/iCloud/App-Store in-guest.
- **Windows** is a licensing problem more than a tech problem: full VMs from sysprep golden images scale, Windows Sandbox is one-instance-per-host (dev tier only), and Windows-Server-Datacenter-per-core / Win11 Multitenant Hosting Rights gate density.

The decisive insight: **a Linux desktop does not require GPU passthrough.** A desktop needs a *surface*, not a physical GPU. Run a headless compositor (Xorg-dummy/Xvfb or wlroots-headless) and render via Mesa software (llvmpipe/lavapipe) or virtio-gpu virgl, then pixel-stream the framebuffer out. This keeps desktops snapshot-forkable because there is no VFIO state to lose.

### Decision

Build a **substrate router** in the control plane that selects a virtualization backend per request keyed on **`(OS × needs-GPU × needs-fast-fork)`**, and expose `pause / resume / fork / branch / restore` **only where the substrate supports them** — surfacing "no fast-fork" as a first-class API property of the GPU tier.

```
                          Substrate Router  (OS × needs-GPU × needs-fast-fork)
                                      │
   ┌──────────────────┬──────────────┼───────────────────┬─────────────────────┐
   ▼                  ▼              ▼                   ▼                     ▼
 Linux headless     Linux desktop   GPU / accel         Windows               macOS
 (default v1)       (v1)            (opt-in, D11)       (v1, heavier)         (v1, scarce)
   │                  │              │                   │                     │
 Firecracker        QEMU-microvm    Cloud Hypervisor    CLH / QEMU            Apple
 microVM            OR crosvm       / QEMU + VFIO        + virtio-win         Virtualization
 (5 virtio devs)    (virtio-gpu +   or vGPU / MIG        + guest agent        .framework
   │                 SW / virgl)       │                  │                    (Apple HW only)
 fork-from-         fork-capable    NO fast snapshot     snapshot-light       APFS CoW clone;
 snapshot           (software       (VFIO state not      sysprep generalize   2 VMs / Mac cap
 (MAP_PRIVATE CoW    render keeps    in snapshots) →      per-clone unique     (TCC pre-grant)
  + userfaultfd +    it forkable)    longer-lived,        SID/host/keys
  warm parent pool)                  recycle on TTL
   │
 target <30 ms VMM restore; post-fork uniqueness hook
 (reseed CSPRNG/numpy, regen MAC/hostname/boot_id, rotate tokens)
```

Concrete tier definitions:

1. **Linux headless (default, v1).** Firecracker microVM. Reset = **fork-from-snapshot**: boot a golden microVM once with the stack warm, snapshot to an immutable `memory.bin` + `vmstate`, and spawn every task/branch as a new KVM VM mapping that file `MAP_PRIVATE`. Add a **REAP-style working-set record-and-prefetch** layer (the real restore cost is page faults, not VMM setup) plus a **userfaultfd** fallback for varying inputs. Target Morph/forkd class (P99 ~1.3 ms fork, ~93% shared pages — vendor-published, unverified). Bin-pack the scheduler on **private/dirty-page RSS**, not snapshot size.
2. **Linux desktop (v1).** QEMU-`microvm` (virtio-mmio + qboot + in-tree virtio-gpu) as the safe default; **crosvm** as a head-to-head PoC because virtio-gpu+Wayland is native and microVM-light. Software render (llvmpipe/lavapipe) keeps these forkable.
3. **GPU / accelerated (opt-in — see D11).** CLH or QEMU + VFIO passthrough or vGPU/MIG. **No fast snapshot**; longer-lived, recycle-on-TTL.
4. **Windows (v1, heavier).** CLH/QEMU + virtio-win + the in-guest Guest Runtime; one sysprep golden image per SKU; cloudbase-init first-boot; quiesced (VSS) snapshots. Licensing-gated.
5. **macOS (v1, scarce premium).** Apple Virtualization.framework on Apple hardware (Tart/lume-style, APFS CoW clone). Hard caps: Apple-HW-only, 2 VMs/host, TCC pre-grant. Treat as premium, scarce capacity — never commodity elasticity.
6. **Android (roadmap).** crosvm/Cuttlefish or redroid quick-boot snapshots.

**Instant reset and replay-branching are the SAME primitive** (fork a snapshot node — see D5). A mandatory **post-fork uniqueness hook** runs on every fork: enable VMGenID (kernel CSPRNG auto-reseed, Linux ≥5.18) *and* reseed userspace PRNGs, regenerate MAC/IP/hostname/`boot_id`, resync clock, delete saved random-seed files, rotate any TLS/session tokens. Resuming the same state twice without this is a crypto/security hole, not a correctness nit.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **One VMM for everything (e.g. QEMU q35 only)** | Possible (QEMU covers headless, desktop, Windows, GPU) but forfeits Firecracker-class fork speed on the high-volume headless tier and presents a large attack surface for hostile multi-tenant. Kept as a *unifier fallback* if operational surface must be cut; not the default. |
| **Firecracker for the desktop/GPU tier** | Dead end: no display device, no GPU; the GPU initiative is paused. Planning a roadmap on it is a trap. |
| **Cloud Hypervisor for the Linux desktop** | Its virtio-gpu exists only as unmaintained out-of-tree Spectrum-OS patches the maintainers will not upstream. Reserve CLH for GPU/Windows. |
| **OSWorld-style full snapshot-revert** | Seconds-to-minutes, I/O-bound on disk-delta size; structurally beaten by fork-from-snapshot. |
| **Containers (bare) as the isolation boundary** | "Your container is not a sandbox": shared host kernel/driver, not a boundary for hostile code. Containers run *under* gVisor/Kata or a microVM, never raw — see the OSS `kubernetes-sigs/agent-sandbox` CRD pattern in D9. |
| **GPU passthrough VM that also fast-forks** | Impossible today: VFIO device state is out of scope for snapshots on both CLH and QEMU. The fast-fork killer feature simply does not exist on the GPU tier. |

### Consequences

- **Positive:** the Morph/E2B fork-density advantage on Linux without betting the cross-OS or GPU story on a substrate that cannot deliver it; reset and branch fall out of one mechanism; uniqueness is handled by construction; the API tells callers honestly when fast-fork is unavailable.
- **Negative:** Shinken operates a **federated, per-OS fleet** (more operational surface than a single-substrate competitor); GPU/Windows/macOS tiers are heavier and lower-density; macOS density is physically bounded at 2 VMs/Mac.
- **Risks:** (1) restore latency is dominated by lazily faulting thousands of guest pages — measuring only "VMM restore <30 ms" ships a system that is hundreds of ms slow on first touch; mitigate with working-set prefetch + userfaultfd. (2) Forked VMs must keep their backing memory file alive for their whole lifetime, constraining snapshot GC. (3) overlayfs whole-file copy-up spikes latency on large-file writes — switch write-heavy guests to dm-thin/btrfs/ZFS. (4) The desktop-tier VMM choice (crosvm vs QEMU-microvm) and real fork density are unverified until the PoC.

### Evidence

- Firecracker snapshot + MAP_PRIVATE CoW restore, userfaultfd, VMGenID: <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md>, <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md>, <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/random-for-clones.md>
- Firecracker GPU/PCIe initiative paused: <https://github.com/firecracker-microvm/firecracker/discussions/4845>
- REAP working-set prefetch (ASPLOS'21): <https://marioskogias.github.io/docs/reap.pdf>; FaaSnap (EuroSys'22): <https://www.sysnet.ucsd.edu/~voelker/pubs/faasnap-eurosys22.pdf>
- Morph Infinibranch (sub-ms CoW fork): <https://www.morph.so/blog/infinibranch/>; forkd (OSS reference): <https://github.com/deeplethe/forkd>; Restoring Uniqueness in MicroVM Snapshots: <https://arxiv.org/abs/2102.12892>
- Cloud Hypervisor VFIO and snapshot-restore (VFIO out of scope): <https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/vfio.md>, <https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/snapshot_restore.md>
- QEMU `microvm` machine type and virtio-gpu: <https://www.qemu.org/docs/master/system/i386/microvm.html>, <https://qemu-project.gitlab.io/qemu/system/devices/virtio-gpu.html>; crosvm GPU: <https://crosvm.dev/book/devices/gpu.html>
- Apple SLA (Apple-HW-only, 2 instances): <https://www.apple.com/legal/sla/docs/macOSSequoia.pdf>; VZ 2-VM cap detail: <https://eclecticlight.co/2022/08/04/virtualisation-on-apple-silicon-macs-8-how-apple-limits-vms/>; Tart: <https://tart.run/quick-start/>
- Windows golden-image + licensing: <https://www.microsoft.com/licensing/guidance/Windows-Server-2025>, <https://www.microsoft.com/licensing/guidance/Windows-11-Licensing-for-Virtual-Desktops>, <https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/>

---

## D2 — ACI action schema: one typed tagged-union with version-pinned adapters

**Status:** Accepted.

### Context

Every model-vendor action grammar surveyed reduces to the same screenshot-in / discrete-action-out loop. OSWorld proves it empirically: it accepts **five** incompatible action representations (Anthropic tool schema, OpenAI CUA, UI-TARS DSL, its own `computer_13` JSON, raw pyautogui) and string-translates **all** of them down to one execution primitive — a pyautogui code string run via `python -c` over plain HTTP with FAILSAFE disabled and the sudo password in the prompt (RCE-by-design). The grammars differ mainly in (a) coordinate space, (b) verb granularity, and (c) how modifiers/buttons/scroll are expressed. Anthropic is schema-less (the grammar is trained into the model), uses absolute `[x,y]` pixels, and has three versions (`computer_20241022`, `computer_20250124`, `computer_20251124`); OpenAI uses `computer_call`/`computer_call_output` with an `actions[]` array, a `call_id`, and `pending_safety_checks`; UI-TARS uses a normalized 0–1000 coordinate DSL.

A platform that wants every off-the-shelf agent to drive it cannot pick one grammar, and cannot do what OSWorld did (lossy string translation into an RCE primitive).

### Decision

Define **one canonical typed tagged-union** as the wire schema, and make **version-pinned bidirectional adapters** the only model-facing surface.

- Discriminated by `verb` (~16 verbs: `click`, `double_click`, `move`, `drag`, `scroll`, `key`, `type`, `wait`, `screenshot`, `zoom`, …).
- `target = oneof{ point_px | point_norm | element_ref }`, with an explicit **`CoordinateSpace`** declared per observation so a coordinate never floats free of its frame.
- **Semver** versioning with **capability negotiation at handshake**: the Operator and Guest Runtime agree on a schema version and the set of supported verbs/capabilities before the session runs.
- **Adapters** translate, bidirectionally and *typed* (not stringly), to and from: Anthropic `computer_2024xxxx`/`2025xxxx` (+ `bash` + `text_editor`), OpenAI `computer_call`, UI-TARS DSL, OSWorld `computer_13`.
- **Code-as-action** (`exec`/`bash`/`edit`) is a separate, **off-by-default capability class** that routes through the **`tool_runner` policy boundary** (D6) — never the open RCE primitive OSWorld ships.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Adopt one vendor grammar verbatim** | Locks Shinken to one model ecosystem and one coordinate model; breaks every other agent. |
| **OSWorld-style string translation to pyautogui** | Lossy, untyped, and the execution primitive is RCE-by-design (FAILSAFE off, sudo in prompt). Unacceptable for a multi-tenant production runtime. |
| **Untyped JSON blob actions** | No capability negotiation, no coordinate-space safety, no replay-stable element refs; pushes correctness into every adapter. |
| **Code-as-action on by default** | Maximally powerful but turns every session into arbitrary code execution; gated behind a capability class and the policy boundary instead. |

### Consequences

- **Positive:** any off-the-shelf Claude or OpenAI agent drives Shinken through a thin adapter; the canonical schema stays small and high-affordance; element-ref targeting (D3) makes actions replay-stable; code-as-action is contained.
- **Negative:** adapters must be maintained per vendor schema version (Anthropic alone has three); capability negotiation adds handshake complexity.
- **Risks:** schema evolution requires an **event upcasting** strategy (carried as an open question — see [`../notes/open-questions.md`](../notes/open-questions.md)); a missing adapter for a new grammar blocks that model until written.

### Evidence

- Anthropic computer-use tool (versioned grammars, bash, text_editor): <https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/computer-use-tool>, <https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/text-editor-tool>
- OpenAI Computer Use (`computer_call`, `pending_safety_checks`): <https://developers.openai.com/api/docs/guides/tools-computer-use>
- OSWorld `computer_13` + RCE-by-design in-VM server: <https://github.com/xlang-ai/OSWorld/blob/main/mm_agents/prompts.py>, <https://github.com/xlang-ai/OSWorld/blob/main/desktop_env/server/main.py>
- UI-TARS normalized DSL: <https://github.com/bytedance/UI-TARS>
- SWE-agent (ACI design principle): <https://arxiv.org/abs/2405.15793>
- Detail in [`../notes/ai-native-interface.md`](../notes/ai-native-interface.md).

---

## D3 — Observation: structured-first, layered escalation

**Status:** Accepted.

### Context

The observation channel — not the action grammar — is the bandwidth, cost, and latency crux of an AI-native ACI. The four modalities are complementary, not competing:

- **Pixels** are universal and zero-instrumentation but the most expensive on every axis. Anthropic's image-token formula is ~`w*h/750`; a 10-step full-screenshot loop is **~150K tokens vs ~25K for a11y — a ~6× difference** (vendor-published, unverified). Opus 4.7/4.8 raised the long-edge cap to 2576 px, *tripling* a dense screenshot's token cost.
- **Accessibility trees** (AT-SPI2 / UIA / AX / CDP) are orders of magnitude cheaper (a few thousand tokens after pruning) and replay-stable, but coverage is uneven on Electron/Qt/canvas/games.
- **Set-of-Marks / OmniParser** recovers structure from pixel-only UIs server-side.
- **Region/zoom and full frame** are the escalation rungs when structure is absent.

### Decision

A **structured-first, layered escalation** observation model:

- **Rung 0 (default):** a normalized cross-OS **a11y/DOM tree diff** (AT-SPI/UIA/AX/CDP) projected onto one `Element{ref, role, name, value, states, bbox, source, …}` schema with **stable per-session refs**.
- **Rung 1:** **Set-of-Marks / OmniParser**, server-side, on demand.
- **Rung 2:** region/zoom pixels. **Rung 3:** full frame.

Agents **act on element refs / marks by default**; raw `x,y` is permitted only at the pixel rungs. Target the ~6× token saving and replay-stable trajectories. The a11y-coverage assumption is **load-bearing and unverified** — gated on a measurement spike (see Risks and [`../notes/open-questions.md`](../notes/open-questions.md)).

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Pixels-only (screenshot loop)** | The dominant cost driver; ~6× more tokens, no replay-stable refs, requires a grounding model for every click. |
| **a11y-only** | Fails on canvas/WebGL/games/Electron with poor trees; needs a pixel fallback. |
| **Single fixed modality** | Misses that the modalities are complementary; the right answer is escalation, not selection. |

### Consequences

- **Positive:** ~6× token reduction, ~150× bandwidth reduction with D4, replay-stable element refs, model-agnostic grounding.
- **Negative:** Shinken must build and maintain a cross-OS a11y normalizer (AT-SPI/UIA/AX/CDP → one schema) and a server-side SoM service.
- **Risks:** **the load-bearing unverified assumption** is that real target apps expose usable a11y trees cheaply. Mitigation: instrument a representative app set (browser, Electron, native Win/macOS, canvas/WebGL, a game) and measure the fraction with usable trees + bandwidth before committing density/cost claims.

### Evidence

- Anthropic vision token formula + caps: <https://platform.claude.com/docs/en/build-with-claude/vision>; ~6× a11y-vs-pixel token measurement: <https://fazm.ai/blog/benchmarked-ai-browser-tools-token-efficiency-native-apis>
- OmniParser V2 (SoM): <https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/>, <https://arxiv.org/abs/2408.00203>
- UFO2 (UIA+vision fusion): <https://arxiv.org/abs/2504.14603>; A11y-CUA accessibility gap: <https://arxiv.org/html/2602.09310>
- Detail in [`../notes/streaming-bandwidth.md`](../notes/streaming-bandwidth.md).

---

## D4 — Streaming: single-PeerConnection WebRTC, dual-transport

**Status:** Accepted.

### Context

For agent-driven desktops the observation is overwhelmingly near-static UI whose *meaning* is far smaller than its *pixels*. Structured operations cost ~5–80 kbps per active desktop versus 3–5 Mbps for naive H.264 1080p — a **30–250× bandwidth reduction**; structured ≈ 20 kbps avg vs ~3 Mbps office H.264 is ~**150×** (vendor-published, unverified). At 100k concurrent 24×7 desktops the egress math is ~$4.9M/mo (H.264) vs ~$0.8M (AV1-SCC) vs near-zero (structured) (vendor-published, unverified). WebRTC gives two transports on one DTLS-secured PeerConnection: SRTP media tracks (lossy, real-time, GCC/TWCC-controlled, jitter-buffered) and RTCDataChannel over SCTP (configurable reliable/ordered). The decisive latency lever is not the codec — a clean path is ~10 ms encode + ~10 ms decode + ~10–30 ms transport — but the jitter buffer.

### Decision

A **single-PeerConnection, dual-transport** design:

- **Reliable-ordered data channel = the structured event stream**, and **this stream IS the replay log** (D5).
- **Media track = on-demand NVENC H.264/AV1**, screen-content-tuned (D11 for the encode hardware constraints).
- **SFU** fan-out (encode-once), **WHIP/WHEP** signaling, jitter buffer minimized; target glass-to-glass ~50–120 ms same-region.
- Tiers: **Tier 0** structured (~20 kbps) / **Tier 1** SoM / **Tier 2** video — video spins up only when pixels are actually needed.
- **host ↔ guest = virtio-vsock** where available (Linux/QEMU); a **guest-initiated outbound TCP/WebSocket callback** is the portable fallback (Windows has no vsock guest driver; VZ/macOS lacks it). **Never HTTP screenshot-polling.**

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **VNC / RFB pixel streaming (E2B noVNC, cua VNC)** | Plain pixel protocol; high bandwidth, no structured channel, no replay-log reuse. |
| **HTTP screenshot polling (OSWorld :5000)** | Worst case on bandwidth and latency; the thing Shinken is built to beat. |
| **Two separate connections (data + media)** | Doubles ICE/DTLS setup and NAT-traversal surface; one PeerConnection multiplexes both. |
| **Always-on video** | Wasteful: most agent time is near-static; make video on-demand and keep structured primary. |
| **vsock everywhere** | Strands Windows/macOS (no vsock guest driver); use a portable outbound callback for those. |

### Consequences

- **Positive:** the headline ~150× bandwidth win; one connection carries actions, observations, permissions, and (on demand) video; the data channel doubles as the durable replay log.
- **Negative:** WebRTC AV1/HEVC payload negotiation is newer/less battle-tested than H.264; SFU, TURN, and signaling are separate builds; WebRTC defaults are camera-tuned and must be re-tuned for screen content.
- **Risks:** glass-to-glass latency numbers are unverified pending a dual-channel PoC; jitter-buffer tuning is the make-or-break for "feels interactive."

### Evidence

- WebRTC data channels (SCTP/DTLS): <https://datatracker.ietf.org/doc/html/rfc8831>; congestion control (TWCC/GCC): <https://bloggeek.me/webrtcglossary/transport-cc/>, <https://c3lab.poliba.it/images/6/65/Gcc-analysis.pdf>; latency breakdown: <https://transitiverobotics.com/blog/webrtc-latency-breakdown/>
- AV1 screen-content coding (~100 kbps class): <https://visionular.ai/av1-screen-content-coding/>; NICE DCV bitrate perspective: <https://aws.amazon.com/blogs/hpc/putting-bitrates-into-perspective/>
- rrweb DOM-mutation streaming model: <https://github.com/rrweb-io/rrweb>; session-recording overhead: <https://posthog.com/blog/session-recording-performance>
- Detail in [`../notes/streaming-bandwidth.md`](../notes/streaming-bandwidth.md).

---

## D5 — Replay: event stream + bisected snapshots, the `.skn` bundle

**Status:** Accepted.

### Context

Prior art converges on a small set of reusable primitives but no one ships the whole stack: OSWorld writes only `traj.jsonl` + `recording.mp4` (no time-travel); Morph has VM-level branch/time-travel but no agent-trajectory event timeline; Playwright trace is record-only. The format primitives are well understood — rrweb's typed/timestamped `eventWithTime` envelope with a discriminator enum; asciicast's JSONL header + `[time, code, data]` rows with `m` marker events as the scrub/branch anchor; Playwright's `trace.zip` packaging; OpenTelemetry GenAI semconv for the decision channel. **Bit-deterministic replay (rr-style) is x86/Linux-only and single-core** — infeasible cross-OS. And **fast snapshot-fork is now a commodity**: the same CoW-fork primitive that resets a sandbox also branches a trajectory.

### Decision

Replay = **the event stream + bisected snapshots**, packaged as the **`.skn` bundle** (a ZIP, Playwright-trace model):

- `manifest.json` + an append-only **`events.jsonl`** with a two-level envelope: `kind ∈ {action, observation, decision, permission, marker, snapshot_ref, meta}`; a logical-clock `seq` plus a wall-clock anchor; an `action_id` pairing each action to its observation.
- An immutable **checkpoint DAG** (branchable, never mutated) + content-addressed media (fMP4).
- The **decision channel uses OpenTelemetry-GenAI** semantic conventions.
- **Not bit-deterministic** — a pragmatic **state-snapshot + event-log + observation-log** model.
- **Branch = CoW-fork the env snapshot + deserialize the agent checkpoint → re-run from step N** (the same primitive as instant reset, D1). The `.skn` replay doubles as **RL/SFT training data** (the adoption wedge, D12).

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Bit-deterministic record/replay (rr)** | x86/Linux-only, single-core; cannot span Windows/macOS or the multi-core desktop tier. |
| **Video-only recording (OSWorld `recording.mp4`)** | No scrub-to-action, no fork, no re-run; not training-grade. |
| **Agent-state-only checkpoint (LangGraph-style)** | Forks the agent but re-runs side effects against a live, drifted world; needs the env snapshot too. |
| **Mutable trajectory log** | Breaks branch provenance; the checkpoint DAG must be immutable and append-only. |

### Consequences

- **Positive:** one representation is the live stream *and* the replay log *and* the branch substrate *and* RL/SFT data; time-travel and counterfactual re-runs are nearly free given shared-prefix CoW pages.
- **Negative:** side-effecting tool calls on a branch need record/mock or idempotency handling; a forked VM pins its backing memory file (snapshot GC complexity).
- **Risks:** event-schema versioning + upcasting must be specified (open question); not bit-deterministic means re-runs can diverge — that is by design and must be documented to users.

### Evidence

- rrweb envelope/types: <https://github.com/rrweb-io/rrweb/blob/master/packages/types/src/index.ts>; asciicast v2/v3 (markers, interval timing): <https://docs.asciinema.org/manual/asciicast/v2/>, <https://docs.asciinema.org/manual/asciicast/v3/>
- Playwright tracing/trace-viewer: <https://playwright.dev/docs/api/class-tracing>, <https://playwright.dev/docs/trace-viewer>
- OpenTelemetry GenAI semconv: <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>
- Firecracker CoW branch + uniqueness: <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md>; Tree-GRPO (branching for RL): <https://arxiv.org/abs/2509.21240>
- Detail in [`../notes/replay.md`](../notes/replay.md).

---

## D6 — Sandbox capabilities: entitlement provisioning + boundary enforcement (Cedar + ocap handle + OS)

**Status:** Accepted. The cornerstone differentiator: Sandboxes are powerful by default inside the isolation boundary, while their boundary-crossing capabilities and OS entitlements are explicit, revocable, replayed, and enforceable.

### Context

An agent sandbox is valuable precisely because it lets an agent do real, risky work in an isolated computer: install packages, edit files, drive UI, run code, capture screens, and even break the guest before resetting it. The permission model must not turn every ordinary in-sandbox action into a human approval. The boundary must instead decide **what powers this Sandbox is provisioned with** and **what leaves or enters the Sandbox**.

Model-level prompt-injection defenses are unreliable — measured at 0–62% robust against static attacks and 80–100% defeated by adaptive attacks, with human red-teaming at 100% success. The boundary must therefore be **architectural**, holding even when the model is jailbroken. The load-bearing questions are **(a) how to describe sandbox capabilities/entitlements**, **(b) which policy engine can prove grants do not widen accidentally**, and **(c) why policy alone is not enough for live revocation or OS-level friction**.

On the engine: **Cedar** is the only candidate that is **statically analyzable** — its SMT/Lean-verified symbolic compiler lets Shinken *prove* a sandbox capability grant or policy edit "never grants more than before." Cedar's PARC + permit/forbid + forbid-overrides + deny-by-default fits capability provisioning; it is reportedly **42–60× faster than OPA/Rego** with sub-ms decisions (vendor-published, unverified). **Rego is Turing-flexible — you cannot statically prove a change never widens access** — which is disqualifying for boundary capabilities such as credentials, host mounts, external egress, GPU, and persistence.

The deeper insight: **a policy engine alone cannot do live revocation or OS entitlement provisioning.** Decision caching (a published authorizer TTL of ~120 s, ~2 min agent cache refresh) leaves a stale-authorization window. Revocation must be a synchronous bit-flip checked at *use* time, not a wait for a cache to expire. Separately, macOS TCC, Windows tokens, Linux seccomp/Landlock, screen capture, input injection, and accessibility automation require OS-specific preflight and provisioning work; this is part of the product, not a mere approval dialog.

Egress is the highest-leverage control but **SNI/Host filtering is not a hard boundary**: a SOCKS5 null-byte parser differential and DNS-tunneling via subdomain labels both bypass it. The forced **out-of-VM egress proxy** (deny-by-default, scoped wildcards, anti-domain-fronting, optional TLS-MITM, fail-closed) is the production pattern, backed by an OS netns/firewall so an agent ignoring proxy env still cannot reach the internet.

### Decision

A **three-layer sandbox capability** model:

```
   Request/provision a sandbox capability (e.g. net.egress to api.github.com)
                         │
   ┌─────────────────────▼──────────────────────┐
   │  (1) CEDAR  — decision / grammar layer      │   "May this Sandbox have this capability?"
   │  PARC, permit/forbid, forbid-overrides,     │   statically analyzable (SMT/Lean):
   │  deny-by-default; template-linked per grant │   prove "no more permissive than before"
   └─────────────────────┬──────────────────────┘   BEFORE the grant ships (pre-grant gate + CI)
                         │ permit
   ┌─────────────────────▼──────────────────────┐
   │  (2) OCAP HANDLE — caretaker / membrane     │   the LIVE on/off switch the panel toggles.
   │  unforgeable, attenuable, REVOCABLE ref     │   revoke = O(1) bit-flip, checked at USE time
   └─────────────────────┬──────────────────────┘   (synchronous, fail-closed — no cache wait)
                         │ enforce
   ┌─────────────────────▼──────────────────────┐
   │  (3) OS ENFORCEMENT — the wall              │   makes it physically real; holds when the
   │  Linux: bubblewrap + seccomp(net-gate) +    │   model is jailbroken.
   │  Landlock + cgroups-v2 + OUT-OF-VM egress   │
   │  proxy.  macOS: Seatbelt + TCC.             │
   │  Windows: restricted token + capability-SID │
   └─────────────────────────────────────────────┘
```

**Cedar decides the capability envelope; the handle is the live switch; the OS/substrate is the wall.**

- **8 capability classes**, each default-empty at the boundary: `net.egress`, `fs.scope` / host mounts, `clipboard`, `gpu`, `install.privileged/sudo`, `persistence`, `credentials`, `peripheral` / OS automation.
- **Sandbox-internal power is expected.** A Sandbox image may intentionally include sudo, package managers, screen capture, clipboard, and automation APIs so the agent can do real work. Those powers are safe because they are scoped to the disposable guest unless paired with a boundary capability.
- **Boundary capabilities are explicit and recorded.** External egress, credential brokering, host filesystem scopes, persistence, expensive compute, peripheral access, and production-side effects are granted by policy, time-boxed, revocable, and emitted as replay events (D5).
- **HITL is exceptional, not the hot path.** Humans approve unusual boundary grants or policy changes; they should not approve every click, keypress, install, or file edit inside an isolated Sandbox.
- **OS entitlement management is first-class.** macOS TCC (Accessibility, Screen Recording, Input Monitoring, Automation, Full Disk Access), Windows restricted tokens/capability SIDs, and Linux Landlock/seccomp/netns must be preflighted, provisioned, and surfaced honestly in the capability descriptor.
- **Secrets brokered via Vault/KMS + proxy header-injection — the model never sees plaintext.** Prefer JIT short-lived credentials (SPIFFE SVIDs, Vault dynamic secrets) tied to the grant lifecycle. Apply the **Rule-of-Two / lethal-trifecta** constraint: at most two of {untrusted input, sensitive data, external comms} unattended, else force human-in-the-loop.

**The boundary rule:** policy is enforced where a capability crosses the Sandbox boundary or binds scarce/privileged host resources. D2's code-as-action and GUI actions are ordinary in-sandbox powers when the Sandbox is provisioned for them; egress, credentials, host mounts, persistence, GPU, and OS automation entitlements flow through the controlled capability layer.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **OPA / Rego as the decision engine** | Turing-flexible → cannot statically prove a policy change never widens access; ~42–60× slower; error-prone in independent benchmarking. Kept only as an *optional outer fleet/org-rule layer* that can further restrict, never widen. |
| **Policy engine as the enforcement / revoke mechanism** | A decision is advisory until something OS-level enforces it; decision caching leaves a stale-revocation window; OS entitlements still must be provisioned. Hence the separate ocap handle + OS/substrate layers. |
| **Ask before every dangerous in-sandbox action** | This turns a sandbox runtime into a permission nag. A real agent sandbox should let agents perform risky operations inside the disposable guest; only boundary powers and scarce resources need capability control. |
| **In-runtime / in-process network allowlist** | Multiple 2025–2026 CVEs (a Claude Code SOCKS5 bypass, AWS AgentCore escapes) show in-runtime allowlists are bypassable. Enforce egress out-of-VM. |
| **SNI/Host-only egress filtering** | Defeated by domain fronting, broad allowlist entries, and parser differentials; canonicalize at the seam, fail-closed on MITM-required, block raw port 53. |
| **Hand the agent raw credentials** | The model leaking/exfiltrating a long-lived key has unbounded blast radius; broker at the proxy, prefer JIT SVIDs, exclude secrets from replay. |
| **Grant-time checks** | TOCTOU; enforce at use time via the live handle. |

### Consequences

- **Positive:** powerful Sandboxes that can do real work; provable non-escalation for boundary grants; synchronous revoke; OS entitlement preflight; a complete, forkable, non-repudiable capability timeline in the replay; the panel is the category-defining product surface.
- **Negative:** more plumbing (a proxy/indirection handle at each enforcement point); cross-OS enforcement diverges sharply (macOS Seatbelt/TCC, Windows AppContainer egress is coarse) and the panel must degrade to the weakest per-OS enforcement and *say so*.
- **Risks:** Cedar's tooling/community is younger than OPA's; some mechanisms need recent kernels (Landlock network 6.7+, unprivileged userns disabled on some hosts) — feature-detect with fallback; the egress host-canonicalization gap must be fixed explicitly (do not copy a `normalize_host` that does not strip NUL/control chars).

### Evidence

- Cedar (analyzable, PARC, templates, operators): <https://docs.cedarpolicy.com/policies/syntax-policy.html>, <https://docs.cedarpolicy.com/policies/templates.html>, <https://arxiv.org/pdf/2403.04651>; Cedar Analysis (SMT): <https://aws.amazon.com/blogs/opensource/introducing-cedar-analysis-open-source-tools-for-verifying-authorization-policies/>; OPA→AVP 42–60×: <https://aws.amazon.com/blogs/security/migrating-from-open-policy-agent-to-amazon-verified-permissions/>; Cedar in AgentCore ENFORCE mode: <https://shinyaz.com/en/blog/2026/03/15/bedrock-agentcore-policy-cedar-authorization>
- ocap caretaker/membrane: <https://tersesystems.github.io/ocaps/guide/management.html>, <https://en.wikipedia.org/wiki/Object-capability_model>
- OS enforcement: Landlock <https://docs.kernel.org/userspace-api/landlock.html>; capabilities(7) <https://man7.org/linux/man-pages/man7/capabilities.7.html>; Claude Code security model <https://code.claude.com/docs/en/security>
- Egress proxy + secret brokering: Codex network-proxy <https://github.com/openai/codex/blob/main/codex-rs/network-proxy/src/policy.rs>, MITM credential injection <https://github.com/openai/codex/blob/main/codex-rs/network-proxy/src/mitm_hook.rs>; Cloudflare Outbound Workers <https://blog.cloudflare.com/sandbox-auth/>; SPIFFE/Vault JIT creds <https://www.hashicorp.com/en/blog/vault-enterprise-1-21-spiffe-auth-fips-140-3-level-1-compliance-granular-secret-recovery>
- Prompt-injection reality + Rule-of-Two: <https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/>, <https://simonw.substack.com/p/new-prompt-injection-papers-agents>; egress bypasses: <https://oddguan.com/blog/second-time-same-sandbox-anthropic-claude-code-network-allowlist-bypass-data-exfiltration/>, <https://unit42.paloaltonetworks.com/bypass-of-aws-sandbox-network-isolation-mode/>
- Detail in [`../notes/permissions.md`](../notes/permissions.md) and the kill chains in [`08-threat-model.md`](08-threat-model.md).

---

## D7 — Eval layer: thin orchestration on the runtime, inverting OSWorld

**Status:** Accepted (phased). Conformance suites land across the roadmap.

### Context

OSWorld bolts a Python eval module onto a gym Env: each task is a JSON config whose evaluator names a metric func plus parallel result/expected getters resolved at runtime via **stringly-typed `getattr`**, collapsed to a single 0/1 reward at episode end. This is brittle on every axis — getters reach into live app internals with hard-coded, admittedly-untested per-OS paths; metrics `eval()` strings; grading is a single destructive snapshot diff; it depends on fixed sleeps. The lesson that matters most: **OSWorld v1 shipped 300+ grader/task bugs** (later fixed in OSWorld-Verified), and self-reported vs verified scores diverge wildly. Graders are not infrastructure plumbing — they are **tested artifacts**. Separately, agentic evals are stochastic, so a single run is not a measurement.

### Decision

**Invert OSWorld**: make the eval layer a *thin orchestration on top of the production runtime*, reusing Shinken's snapshots, forks, and replay rather than a bolt-on gym.

- A **typed verifier DAG** (not stringly-typed `getattr`), **programmatic-primary with a constrained model-verifier fallback**.
- A **golden snapshot per task** (D1/D5), so setup is deterministic and instant.
- **N ≥ 5 CoW-forked replicas → pass@k / pass^k with confidence intervals** — evals are statistical, not single-shot.
- **Readiness probes, not sleeps.**
- **Task + grader + env are versioned together**; the grader is a tested artifact with an independent-verification policy.
- Built-in conformance: **OSWorld-Verified, WindowsAgentArena, AndroidWorld, WebArena / VisualWebArena / WebVoyager.** Trajectory capture (`.skn`) feeds RL/SFT (D12).

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Clone OSWorld's eval design** | Stringly-typed getters, destructive single-snapshot grading, fixed sleeps, untested cross-OS paths, 300+ historical grader bugs. |
| **Single-run pass/fail** | Agentic evals are stochastic; one run is noise, not a measurement. |
| **Model-verifier-primary grading** | Non-deterministic and gameable; use programmatic-primary with a constrained model fallback only where programmatic checks can't reach. |
| **Live-site benchmarks as the bar (WebVoyager drift)** | Live sites drift; pin/snapshot environments and version task+grader+env together. |

### Consequences

- **Positive:** reproducible, statistically honest evals on the same runtime that serves production; instant golden-snapshot setup; the eval layer and production share one substrate (the north-star "one platform, layered").
- **Negative:** building typed verifier DAGs and versioned conformance suites is substantial work; replicating each benchmark faithfully is ongoing maintenance.
- **Risks:** Shinken's own graders can carry the same bug class OSWorld did — hence "grader as tested artifact" and an independent-verification policy are non-negotiable.

### Evidence

- OSWorld-Verified (300+ grader fixes): <https://xlang.ai/blog/osworld-verified>; OSWorld stringly-typed eval: <https://github.com/xlang-ai/OSWorld>
- Pass@k vs pass^k / eval stochasticity: <https://arxiv.org/pdf/2602.07150>, <https://arxiv.org/pdf/2512.06710>; reliability under stress: <https://arxiv.org/pdf/2601.06112>
- Verifiable worlds / rollout-as-a-service: <https://arxiv.org/html/2605.19769v1>, <https://arxiv.org/html/2603.18815v1>; HUD RL environments: <https://www.hud.ai/resources/best-platforms-publishing-rl-environments-model-labs>
- Detail in [`../notes/eval-benchmarks.md`](../notes/eval-benchmarks.md) and [`03-osworld-analysis.md`](03-osworld-analysis.md).

---

## D8 — Interfaces: native streaming SDK core + optional MCP facade

**Status:** Accepted.

### Context

The market validates a clear stratification — `trycua/cua` (≈15K stars, MIT) ships exactly it: (1) a native Computer SDK with granular primitives talking to an in-VM server over a persistent connection; (2) an Agent SDK wrapping it in a ReAct loop; (3) MCP exposure at *both* a granular and a high-level task altitude. Crucially, the **MCP spec itself says progress notifications are NOT suitable for high-frequency updates** — MCP is a control-plane/tool facade, not a real-time media transport.

### Decision

A **native streaming SDK core + an optional MCP facade**:

- **One IDL → generated `py`/`ts` SDKs** over the bidirectional streaming transport (D4).
- An **MCP facade at two altitudes** — granular tools (screenshot/click/type) and an agent-task tool — for model-agnostic hosts.
- **Never route the high-frequency action/observation/video loop or media through MCP.**
- **OAuth 2.1** for the facade.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **MCP as the only interface** | MCP's own spec rules out high-frequency/real-time transport; the hot loop and media must not go through it. |
| **Native SDK only (no MCP)** | Forgoes drop-in compatibility with MCP hosts (Claude Code, Cursor); the facade is cheap to add at two altitudes. |
| **Per-language hand-written SDKs** | Drift between `py` and `ts`; generate both from one IDL. |

### Consequences

- **Positive:** the performance-critical loop stays on the native transport; MCP hosts get a clean facade; SDK parity across languages; matches the validated cua layering.
- **Negative:** maintaining both a native transport and an MCP facade; OAuth 2.1 plumbing for the facade.
- **Risks:** MCP transport is itself evolving (stateless/horizontal-scale roadmap); keep the facade thin so spec churn does not ripple into the core.

### Evidence

- cua dual-interface layering: <https://github.com/trycua/cua>, <https://cua.ai/docs/cua/reference/mcp-server/usage>
- MCP transports + "progress not for high-frequency": <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>, <https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress>; OAuth 2.1: <https://modelcontextprotocol.io/specification/draft/basic/authorization>
- Detail in [`../notes/ai-native-interface.md`](../notes/ai-native-interface.md).

---

## D9 — Control plane: Fleet Manager + Action Gateway

**Status:** Accepted.

### Context

Running a computer-use platform at ultra-high concurrency is a **fleet-of-idle-VMs economics problem** layered on a **hostile-untrusted-input security problem**. The industry has converged: don't cold-boot per request — keep **warm pools** and **fork/restore from memory snapshots**. The OSS **`kubernetes-sigs/agent-sandbox`** SIG project has standardized the control-plane primitives (`Sandbox`, `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool` CRDs) plus pod-snapshot suspend/resume and a cheap suspended cold pool that replenishes the warm pool (a published reference cites ~300 sandboxes/s/cluster, p90 ~200 ms — vendor-published, unverified). **Idle time dominates cost**, so auto-suspend-to-snapshot on idle is the central lever.

### Decision

- **Fleet Manager** = warm pools (per image / region / tier) + fork-on-demand + cold-pool replenish, in the OSS `agent-sandbox` CRD shape.
- **Action Gateway** = a single chokepoint: **tenant-auth → token-bucket/WFQ rate-limit → budget → Cedar policy (D6) → dispatch.**
- **Dual-timer sessions:** idle ~15 min reset-on-activity; max-lifetime ~4–8 h; **auto-suspend-to-snapshot on idle.**
- **OTel-GenAI telemetry.** Sandbox health is a **circuit-breakable dependency.**

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Cold-boot per request** | Seconds of latency and wasted spend; warm pools + fork are the standard. |
| **No single chokepoint** | Scatters auth/rate/budget/policy across services; the Action Gateway centralizes them so D6 is enforced once. |
| **Keep idle sandboxes hot** | Idle dominates cost; auto-suspend-to-snapshot reclaims it. |
| **Bespoke control-plane CRDs** | The `agent-sandbox` SIG already standardized the shape; align rather than reinvent. |

### Consequences

- **Positive:** sub-second perceived starts via warm pools + fork; one place to enforce policy and budget; idle cost reclaimed; standard K8s operational model.
- **Negative:** warm-pool sizing math and cold-pool replenishment are real engineering; circuit-breaking and graceful degradation under GPU/NVENC saturation must be designed.
- **Risks:** warm-pool exhaustion behavior and snapshot-store growth/GC need explicit SLOs (see [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md)).

### Evidence

- Agent Sandbox on GKE (CRDs, throughput): <https://cloud.google.com/blog/products/containers-kubernetes/bringing-you-agent-sandbox-on-gke-and-agent-substrate>; agent-sandbox SIG (Kata): <https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/kata-containers/>; production overview: <https://northflank.com/blog/agent-sandbox-on-kubernetes>
- Firecracker warm-resume + page faults: <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md>; OTel GenAI agent spans: <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/>
- Detail in [`../notes/sandbox-infra.md`](../notes/sandbox-infra.md).

---

## D10 — Cross-platform: one control plane, one Guest Runtime contract, one ACI

**Status:** Accepted (phased). Linux v1; Windows + macOS v1 heavier tiers; Android roadmap.

### Context

`trycua/cua` is the cross-platform bar (macOS/Linux/Windows/Android under one SDK). No single hypervisor covers all three desktop guests (D1), and host↔guest transport is not uniform (vsock on Linux/QEMU; outbound TCP/WebSocket fallback on Windows/macOS). The unifying pattern across cua, Daytona, and OSWorld is **one small in-guest daemon** exposing screenshot + input + shell over an API, started by the OS-native first-boot hook (cloud-init / cloudbase-init / a baked LaunchDaemon) and dialing back to the control plane.

### Decision

**Linux is first-class in v1** (the fork tier). **Windows + macOS ship in v1** as heavier, longer-lived tiers; **Android is roadmap.** Above the per-OS divergence sit exactly three unifying contracts:

- **One** control plane (D9),
- **One** Guest Runtime (`shinkend`) contract — the in-Sandbox daemon executing the ACI and emitting the event stream,
- **One** ACI (D2/D3) across all guests,

with a **per-OS handler-factory beneath**. The post-clone uniqueness hook (D1) runs per OS (Linux reseed; Windows sysprep generalize; macOS regenerate identifiers + re-register).

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Linux-only (E2B/Morph)** | Forfeits the full cross-platform desktop differentiator; cua already spans all OSes. |
| **Three separate stacks/APIs per OS** | Users would learn three systems; the value is one ACI + one control plane with the substrate routed underneath. |
| **Android in v1** | redroid/Cuttlefish touch/gesture reconciliation with the desktop-pointer ACI is unsolved; defer to roadmap. |
| **vsock everywhere** | No Windows/macOS vsock guest driver; portable outbound callback for those. |

### Consequences

- **Positive:** one mental model and one ACI regardless of OS; the scheduler routes to the right substrate pool transparently; Linux gets the killer fork features without holding back the cross-OS story.
- **Negative:** Windows/macOS are heavier, lower-density, and (macOS) hard-capped; fast reset is largely infeasible on Windows/macOS today.
- **Risks:** Windows-in-cloud licensing and the macOS 2-VM/host cap shape cost and roadmap (see [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md)); a11y coverage differs per OS (D3 risk).

### Evidence

- cua cross-platform + in-guest server: <https://github.com/trycua/cua>, <https://cua.ai/docs/lume/guide/getting-started/introduction>; Windows Sandbox provider: <https://cua.ai/blog/windows-sandbox>
- Host↔guest transport (no Windows vsock driver): <https://github.com/cloud-hypervisor/cloud-hypervisor/discussions/5431>; Windows golden image + cloudbase-init: <https://deepwiki.com/cloudbase/windows-imaging-tools/5-configuration-reference>
- macOS substrate (Tart/lume, APFS CoW): <https://tart.run/quick-start/>, <https://cua.ai/docs/lume/guide/getting-started/introduction>
- Detail in [`../notes/sandbox-infra.md`](../notes/sandbox-infra.md).

---

## D11 — GPU: optional acceleration tier

**Status:** Provisional. The architecture (opt-in, two pools, no encode on AI flagships) is firm; per-card density, the vGPU mdev→VFIO transition, and GPU-TEE maturity are gated on a first-party spike.

### Context

GPU is the NVIDIA-aligned wedge no pure-Linux-Firecracker competitor holds — but the research overturns several intuitions, and three findings are decision-changing:

1. **NVIDIA's flagship AI GPUs A100, H100, H200, B200 have ZERO NVENC encode engines** — they ship NVDEC (decode) and NVJPEG/OFA only. A streaming encode tier therefore **cannot** run encode on the same GPUs an AI fleet uses. **MIG does not surface NVENC on these parts** (only the `+me` profile gets one of each *available* media engine, and NVENC isn't available). Hardware encode for streaming must run on the **Ada L-series (L4, L40, L40S)**, A-series graphics (A10/A16/A40), Turing T4, or RTX/RTX PRO. (Public NVIDIA fact.)
2. **The "8 NVENC sessions" cap is consumer-GeForce-only.** Qualified datacenter/pro GPUs (T4/A10/L4/L40S/RTX PRO) have **no artificial session cap** — concurrency is bounded by encoder throughput, VRAM, and memory bandwidth. (Public NVIDIA fact.)
3. **VFIO passthrough and vGPU device state cannot be snapshotted or live-migrated** — so the sub-second VM-fork reset that defines the Linux CPU tier (D1) **does not extend to the GPU tier**.

Most agent/browser tasks are CPU-only and ride the Firecracker fork tier; a desktop needs a *surface*, not a GPU (D1). So GPU should be a minority, opt-in tier. Density for GPU desktops is double-bounded by **frame buffer** *and* a **context-switch ceiling** (NVIDIA documents the 48 GB A40 capped at 32 vGPU users despite memory headroom — public NVIDIA fact). NICE/Amazon DCV is the closest production blueprint for the NVENC+QUIC+browser pixel path.

### Decision

GPU is an **opt-in acceleration tier**, not the default path.

- **Encode tier NEVER on A100/H100/H200/B200** (zero NVENC). Use **Ada L4** for density and **L40S** for premium 4K / AV1 + render (public NVIDIA fact). **No MIG for the encode tier** (Ada has no MIG; A100/H100 MIG has no NVENC). Multi-tenant the encoders by app-level session packing on a qualified GPU or by **time-sliced vGPU** (all vGPUs share the card's NVENC engines).
- **Two GPU pools:**
  - **Pool A — time-sliced vGPU** for many light desktops (density). Size by frame buffer first (~1–2 GB profiles → ~24–48 sessions on a 48 GB card by memory) **then validate against the ~32-user context-switch ceiling**; schedule **Equal/Fixed Share, never Best Effort** for untrusted multi-tenant.
  - **Pool B — MIG-backed / Confidential Containers** for isolation-sensitive / trusted workloads, with **GPU-TEE + NRAS attestation** (public NVIDIA Confidential Computing / NRAS facts).
- **GPU tier is snapshot-light / longer-lived**, recycled on TTL; if fast GPU restore is ever needed, the only viable path is gVisor-GPU + CRIU-style GPU memory snapshots (software isolation, not KVM).
- **Codec:** AV1-first with strict capability negotiation (AV1 → HEVC → H.264). AV1 gives ~40% bitrate savings vs H.264 at equal quality (NVIDIA-measured, vendor-published, unverified); NVENC encode latency is near-invariant across presets, so run high quality at low latency.
- **Capture:** NVFBC on Linux, DXGI Desktop Duplication on Windows (NVFBC is dead on Windows); macOS has no NVENC — use VideoToolbox.
- **Build-vs-buy for the pixel channel: NICE DCV** (publicly available NVENC + QUIC + browser-native remote-display product) **vs** a custom GStreamer `nvcodec → webrtcbin` pipeline (D4). Adopt DCV where wire-format control is not required; build the custom WebRTC+NVENC path where Shinken must also carry its structured event channel on the same connection.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **GPU on by default** | Most tasks are CPU-only; GPU is expensive, snapshot-hostile, and scarce. Keep it opt-in and the fork tier the default. |
| **Encode on A100/H100 (the AI fleet)** | Zero NVENC engines; silently forces CPU software encode (x264/SVT-AV1) — high latency, low density. Decision-changing constraint. |
| **MIG for the encode tier** | MIG doesn't surface NVENC on A100/H100; Ada (L4/L40S) has no MIG. Use vGPU time-slicing or app-level packing. |
| **Raw time-slicing / MPS for untrusted agents** | No memory/fault isolation; one tenant can OOM or crash the whole GPU. Must sit inside a vGPU/MIG/VM boundary. |
| **VFIO passthrough for high concurrency** | 1 GPU = 1 VM (worst density) and blocks snapshot/migration. Only for rare single-tenant max-perf agents. |
| **Forcing AV1 without negotiation** | AV1 HW decode is still limited; forcing it yields jank or a black screen. Negotiate AV1 → HEVC → H.264. |

### Consequences

- **Positive:** a credible GPU-accelerated tier (3D/WebGL/CUDA/heavy-render) plus a high-fidelity NVENC pixel channel and an enterprise trusted-compute variant — a differentiator no Firecracker-only competitor can match; the expensive, snapshot-hostile pool stays small.
- **Negative:** the GPU tier cannot fast-fork (a major architectural asymmetry); vGPU is a licensed product whose per-concurrent-user cost can dominate GPU TCO; the mdev→vendor-VFIO/SR-IOV transition (kernel 6.8+) makes guest-driver installs fragile and version-matrix-sensitive.
- **Risks:** per-card density numbers are vendor best-case (e.g. ~130 AV1 720p30 streams/L4 is P1 preset at 720p — vendor-published, unverified) and must be benchmarked at Shinken's resolution/FPS/preset; GPU-TEE + NRAS maturity for agent workloads in 2026 is the unverified enterprise-wedge assumption.

### Evidence

- A100/H100 zero NVENC, qualified-GPU no-cap, MIG media-engine allocation: NVENC Application Note <https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html>; NVENC per-GPU encoder generations <https://en.wikipedia.org/wiki/Nvidia_NVENC>; MIG profiles <https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html>
- AV1 ~40% savings + preset latency-invariance + L4 density: <https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/>, <https://www.nvidia.com/en-us/data-center/l4/>
- vGPU vs MIG vs time-slicing + scheduling + A40 32-user cap: <https://research.colfax-intl.com/sharing-nvidia-gpus-at-the-system-level-time-sliced-and-mig-backed-vgpus/>, <https://docs.nvidia.com/ai-enterprise/release-8/latest/infra-software/vgpu/features/scheduling.html>, <https://www.nvidia.com/en-us/data-center/a40/>
- VFIO/vGPU non-snapshottable: <https://forum.proxmox.com/threads/cannot-snapshot-vm-vfio-migration-not-supported.179190/>; CRIU GPU restore (Modal): <https://modal.com/blog/gpu-mem-snapshots>
- NICE/Amazon DCV (NVENC + QUIC + browser): <https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html>, <https://aws.amazon.com/blogs/gametech/stream-remote-environment-nice-dcv-quic-udp-4k-monitor-60-fps/>; GStreamer nvcodec build path: <https://gstreamer.freedesktop.org/documentation/nvcodec/nvautogpuh264enc.html>; capture (NVFBC deprecation on Windows): <https://developer.nvidia.com/capture-sdk>
- Detail in [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md).

---

## D12 — Business / positioning: open self-hostable core + optional hosted commercial layer

**Status:** Accepted.

### Context

The market punishes closed single-modality products (Scrapybara sunset). The open-source competitors (E2B, trycua/cua, OSWorld, HUD) thrive; the proprietary players (Anthropic Computer Use, OpenAI Operator, Browserbase) ship the model or the host but not an open, self-hostable, cross-platform runtime. First users are teams **building and evaluating** computer-use agents (model labs, researchers, RPA builders), and the `.skn` replay (D5) doubles as RL/SFT trajectory data — a concrete adoption wedge.

### Decision

- **Open, self-hostable core** + a reusable Operator + an open, provider-agnostic agent loop (no lock-in).
- An **optional hosted commercial layer**: the Control Panel, observability, permission-audit, and eval as a service.
- **North star:** ONE platform for production *and* eval, layered.
- **First users:** CUA model/eval teams, with the **replay-as-training-data** wedge.
- **Vendor-neutral**: runs on any Kubernetes/cloud; **optimized for NVIDIA GPUs where present** (D11), never dependent on them.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Closed/proprietary product** | The market punishes closed single-modality CUA products; open core drives adoption. |
| **Open everything, no commercial layer** | No sustainable funding for the hosted Control Panel / audit / eval; the differentiator is the panel-as-product. |
| **Vendor-locked (NVIDIA-only)** | Contradicts the vendor-neutral mandate; GPU is an acceleration option, not a dependency. |
| **Production-only or eval-only** | The north star is one platform serving both, layered — eval is thin orchestration on the production runtime (D7). |

### Consequences

- **Positive:** adoption with no lock-in; the replay-as-training-data wedge gives a concrete first-user hook; the hosted layer monetizes the category-defining panel; runs anywhere, faster on NVIDIA.
- **Negative:** open core means competitors can fork; the commercial layer must stay genuinely valuable (panel, audit, eval, observability) to fund development.
- **Risks:** balancing open vs hosted feature lines; sustaining cross-OS + GPU engineering on an open-core model.

### Evidence

- Competitive landscape (open vs hosted, Scrapybara sunset, cua/E2B/HUD): <https://github.com/trycua/cua>, <https://www.hud.ai/resources/best-platforms-publishing-rl-environments-model-labs>
- Replay-as-RL-data: Tree-GRPO <https://arxiv.org/abs/2509.21240>; rollout-as-a-service <https://arxiv.org/html/2603.18815v1>
- Full positioning in [`00-vision.md`](00-vision.md), [`01-prd.md`](01-prd.md), and [`04-landscape.md`](04-landscape.md).

---

## Cross-cutting reconciliation

The twelve decisions interlock; the load-bearing couplings:

- **One CoW-fork primitive serves three masters:** instant reset (D1), replay-branching (D5), and N-replica eval (D7). Build it once, expose `fork/branch/restore` as first-class verbs.
- **The structured event stream is one artifact in three roles:** the live stream (D4), the replay log (D5), and RL/SFT data (D12).
- **The `tool_runner` boundary is where D2 and D6 meet:** code-as-action and every privileged action route through the controlled API that enforces the Cedar decision and the egress allowlist before executing.
- **The Action Gateway (D9) is the single place D6 is enforced:** auth → rate → budget → Cedar → dispatch.
- **GPU (D11) is the one tier that breaks the fork invariant (D1):** it is snapshot-light by physics (VFIO/vGPU state is non-migratable), opt-in, and the encode hardware must be physically separate from any A100/H100 AI fleet.
- **a11y coverage (D3)** is the single most load-bearing unverified assumption across the design; **every "(vendor-published, unverified)"** number in this document requires the first-party measurement plan before it anchors an SLA.

Open questions carried forward (do not paper over): a11y coverage on Electron/Qt/canvas/games; Windows-in-cloud licensing and the macOS 2-VM/host economics; no first-party perf numbers yet; the consolidated threat model ([`08-threat-model.md`](08-threat-model.md)); the multi-player / non-exclusive computer-use in/out decision; and protocol/event-schema versioning + upcasting. See [`../notes/open-questions.md`](../notes/open-questions.md).
