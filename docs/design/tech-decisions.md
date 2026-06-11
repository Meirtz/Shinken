# Shinken — Technical Decisions (ADRs)

> The keystone document. Each Architecture Decision Record (ADR) below corresponds to one
> of the fourteen decisions D1–D14 that define Shinken. Every ADR follows one template:
> **Title · Status · Context · Decision · Alternatives (and why rejected) ·
> Consequences (positive / negative / risks) · Evidence**.
>
> Audience: maintainers and reviewers · Role: authoritative design-decision canon. If another doc
> disagrees with an ADR, the ADR wins until it is explicitly amended.
>
> Shinken is a public, vendor-neutral open-source project: the open infrastructure stack for
> computer-use agents — an AI-native, cross-platform **sandbox runtime + control plane + control
> panel** and a streaming-first successor to OSWorld. NVIDIA GPUs are a *supported, optimized
> acceleration option*, never a dependency.
>
> **Status legend:** *Accepted* = committed for v1; *Accepted (phased)* = committed but
> staged across the roadmap; *Provisional* = the right call on today's evidence, gated on a
> first-party measurement spike. **Every speed / density / cost number sourced from a vendor
> blog or spec sheet is marked "(vendor-published, unverified)"; a first-party measurement
> plan is required before any of them anchor an SLA.** Today's date: **2026-06-02**.
>
> Companion documents: [`02-architecture.md`](architecture.md),
> [`04-landscape.md`](landscape.md), [`08-threat-model.md`](threat-model.md),
> [`09-economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md). Full source list:
> [`../../notes/sources.md`](../../notes/sources.md).

---

## Decision map

| ADR | Decision | Status | One-line stance |
|-----|----------|--------|-----------------|
| **D1** | Isolation = tiered, substrate-pluggable, routed by `(OS × needs-GPU × needs-fast-fork)` | Accepted (phased) | Firecracker for headless Linux fork; QEMU-microvm/crosvm for Linux desktop; CLH/QEMU+VFIO for GPU/Windows; Apple VZ for macOS |
| **D2** | ACI action schema = one typed tagged-union with version-pinned adapters | Accepted | ~16 verbs, `target` oneof, code-as-action off by default behind the policy boundary |
| **D3** | Observation = screenshot-first baseline, structured upgrade | Accepted (screenshot baseline) / Provisional (structured-default upgrade, gated on spike #2) | v0.0.1 proves screenshot GUI loop plus reference structure; a11y/DOM diff → Set-of-Marks → region pixels optimize tree-rich apps |
| **D4** | Streaming = single-PeerConnection WebRTC, dual-transport | Accepted | Reliable data channel = event stream (= the replay log); on-demand media track |
| **D5** | Runtime state (checkpoint/fork/resume) is the headline; `.skn` replay is a supporting evidence ledger | Accepted | Immutable checkpoint DAG (snapshot/checkpoint/fork/resume) — implemented on the Docker disk tier (#209); the append-only `events.jsonl` / `.skn` replay surface is **removed/deferred per #216** (see [replay](../user/replay.md)). Reset and branch are one primitive |
| **D6** | Capability scoping — a Sandbox is granted the resources its task needs (supporting runtime feature) | Accepted (mostly designed) | In-sandbox power is unscoped; boundary grants (egress/fs/GPU/credentials/…) are scoped + recorded; server-side resolution designed (D9) |
| **D7** | Eval layer = thin orchestration on the runtime, inverting OSWorld | Accepted (phased) | Typed verifier DAG, golden snapshot per task, N≥5 forked replicas, readiness probes |
| **D8** | Interfaces = native streaming SDK core + optional MCP facade | Accepted | One IDL → py/ts SDKs; MCP facade at two altitudes; never the hot loop over MCP |
| **D9** | Control plane = Fleet Manager + Action Gateway | Accepted | Warm pools + fork-on-demand; single auth→rate→budget→policy chokepoint; dual-timer sessions |
| **D10** | Cross-platform = one control plane, one Guest Runtime contract, one ACI | Accepted (phased) | Linux first-class v1; Windows + macOS heavier v1 tiers; Android roadmap |
| **D11** | GPU = optional acceleration tier | Provisional | Opt-in; encode **never on A100/H100**; vGPU (density) + MIG/CoCo (trusted); NICE DCV is the build-vs-buy fork |
| **D12** | Business = open self-hostable core + optional hosted commercial layer | Accepted | No lock-in; runtime-state wedge; trajectory export as byproduct; optimized for NVIDIA where present |
| **D13** | Operation layer = one dual-tier observe + stable element identity/diffs + an element verb family | Accepted (phased) | One observe returns pixels + tree + focus; stable ids → `~/+/-` diffs; settle-before-observe; act-returns-observation; per-app scoping; physical events first — design canon in [operation-layer.md](operation-layer.md) |
| **D14** | macOS engine substrate = ScreenCaptureKit + AXUIElement + CGEvent under TCC | Accepted (phased) | One-shot SCK capture, AX tree (incl. Chromium/Electron enable attributes), CGEvent input; permissions-pending observe keeps the session alive |

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
| **One VMM for everything (e.g. QEMU q35 only)** | Possible (QEMU covers headless, desktop, Windows, GPU) but forfeits Firecracker-class fork speed on the high-volume headless tier and presents a larger device-model surface for multi-tenant use. Kept as a *unifier fallback* if operational surface must be cut; not the default. |
| **Firecracker for the desktop/GPU tier** | Dead end: no display device, no GPU; the GPU initiative is paused. Planning a roadmap on it is a trap. |
| **Cloud Hypervisor for the Linux desktop** | Its virtio-gpu exists only as unmaintained out-of-tree Spectrum-OS patches the maintainers will not upstream. Reserve CLH for GPU/Windows. |
| **OSWorld-style full snapshot-revert** | Seconds-to-minutes, I/O-bound on disk-delta size; structurally beaten by fork-from-snapshot. |
| **Containers (bare) as the isolation boundary** | "Your container is not a sandbox": a shared host kernel/driver is not a strong isolation boundary. Containers run *under* gVisor/Kata or a microVM, never raw — see the OSS `kubernetes-sigs/agent-sandbox` CRD pattern in D9. |
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

  **Sub-decision — settled 2026-06 (#56 reconciliation): `exec`/file-transfer are NOT ACI wire verbs in v0.0.1.** Workload setup/scoring empirically need shell-exec, python-exec, file up/download, and app launch (xlang-ai/CUA-Gym's 32k-task pipeline drives exactly seven such OSWorld endpoints — <https://github.com/xlang-ai/CUA-Gym>), but for v0.0.1 those flow over the **substrate's own out-of-band channel** (the OSWorld controller's `/execute`, `docker cp`/`docker exec`, the inject transport), not the typed ACI WebSocket. The SDK's `put_file`/`get_file` are deliberately substrate-side (`DockerGuestTransport`/`LocalArtifactStore`), not wire messages. **Rationale:** keep the v0 verb set small + auditable; the alpha gate's setup/scoring run on OSWorld's own server, and file transfer already works substrate-side, so no ACI exec verb is on the v0.0.1 critical path. **Revisit (post-v0.0.1):** add typed `exec`/`put_file`/`get_file`/`launch` wire verbs — behind the code-as-action capability class above — when a Workload must do setup/scoring purely through the ACI (e.g. a substrate with no side channel); spec them then, with the frozen-`ToolContext`/`context_updates` state shape but JSON-typed, not the untyped tunnels every harness uses today.

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
- **Risks:** schema evolution requires an **event upcasting** strategy (carried as an open question — see [`../../notes/open-questions.md`](../../notes/open-questions.md)); a missing adapter for a new grammar blocks that model until written.

### Evidence

- Anthropic computer-use tool (versioned grammars, bash, text_editor): <https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/computer-use-tool>, <https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/text-editor-tool>
- OpenAI Computer Use (`computer_call`, `pending_safety_checks`): <https://developers.openai.com/api/docs/guides/tools-computer-use>
- OSWorld `computer_13` + RCE-by-design in-VM server: <https://github.com/xlang-ai/OSWorld/blob/main/mm_agents/prompts.py>, <https://github.com/xlang-ai/OSWorld/blob/main/desktop_env/server/main.py>
- UI-TARS normalized DSL: <https://github.com/bytedance/UI-TARS>
- SWE-agent (ACI design principle): <https://arxiv.org/abs/2405.15793>
- Detail in [`../../notes/ai-native-interface.md`](../../notes/ai-native-interface.md).

---

## D3 — Observation: screenshot-first baseline, structured upgrade

**Status:** Accepted (screenshot baseline) / Provisional (structured-default upgrade, gated on spike #2). The screenshot GUI loop is committed and proven in v0.0.1; the structured-default upgrade — and every density/cost claim that rides on it — is the right call on today's evidence but stays provisional until the first-party a11y-coverage spike (#2) measures real coverage and tree-diff bandwidth.

### Context

The observation channel — not the action grammar — is the bandwidth, cost, and latency crux of an AI-native ACI. The first usable GUI agent must work with screenshots because pixels are universal; structured observation is the optimization that makes the runtime cheaper, more stable, and more replayable where the UI exposes structure. The four modalities are complementary, not competing:

- **Pixels** are universal and zero-instrumentation but the most expensive on every axis. Anthropic's image-token formula is ~`w*h/750`; a 10-step full-screenshot loop is **~150K tokens vs ~25K for a11y — a ~6× difference** (vendor-published, unverified). Opus 4.7/4.8 raised the long-edge cap to 2576 px, *tripling* a dense screenshot's token cost.
- **Accessibility trees** (AT-SPI2 / UIA / AX / CDP) are orders of magnitude cheaper (a few thousand tokens after pruning) and replay-stable, but coverage is uneven on Electron/Qt/canvas/games.
- **Set-of-Marks / OmniParser** recovers structure from pixel-only UIs server-side.
- **Region/zoom and full frame** are the escalation rungs when structure is absent.

### Decision

A **screenshot-first baseline with structured upgrade** observation model:

- **Phase-0 baseline:** full-screen or focused-region **screenshot observations** with explicit coordinate space. This is the universal loop every GUI model can use.
- **Structured upgrade:** a normalized cross-OS **a11y/DOM tree diff** (AT-SPI/UIA/AX/CDP) projected onto one `Element{ref, role, name, value, states, bbox, source, …}` schema with **stable per-session refs**.
- **Vision grounding upgrade:** **Set-of-Marks / OmniParser**, server-side, on demand for screenshot-only or low-a11y surfaces.
- **Media escalation:** region/zoom pixels, full frame, and video for humans or pixel-heavy tasks.

Agents can act on raw `x,y` in the screenshot baseline; they should act on element refs / marks when structure is available. Target the ~6× token saving and replay-stable trajectories for tree-rich apps. The a11y-coverage assumption is **important and unverified**, but it is no longer a blocker for the first screenshot-based GUI loop (see Risks and [`../../notes/open-questions.md`](../../notes/open-questions.md)).

**`element_ref` resolution — settled 2026-06: SDK-side is the v0.0.1 reference path.** The structured tree is captured co-located with the SDK (`a11y.py` / CDP), and `element_ref` resolves to a click point **client-side** (`Sandbox.resolve`/`act_on` map a ref from the last structured observation to a `point_px`); the in-Sandbox shinkend executor deliberately **bails** on `element_ref` (`executor.rs`), because guest-side over-the-wire a11y resolution requires the native automation/accessibility backend ladder, which is **post-v0.0.1** (#96). This is honest in `status.md` and is the reference-implementation reading of the plan's local/reference scope — it ships the structured fast path for SDK-co-located setups now and defers the guest engine, rather than blocking v0.0.1 on ~a week of guest-side work.

**Sub-decision — extended 2026-06-11 (the operation layer, D13): stable element identity + diff observations + settle-before-observe.** The a11y-coverage spike (#2/E5) has now **measured** the coverage picture — strong Qt/AT-SPI (0.87 addressable) and Chromium-family-via-CDP, weak GTK, absent terminals, canvas measured at zero with a change-blind diff; tree-diff ~1–3% of a screenshot's bytes — so the structured-default upgrade **stays Provisional** and the committed shape is **hybrid per-window**. D3's structured track is accordingly deepened by three operation-layer commitments specified in [operation-layer.md](operation-layer.md) and owned by **D13**: (a) element ids (`Element.ref`) are **stable across observations within a session**, so re-observations emit `~/+/-` **diffs** (removed-id range summarization under a line budget, full-tree fallback) and stale-id failures carry a typed re-observe hint; (b) observation capture **settles** — debounce on accessibility change notifications until the UI quiesces, bounded by a deadline; (c) the **one observe primitive** returns both tiers (screenshot + tree + focus pointer), and the model picks the tier per step, pixels always the universal fallback. The identity/diff layer is named plainly as the hardest in-house component and the headline token/correctness optimization. All of this is **designed-only** today (the guest observation engine is unbuilt — see [status.md](../engineering/status.md)).

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Pixels-only forever** | Works universally but is the dominant cost driver; ~6× more tokens, no replay-stable refs, requires a grounding model for every click. Kept as the Phase-0 baseline, not the end state. |
| **a11y-only** | Fails on canvas/WebGL/games/Electron with poor trees; needs a pixel fallback. |
| **Single fixed modality** | Misses that the modalities are complementary; the right answer is escalation, not selection. |

### Consequences

- **Positive:** a usable GUI-agent runtime immediately via screenshots, then ~6× token reduction, ~150× bandwidth reduction with D4, replay-stable element refs, and model-agnostic grounding where structure exists.
- **Negative:** Shinken must build and maintain a cross-OS a11y normalizer (AT-SPI/UIA/AX/CDP → one schema) and a server-side SoM service.
- **Risks:** the unverified assumption is that real target apps expose usable a11y trees cheaply enough for structured observation to become the common fast path. Mitigation: instrument a representative app set (browser, Electron, native Win/macOS, canvas/WebGL, a game) and measure the fraction with usable trees + bandwidth before committing density/cost claims.

  Field priors from the 2026-06 reference teardowns sharpen spike #2's hypothesis and must be cited in its report: (a) trycua/cua's production Rust driver ships an a11y-first per-window ACI (indexed AX/UIA/AT-SPI trees) but embeds a small-tree heuristic that explicitly instructs the model "this app likely uses custom rendering (e.g. Blender, games, Electron) — use pixel clicks", plus a flag to force-enable Chromium/Electron AX trees — i.e. the shipped answer is **hybrid per-window with pixel fallback**, not structured-default (<https://github.com/trycua/cua>). (b) xlang-ai/CUA-Gym built a 32k-task verified dataset with an a11y passthrough available and **never called** — all verification runs on structured *file/app state* (openpyxl/python-docx reads, server-computed HTTP state_diffs) plus a budgeted vision judge (<https://github.com/xlang-ai/CUA-Gym>). Implication: the spike should evaluate a **guest state probe** (files/app state read in-guest) as a structured rung for *verification*, distinct from a11y-for-*acting*; the expected D3 outcome shifts from "structured-default where coverage is strong" toward "per-window hybrid for acting + guest-state for verifying".

### Evidence

- Anthropic vision token formula + caps: <https://platform.claude.com/docs/en/build-with-claude/vision>; ~6× a11y-vs-pixel token measurement: <https://fazm.ai/blog/benchmarked-ai-browser-tools-token-efficiency-native-apis>
- OmniParser V2 (SoM): <https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/>, <https://arxiv.org/abs/2408.00203>
- UFO2 (UIA+vision fusion): <https://arxiv.org/abs/2504.14603>; A11y-CUA accessibility gap: <https://arxiv.org/html/2602.09310>
- Detail in [`../../notes/streaming-bandwidth.md`](../../notes/streaming-bandwidth.md).

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
- **Risks:** glass-to-glass latency numbers are unverified pending a dual-channel PoC; jitter-buffer tuning is the make-or-break for "feels interactive." The headline ~150× bandwidth/cost advantage **depends on D3's structured-observation coverage**, which is itself provisional and gated on the a11y-coverage spike (#2): if structured coverage is poor and sessions fall back to Tier-2 pixels, the bandwidth win shrinks toward the video regimes (see D3 Risks and [`09-economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md) §5).

### Evidence

- WebRTC data channels (SCTP/DTLS): <https://datatracker.ietf.org/doc/html/rfc8831>; congestion control (TWCC/GCC): <https://bloggeek.me/webrtcglossary/transport-cc/>, <https://c3lab.poliba.it/images/6/65/Gcc-analysis.pdf>; latency breakdown: <https://transitiverobotics.com/blog/webrtc-latency-breakdown/>
- AV1 screen-content coding (~100 kbps class): <https://visionular.ai/av1-screen-content-coding/>; NICE DCV bitrate perspective: <https://aws.amazon.com/blogs/hpc/putting-bitrates-into-perspective/>
- rrweb DOM-mutation streaming model: <https://github.com/rrweb-io/rrweb>; session-recording overhead: <https://posthog.com/blog/session-recording-performance>
- Detail in [`../../notes/streaming-bandwidth.md`](../../notes/streaming-bandwidth.md).

---

## D5 — Runtime state + replay: event stream, snapshots, checkpoints, forks, and `.skn`

**Status:** Accepted.

### Context

Prior art converges on a small set of reusable primitives but no one ships the whole stack: OSWorld writes only `traj.jsonl` + `recording.mp4` (no time-travel); Morph has VM-level branch/time-travel but no agent-trajectory event timeline; Playwright trace is record-only. The format primitives are well understood — rrweb's typed/timestamped `eventWithTime` envelope with a discriminator enum; asciicast's JSONL header + `[time, code, data]` rows with `m` marker events as the scrub/branch anchor; Playwright's `trace.zip` packaging; OpenTelemetry GenAI semconv for the decision channel. **Bit-deterministic replay (rr-style) is x86/Linux-only and single-core** — infeasible cross-OS. And **fast snapshot-fork is now a commodity**: the same CoW-fork primitive that resets a sandbox also branches a trajectory.

### Decision

Shinken separates the evidence ledger from runnable state while linking them tightly:

- **`.skn` replay/data bundle** = the evidence ledger. It contains `manifest.json`, append-only
  **`events.jsonl`**, content-addressed media/resources, verifier receipts, permission events, and
  future `checkpoint_ref` / `snapshot_ref` markers. It answers "what happened?"
- **Snapshot** = provider/substrate state captured at a point in time: disk, memory, device state,
  process state, or provider-managed metadata depending on tier. It answers "what raw state can this
  substrate restore?"
- **Checkpoint** = Shinken's named restore point: substrate snapshot refs + logical event offset +
  optional agent-side state. Checkpoints form an immutable parent-pointer DAG. It answers "where can
  Shinken continue or branch?"
- **Fork** = create a new live Sandbox/run branch from a checkpoint. On the Linux fast-fork tier this
  is CoW memory/disk fork; on weaker tiers it may be unsupported or degrade to restore/recreate.
- **Resume/restore** = make a paused/snapshotted Sandbox live again. It answers "can this run
  continue?", which is distinct from viewing a `.skn` replay.
- **Checkpoint refs are first-class creation inputs.** `checkpoint()`/`snapshot()` returns a ref
  consumable anywhere a base image is accepted — `fork` is "create from checkpoint ref", one
  primitive rather than parallel checkpoint/restore APIs. Lifecycle guards are part of the contract:
  destroying a sandbox that has live checkpoints warns; deleting a checkpoint with live forks is
  refused or staged. (Prior art: trycua/cua's `snapshot()` returns the same `Image` type its
  `Sandbox.ephemeral()` consumes, with destroy-warning and auto-suspend guards —
  <https://github.com/trycua/cua>.)
- **Verifier-validation mode.** `run_eval_forked` supports a dual-fork agreement gate before a task
  enters an eval/training set: `score(fork(golden_checkpoint)) == 1.0` AND
  `score(fork(initial_checkpoint)) == 0.0`. This makes the most expensive invariant of automated
  task factories — two isolated environments per validation round (xlang-ai/CUA-Gym provisions
  2 fresh cloud VMs per task for exactly this — <https://github.com/xlang-ai/CUA-Gym>) — two forks
  of one boot.
- The **decision channel uses OpenTelemetry-GenAI** semantic conventions.
- **Not bit-deterministic** — a pragmatic **state-snapshot + event-log + observation-log** model.
- The `.skn` bundle doubles as **RL/SFT training data** (a supporting byproduct — D12), while
  checkpoints and forks make counterfactual reruns live.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Bit-deterministic record/replay (rr)** | x86/Linux-only, single-core; cannot span Windows/macOS or the multi-core desktop tier. |
| **Video-only recording (OSWorld `recording.mp4`)** | No scrub-to-action, no checkpoint references, no fork, no re-run; not training-grade. |
| **Agent-state-only checkpoint (LangGraph-style)** | Forks the agent but re-runs side effects against a live, drifted world; needs the env snapshot too. |
| **Mutable trajectory log** | Breaks branch provenance; the checkpoint DAG must be immutable and append-only. |

### Consequences

- **Positive:** the live stream becomes the replay/data ledger, while snapshots/checkpoints/forks make selected points runnable; time-travel and counterfactual re-runs are nearly free on tiers with shared-prefix CoW pages.
- **Negative:** side-effecting tool calls on a branch need record/mock or idempotency handling; a forked VM pins its backing memory file (snapshot GC complexity).
- **Risks:** event-schema versioning + upcasting must be specified (open question); not bit-deterministic means re-runs can diverge — that is by design and must be documented to users.

### Evidence

- rrweb envelope/types: <https://github.com/rrweb-io/rrweb/blob/master/packages/types/src/index.ts>; asciicast v2/v3 (markers, interval timing): <https://docs.asciinema.org/manual/asciicast/v2/>, <https://docs.asciinema.org/manual/asciicast/v3/>
- Playwright tracing/trace-viewer: <https://playwright.dev/docs/api/class-tracing>, <https://playwright.dev/docs/trace-viewer>
- OpenTelemetry GenAI semconv: <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/>
- Firecracker CoW branch + uniqueness: <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md>; Tree-GRPO (branching for RL): <https://arxiv.org/abs/2509.21240>
- Detail in [`../../notes/replay.md`](../../notes/replay.md).

---

## D6 — Capability scoping (a supporting runtime feature)

**Status:** Accepted (mostly designed). A Sandbox is granted the runtime resources its task
needs and nothing more. This is resource scoping / entitlement management — supporting runtime
plumbing, not a headline differentiator.

### Context

A Sandbox is powerful *inside* its isolation boundary by design — install packages, edit files,
drive the UI, run code — because it is a disposable guest. What is worth managing is only what a
Sandbox is *granted at the boundary*: network egress, filesystem scope, clipboard, GPU,
privileged install, persistence, credentials, peripheral / OS automation. The permission model
must not turn ordinary in-sandbox actions into approvals; it only scopes the boundary grants a
task declares.

### Decision

A Sandbox carries an explicit, default-empty **capability envelope** over a small set of classes
(`net.egress`, `fs.scope` / host mounts, `clipboard`, `gpu`, `install.privileged`, `persistence`,
`credentials`, `peripheral` / OS automation). In-sandbox power is expected and unscoped; only
these boundary grants are scoped, time-boxed, and recorded as events (D5). Human approval is for
the exceptional boundary grant, never the hot path. Server-side resolution of the envelope and
credential brokering are control-plane concerns (D9).

### Status today vs designed

- **Built (reference):** a client-side capability-gateway shim records the granted envelope and
  routes each action allow / ask / deny (`sdk/python/src/shinken/gateway.py`) — the eval/audit
  surface, not an enforcement guarantee.
- **Designed (D9):** server-side resolution so an ungranted action is simply not dispatched, plus
  OS-level scoping (Linux Landlock/seccomp/cgroups, macOS sandbox profiles, Windows tokens) where
  the substrate supports it, and credential brokering. The concrete engine is deferred with the
  control plane.

### Consequences

- **Positive:** runs declare the resources they touch; boundary grants are scoped and auditable;
  composes with the runtime-state and eval layers without special-casing.
- **Negative / deferred:** server-side resolution, cross-OS scoping divergence, and credential
  brokering are designed-only; the reference shim is advisory.

See the [isolation & capability note](threat-model.md) for how the isolation and capability
boundaries fit together.


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
- Detail in [`../../notes/eval-benchmarks.md`](../../notes/eval-benchmarks.md) and [`03-osworld-analysis.md`](osworld-analysis.md).

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
- Detail in [`../../notes/ai-native-interface.md`](../../notes/ai-native-interface.md).

---

## D9 — Control plane: Fleet Manager + Action Gateway

**Status:** Accepted.

### Context

Running a computer-use platform at ultra-high concurrency is a **fleet-of-idle-VMs economics problem**. The industry has converged: don't cold-boot per request — keep **warm pools** and **fork/restore from memory snapshots**. The OSS **`kubernetes-sigs/agent-sandbox`** SIG project has standardized the control-plane primitives (`Sandbox`, `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool` CRDs) plus pod-snapshot suspend/resume and a cheap suspended cold pool that replenishes the warm pool (a published reference cites ~300 sandboxes/s/cluster, p90 ~200 ms — vendor-published, unverified). **Idle time dominates cost**, so auto-suspend-to-snapshot on idle is the central lever.

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
- **Risks:** warm-pool exhaustion behavior and snapshot-store growth/GC need explicit SLOs (see [`09-economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md)).

### Evidence

- Agent Sandbox on GKE (CRDs, throughput): <https://cloud.google.com/blog/products/containers-kubernetes/bringing-you-agent-sandbox-on-gke-and-agent-substrate>; agent-sandbox SIG (Kata): <https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/kata-containers/>; production overview: <https://northflank.com/blog/agent-sandbox-on-kubernetes>
- Firecracker warm-resume + page faults: <https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md>; OTel GenAI agent spans: <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/>
- Detail in [`../../notes/sandbox-infra.md`](../../notes/sandbox-infra.md).

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
- **Risks:** Windows-in-cloud licensing and the macOS 2-VM/host cap shape cost and roadmap (see [`09-economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md)); a11y coverage differs per OS (D3 risk).

### Evidence

- cua cross-platform + in-guest server: <https://github.com/trycua/cua>, <https://cua.ai/docs/lume/guide/getting-started/introduction>; Windows Sandbox provider: <https://cua.ai/blog/windows-sandbox>
- Host↔guest transport (no Windows vsock driver): <https://github.com/cloud-hypervisor/cloud-hypervisor/discussions/5431>; Windows golden image + cloudbase-init: <https://deepwiki.com/cloudbase/windows-imaging-tools/5-configuration-reference>
- macOS substrate (Tart/lume, APFS CoW): <https://tart.run/quick-start/>, <https://cua.ai/docs/lume/guide/getting-started/introduction>
- Detail in [`../../notes/sandbox-infra.md`](../../notes/sandbox-infra.md).

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
| **GPU on by default** | Most tasks are CPU-only; GPU is expensive, snapshot-resistant, and scarce. Keep it opt-in and the fork tier the default. |
| **Encode on A100/H100 (the AI fleet)** | Zero NVENC engines; silently forces CPU software encode (x264/SVT-AV1) — high latency, low density. Decision-changing constraint. |
| **MIG for the encode tier** | MIG doesn't surface NVENC on A100/H100; Ada (L4/L40S) has no MIG. Use vGPU time-slicing or app-level packing. |
| **Raw time-slicing / MPS for untrusted agents** | No memory/fault isolation; one tenant can OOM or crash the whole GPU. Must sit inside a vGPU/MIG/VM boundary. |
| **VFIO passthrough for high concurrency** | 1 GPU = 1 VM (worst density) and blocks snapshot/migration. Only for rare single-tenant max-perf agents. |
| **Forcing AV1 without negotiation** | AV1 HW decode is still limited; forcing it yields jank or a black screen. Negotiate AV1 → HEVC → H.264. |

### Consequences

- **Positive:** a credible GPU-accelerated tier (3D/WebGL/CUDA/heavy-render) plus a high-fidelity NVENC pixel channel and an enterprise trusted-compute variant — a differentiator no Firecracker-only competitor can match; the expensive, snapshot-resistant pool stays small.
- **Negative:** the GPU tier cannot fast-fork (a major architectural asymmetry); vGPU is a licensed product whose per-concurrent-user cost can dominate GPU TCO; the mdev→vendor-VFIO/SR-IOV transition (kernel 6.8+) makes guest-driver installs fragile and version-matrix-sensitive.
- **Risks:** per-card density numbers are vendor best-case (e.g. ~130 AV1 720p30 streams/L4 is P1 preset at 720p — vendor-published, unverified) and must be benchmarked at Shinken's resolution/FPS/preset; GPU-TEE + NRAS maturity for agent workloads in 2026 is the unverified enterprise-wedge assumption.

### Evidence

- A100/H100 zero NVENC, qualified-GPU no-cap, MIG media-engine allocation: NVENC Application Note <https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html>; NVENC per-GPU encoder generations <https://en.wikipedia.org/wiki/Nvidia_NVENC>; MIG profiles <https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html>
- AV1 ~40% savings + preset latency-invariance + L4 density: <https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/>, <https://www.nvidia.com/en-us/data-center/l4/>
- vGPU vs MIG vs time-slicing + scheduling + A40 32-user cap: <https://research.colfax-intl.com/sharing-nvidia-gpus-at-the-system-level-time-sliced-and-mig-backed-vgpus/>, <https://docs.nvidia.com/ai-enterprise/release-8/latest/infra-software/vgpu/features/scheduling.html>, <https://www.nvidia.com/en-us/data-center/a40/>
- VFIO/vGPU non-snapshottable: <https://forum.proxmox.com/threads/cannot-snapshot-vm-vfio-migration-not-supported.179190/>; CRIU GPU restore (Modal): <https://modal.com/blog/gpu-mem-snapshots>
- NICE/Amazon DCV (NVENC + QUIC + browser): <https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html>, <https://aws.amazon.com/blogs/gametech/stream-remote-environment-nice-dcv-quic-udp-4k-monitor-60-fps/>; GStreamer nvcodec build path: <https://gstreamer.freedesktop.org/documentation/nvcodec/nvautogpuh264enc.html>; capture (NVFBC deprecation on Windows): <https://developer.nvidia.com/capture-sdk>
- Detail in [`09-economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md).

---

## D12 — Business / positioning: open self-hostable core + optional hosted commercial layer

**Status:** Accepted.

### Context

The market punishes closed single-modality products (Scrapybara sunset). The open-source competitors (E2B, trycua/cua, OSWorld, HUD) thrive; the proprietary players (Anthropic Computer Use, OpenAI Operator, Browserbase) ship the model or the host but not an open, self-hostable, cross-platform runtime. First users are teams **building and evaluating** computer-use agents (model labs, researchers, RPA builders), and the `.skn` replay (D5) doubles as RL/SFT trajectory data — a supporting byproduct of the runtime-state wedge, never the headline.

### Decision

- **Open, self-hostable core** + a reusable Operator + an open, provider-agnostic agent loop (no lock-in).
- An **optional hosted commercial layer**: the Control Panel, observability, permission-audit, and eval as a service.
- **North star:** ONE platform for production *and* eval, layered.
- **First users:** CUA model/eval teams, with the **runtime-state wedge**: cheap golden-checkpoint →
  fork-N replicas and deterministic reset that no shipped harness has — as of 2026-06, cua's CoW fork
  is cloud-only and unused by its own bench, and uni-agent/Agentix/CUA-Gym all pay cold-boot or
  fresh-VM costs per rollout (see [landscape](landscape.md), trainer-side capsules). Trajectory/`.skn`
  export is the supporting byproduct that turns those runs into training data, never the headline.
- **Vendor-neutral**: runs on any Kubernetes/cloud; **optimized for NVIDIA GPUs where present** (D11), never dependent on them.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Closed/proprietary product** | The market punishes closed single-modality CUA products; open core drives adoption. |
| **Open everything, no commercial layer** | No sustainable funding for the hosted Control Panel / audit / eval; the differentiator is the panel-as-product. |
| **Vendor-locked (NVIDIA-only)** | Contradicts the vendor-neutral mandate; GPU is an acceleration option, not a dependency. |
| **Production-only or eval-only** | The north star is one platform serving both, layered — eval is thin orchestration on the production runtime (D7). |

### Consequences

- **Positive:** adoption with no lock-in; the runtime-state wedge gives a concrete first-user hook (with trajectory export as the supporting byproduct); the hosted layer monetizes the category-defining panel; runs anywhere, faster on NVIDIA.
- **Negative:** open core means competitors can fork; the commercial layer must stay genuinely valuable (panel, audit, eval, observability) to fund development.
- **Risks:** balancing open vs hosted feature lines; sustaining cross-OS + GPU engineering on an open-core model.

### Evidence

- Competitive landscape (open vs hosted, Scrapybara sunset, cua/E2B/HUD): <https://github.com/trycua/cua>, <https://www.hud.ai/resources/best-platforms-publishing-rl-environments-model-labs>
- Replay-as-RL-data: Tree-GRPO <https://arxiv.org/abs/2509.21240>; rollout-as-a-service <https://arxiv.org/html/2603.18815v1>
- Full positioning in [`00-vision.md`](vision.md), [`01-prd.md`](prd.md), and [`04-landscape.md`](landscape.md).

---

## D13 — Operation layer: one observe contract, stable element identity, and an element verb family

**Status:** Accepted (phased). The contract is committed design canon (the full specification is
[`operation-layer.md`](operation-layer.md)); the guest-side observation engine, the new verbs, and
the per-OS engines are **designed-only, not built** ([status.md](../engineering/status.md)). The
coverage evidence underneath it is first-party (spike #2/E5).

### Context

The a11y-coverage spike (E5) replaced the "structured-by-default?" question with a measured answer:
coverage is **uneven per window** (Qt 0.87 addressable, Chromium page content fully resolvable over
CDP, GTK ~0.10, terminals zero, canvas zero *with a change-blind diff*), while a stable-frame tree
diff costs ~1–3% of a screenshot's bytes. That verdict makes the per-step loop mechanics — not the
modality choice — the open design problem: how an agent gets both tiers without paying for both
every step, how element targets stay valid across steps, how observation timing avoids half-painted
UIs, and how round trips are minimized. D2's tagged-union and D3's layered observation are the
right frames but underspecify this **operation layer**.

### Decision

One operation-layer contract, fully specified in [`operation-layer.md`](operation-layer.md):

1. **One observe primitive, two perception tiers bound together** — every observation can carry the
   screenshot *and* the structured element tree *and* a focus pointer; the model picks the tier per
   step; pixels are the universal fallback.
2. **Stable element identity + diff observations** — interactable ids stable across observations
   within a session (never reused, never migrated); re-observations emit `~/+/-` diffs with
   removed-id range summarization under a line budget and a full-tree fallback; stale-id failures
   are typed and carry a re-observe hint. Stated plainly: this identity/diff layer is the hardest
   in-house component of the operation layer and the headline token/correctness optimization.
3. **Settle-before-observe** — debounce on accessibility change notifications until the UI
   quiesces (quiet window + deadline), reported as `settle{quiesced, waited_ms}`.
4. **Act-returns-observation** — every mutating action MAY return a fresh settled observation
   (opt-in argument); the recommended loop is one-observe-per-turn.
5. **Per-app/window scoping** — actions/observations target an app (name/bundle-id/path/pid;
   ambiguous names rejected with candidates) and its key window; capture defaults to the key window
   and reuses the existing fidelity knobs (`scope`, `max_long_edge`, `format`/`quality`).
6. **The element verb family** — additive over the v0 enum, capability-negotiated: `click` by
   element-id or coordinates (existing), `drag`, `invoke_element_action` (named secondary action),
   `set_element_value`, `set_text_selection` (select / caret-before / caret-after with
   prefix/suffix disambiguation), `scroll_element` (by pages), `send_keys` (= existing `key`,
   xdotool keysym chords), `enter_text` (= existing `type_text`), `observe`, and the
   `enumerate_apps`/`list_windows` read surface on the query channel.
7. **Physical events first** — pointer/keyboard verbs resolve elements to geometry and synthesize
   real OS input (XTEST/CGEvent/SendInput); accessibility-interface invocation is the fallback (and
   the primary mechanism only for the inherently element-interface verbs). This amends the earlier
   semantic-first router priority in [aci-spec.md](aci-spec.md) §3.2.
8. **Legible serialization grammar** — numbered, dot-indented lines with role + states +
   title/desc/value + secondary actions and a focus trailer; the grammar is versioned with the ACI.
9. **Per-app hint packs** — optional curated preambles injected once per app per session, recorded
   in the observation.

The Browser Runtime (a browser-specialized runtime preferring locator scripts / semantic node-ids /
CDP pixels for web tasks) is outlined in the same document as designed/phase-next.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Two observe verbs (pixel vs structured), harness picks the mode** | The harness can't know per step which tier a window rewards; the spike shows tier quality is per-window and per-moment. Bind both into one observation and let the model choose. |
| **Full tree every observation** | Pays the full serialization cost every step; the measured win (~1–3% of screenshot bytes) comes from diffs, which require the identity layer anyway. |
| **Per-observation ids (no stability)** | Every prior step's reasoning dangles; diffs are impossible; replay loses element-level continuity. Stability is what makes the tree a *state* rather than a rendering. |
| **Best-effort id reuse without typed staleness** | A stale id silently resolving to a wrong control is a confidently wrong click — worse than a failure. Stale must be a typed, hinted error. |
| **Fixed post-action sleeps** | The OSWorld anti-pattern (D7 bans it for eval); event-driven quiescence is cheaper and honest (`quiesced:false` when the deadline hits). |
| **Semantic-first actuation (the previous router default)** | Programmatic invocation skips hover/focus/animation paths and behaves un-human in exactly the apps agents must master; physical events at resolved geometry are higher-fidelity, with the element interface kept as fallback. |
| **Screen-scoped observation only** | Coverage and diff baselines are per-window properties; screen scope drags every background app into both tiers' cost. |

### Consequences

- **Positive:** the minimum-token steady-state loop (*act → settled diff → act*) with element-level
  continuity; correctness levers (typed staleness, settle reporting, ambiguous-app rejection) built
  into the contract; the verb family is additive behind capability negotiation, so old runtimes and
  clients are untouched.
- **Negative:** the identity/diff engine is genuinely hard in-house work (platform node identity is
  unstable everywhere except CDP); the serialization grammar becomes a versioned model-facing
  contract; per-OS engines must each implement settle + identity faithfully.
- **Risks:** (1) identity mis-binding produces wrong-target actions — mitigation: conservative
  matching that mints new ids when unsure, plus typed staleness; (2) diff quality on
  change-blind surfaces (canvas, measured zero) — mitigation: the pixel tier is always present in
  the same observation; (3) line budgets and quiet windows are tuning defaults, not measured —
  first-party loop-level token/latency measurements must precede any SLA-grade claim.

### Evidence

- First-party coverage + diff-bandwidth measurements: [`../engineering/spike-a11y-coverage.md`](../engineering/spike-a11y-coverage.md), [`../../spikes/a11y-coverage/`](../../spikes/a11y-coverage)
- Platform element/event APIs: AT-SPI2 <https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/>; UI Automation <https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32>; AXUIElement <https://developer.apple.com/documentation/applicationservices/axuielement_h>; CDP Accessibility/DOM/Input <https://chromedevtools.github.io/devtools-protocol/>
- Actionability/auto-wait discipline (the settle prior art): Playwright <https://playwright.dev/docs/actionability>
- Keysym chord notation: xdotool <https://github.com/jordansissel/xdotool>
- Full specification: [`operation-layer.md`](operation-layer.md)

---

## D14 — macOS engine substrate: ScreenCaptureKit + AXUIElement + CGEvent under TCC

**Status:** Accepted (phased). Designed-only — no macOS runtime is built (the proven slice is
Linux/X11); this ADR fixes the API substrate the Phase-3 macOS engine implements, so the operation
layer (D13) is specified against real platform surfaces. Readiness analysis:
[macOS readiness spike](../engineering/spike-macos-readiness.md).

### Context

macOS is the second OS the operation layer targets (D10 sequences it with Phase 3). The platform
offers exactly one modern sanctioned stack for each pillar: ScreenCaptureKit for capture (the
legacy CGWindowList paths return blank content without consent and are deprecated), the AXUIElement
API for reading other apps' element trees, and CGEvent synthesis for input — all gated by **TCC**
(Transparency, Consent, and Control) user grants: **Screen Recording** for capture, **Accessibility**
for tree reads and input synthesis. Chromium-family and Electron apps build their renderer
accessibility tree lazily, only when something asks for it.

### Decision

The macOS engine is built on three public-API pillars under the platform consent model:

- **Capture = ScreenCaptureKit**: `SCScreenshotManager`/`SCShareableContent` for one-shot
  key-window and screen screenshots (per-window capture including occluded windows); `SCStream`
  reserved for the later screencast path.
- **Tree = AXUIElement**: walk the target app's AX tree into the normalized `Element` schema; for
  Chromium/Electron targets, set the app-level accessibility-enable attributes
  (`AXManualAccessibility`, with `AXEnhancedUserInterface` as the compatibility path) so renderer
  content is exposed without a screen reader attached.
- **Input = CGEvent synthesis**, posted per-pid where targeting allows — the physical-events-first
  policy of D13, with AX action/value invocation as the fallback.
- **TCC posture**: the engine runs on user-granted (or managed-profile pre-granted) Screen
  Recording + Accessibility; while grants are pending, **observe returns a typed keep-alive
  observation** naming the missing grants instead of failing, so a session survives the human
  grant flow. No bypass of the consent model is in scope, by design.

### Alternatives (and why rejected)

| Alternative | Why rejected |
|---|---|
| **Legacy capture (CGWindowList/CGDisplayStream)** | Deprecated path; returns blank/placeholder content without consent; SCK is the sanctioned, per-window-capable replacement. |
| **AppleScript/Apple Events as the actuation layer** | Consent is tracked per (source→target) app pair and per-app dialect coverage is wildly uneven; kept as a separately-gated capability, never the baseline loop. |
| **Failing the session while TCC grants are pending** | Turns a one-time human setup step into an infra failure class; a typed keep-alive observation keeps the loop honest and alive. |
| **Skipping the Chromium/Electron AX enable attributes** | The renderer tree simply isn't built; the spike's Electron evidence shows page content appears only when accessibility is explicitly enabled. |

### Consequences

- **Positive:** the operation layer's macOS behavior is specified against the only sanctioned
  modern APIs; per-window capture (including occluded) matches the key-window scoping default
  (D13); pool images can be made automation-ready via managed pre-grant.
- **Negative:** the engine inherits TCC's operational reality — unmanaged images need an
  interactive first-run; grants bind to the signing identity, so the runtime must ship signed with
  a stable identity.
- **Risks:** Chromium/Electron AX-enable behavior varies across app versions (the attributes are
  honored by convention, not contract) — the pixel tier in the same observation is the safety net.

### Evidence

- ScreenCaptureKit: <https://developer.apple.com/documentation/screencapturekit>
- AXUIElement: <https://developer.apple.com/documentation/applicationservices/axuielement_h>
- CGEvent: <https://developer.apple.com/documentation/coregraphics/cgevent>
- TCC preflight/grant APIs: <https://developer.apple.com/documentation/coregraphics/3656523-cgpreflightscreencaptureaccess> · <https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrusted>
- Electron/Chromium accessibility enablement: <https://www.electronjs.org/docs/latest/tutorial/accessibility> · <https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/overview.md>
- Readiness matrix (grants, pre-bake, clone behavior): [`../engineering/spike-macos-readiness.md`](../engineering/spike-macos-readiness.md)

---

## Cross-cutting reconciliation

The fourteen decisions interlock; the load-bearing couplings:

- **One CoW-fork primitive serves three masters:** instant reset (D1), replay-branching (D5), and N-replica eval (D7). Build it once, expose `fork/branch/restore` as first-class verbs.
- **The structured event stream is one artifact in three roles:** the live stream (D4), the replay log (D5), and RL/SFT data (D12).
- **The `tool_runner` boundary is where D2 and D6 meet:** code-as-action and every privileged action route through the controlled API that enforces the Cedar decision and the egress allowlist before executing.
- **The Action Gateway (D9) is the single place D6 is enforced:** auth → rate → budget → Cedar → dispatch.
- **GPU (D11) is the one tier that breaks the fork invariant (D1):** it is snapshot-light by physics (VFIO/vGPU state is non-migratable), opt-in, and the encode hardware must be physically separate from any A100/H100 AI fleet.
- **The operation layer (D13) is where D2 and D3 meet at the per-step loop:** the element verb family extends the D2 tagged-union, and the stable-id/diff/settle observation engine is how D3's structured track is actually consumed; the per-OS engines (Linux today, macOS per D14, Windows per D10) implement one contract.
- **a11y coverage (D3)** — the formerly load-bearing unverified assumption — is now **first-party measured** (spike #2/E5: hybrid per-window verdict; D3's structured-default stays Provisional); **every remaining "(vendor-published, unverified)"** number in this document still requires the first-party measurement plan before it anchors an SLA.

Open questions carried forward (do not paper over): a11y coverage on Electron/Qt/canvas/games; Windows-in-cloud licensing and the macOS 2-VM/host economics; no first-party perf numbers yet; the isolation & capability note ([`threat-model.md`](threat-model.md)); the multi-player / non-exclusive computer-use in/out decision; and protocol/event-schema versioning + upcasting. See [`../../notes/open-questions.md`](../../notes/open-questions.md).
