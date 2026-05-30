# 07 — Glossary

Shared vocabulary for the Shinken design corpus. This is the canonical reference for every term used in the sibling docs ([Vision](00-vision.md), [PRD](01-prd.md), [Architecture](02-architecture.md), [OSWorld teardown](03-osworld-analysis.md), [Landscape](04-landscape.md), [Tech Decisions](05-tech-decisions.md), [Roadmap](06-roadmap.md), [Threat Model](08-threat-model.md), [Economics & Build-vs-Buy](09-economics-and-build-vs-buy.md)). Decisions are referenced by their **D-number** (see [05 — Tech Decisions](05-tech-decisions.md)) so each definition reconciles to the choice that owns it. Speed/density/cost numbers are tagged **(vendor-published, unverified)** — no first-party Shinken measurements exist yet, and a measurement spike is a tracked open question (see [Roadmap](06-roadmap.md) and [notes/open-questions.md](../notes/open-questions.md)). Date of record: **2026-05-30**.

Terms are alphabetical. Cross-references use *italics*. External sources are cited inline by URL and collected in [notes/sources.md](../notes/sources.md).

---

### accessibility tree (AT-SPI / UIA / AX)

The OS-native, structured representation of the on-screen UI: a hierarchy of elements carrying role, name, value, state, and bounding box. Shinken's observation model (**D3**) normalizes the platform sources — **AT-SPI** (Linux GTK/Qt), **UIA** (Windows UI Automation), **AX** (macOS `AXUIElement`), and *CDP* for browsers — into one `Element{ref, role, name, value, states, bbox, source, ...}` schema with stable per-session refs. This "structured-first" rung is the basis for the ~6× token saving (~25k vs ~150k tokens/task, vendor-published, unverified) and is replay-stable. **Caveat:** a11y coverage on Electron/Qt/canvas/games is the load-bearing *unverified* assumption (see [notes/open-questions.md](../notes/open-questions.md)) and needs a measurement spike. CDP accessibility domain: [chromedevtools.github.io/devtools-protocol](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/).

### ACI (Agent-Computer Interface)

The versioned protocol plus typed action/observation schema that every agent uses to drive a *Sandbox* (**D2**). The action schema is one canonical tagged-union discriminated by `verb` (~16 verbs), with `target = oneof{ point_px | point_norm | element_ref }` and an explicit *CoordinateSpace* on every observation. It is semver-versioned with capability negotiation at handshake, and exposes **version-pinned bidirectional adapters** as the only model-facing surface (Anthropic `computer_2024xxxx/2025xxxx`, OpenAI `computer_call`, UI-TARS, OSWorld `computer_13`). The ACI is the seam Shinken differentiates on: a *structured-first, pixels-on-demand* observation model rather than screenshot polling. Adapter targets: [Anthropic computer use](https://code.claude.com/docs/en/computer-use), [OpenAI computer-use tool](https://developers.openai.com/api/docs/guides/tools-computer-use).

### Action Gateway

The single choke point in the *Control Plane* through which every privileged action passes (**D9**). Pipeline: tenant-auth → token-bucket / weighted-fair-queue rate-limit → combined budget → *Cedar* policy decision → dispatch. Centralizing this makes auth, rate-limiting, and policy one auditable surface instead of scattered checks. Aligns with the generic *tool_runner boundary*.

### Capability

A typed permission class — the unit the *Permission Panel* grants or denies (**D6**). Shinken defines **8 classes**: `net.egress`, `fs.scope`, `clipboard`, `gpu`, `install.privileged/sudo` (the headline "unlock"), `persistence`, `credentials`, `peripheral`. Each request is evaluated against **4 risk tiers** — Auto / Notify / Ask / Block — and is *taint-aware* (see *taint-tracking*).

### CDP (Chrome DevTools Protocol)

The browser automation/inspection protocol used to extract the DOM accessibility tree and drive in-page actions for browser targets. In Shinken, CDP is the browser branch of the unified *accessibility tree* (**D3**); it is a structured observation source, not a separate transport. Spec: [chromedevtools.github.io/devtools-protocol](https://chromedevtools.github.io/devtools-protocol/tot/Page/).

### Cedar

The declarative policy language/engine that forms layer 1 of Shinken's 3-layer permission model (**D6**). Chosen over OPA/Rego because Cedar policies are formally verifiable (the policy set can be analyzed by tooling) and evaluate sub-millisecond. Cedar renders the *decision* ("is this action allowed?"); enforcement happens below it in the *ocap/caretaker* handle layer and the OS layer. References: [Cedar policy syntax](https://docs.cedarpolicy.com/policies/syntax-policy.html), [AWS Cedar analysis tooling](https://aws.amazon.com/blogs/opensource/introducing-cedar-analysis-open-source-tools-for-verifying-authorization-policies/).

### Confidential Containers

The CNCF project that runs Kata-isolated pods inside a hardware *GPU-TEE*, so guest data and model weights are protected and attested even from the host. In Shinken, Confidential Containers is the substrate for the *trusted* variant of the optional GPU tier, paired with *NRAS* attestation (**D1**, **D11**) — one public technology option, not a dependency. Reference: [NVIDIA GPU Operator confidential-containers guide](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/24.9.2/gpu-operator-confidential-containers.html).

### Control Plane

The server-side orchestration layer (**D9**). Components: the Sandbox *Fleet Manager* (warm pools + fork-on-demand), the *Action Gateway*, the scheduler, the replay store, the eval service, and OpenTelemetry GenAI telemetry. Distinct from the *Control Panel*. Sandbox health is treated as a circuit-breakable dependency — kill and replace from the warm pool. Telemetry semconv reference: [opentelemetry.io GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/).

### Control Panel

The human-facing web UI: live structured + on-demand video view, *Permission Panel* approvals, replay scrubbing/branching, cross-session search, and takeover. It is a category-defining surface for Shinken and the optional commercial layer in the open-core split (**D12**). Do not confuse with the *Control Plane*.

### CoW (copy-on-write)

The memory/disk-sharing technique behind instant *fork-from-snapshot*. A child *Sandbox* shares the parent's pages read-only (RAM via `MAP_PRIVATE` mmap; disk via qcow2/overlay) and copies a page only on write, so N parallel replicas cost roughly the divergent pages plus a small constant each, instead of a full VM each. This is what makes high-concurrency forking and replay-branching cheap (**D1**, **D5**). Background: [Firecracker snapshot system](https://deepwiki.com/firecracker-microvm/firecracker/5-snapshot-system).

### control / event / media planes

The three logical planes of the streaming *ACI* (**D4**). **Control plane** — session lifecycle and signaling. **Event plane** — the reliable-ordered WebRTC data channel carrying actions + observations + permissions; *this stream IS the replay log*. **Media plane** — the on-demand *NVENC* video track, attached only when pixels are requested. Note this is the *streaming* plane terminology and is separate from the *Control Plane* / *Control Panel* server-side split.

### CoordinateSpace

The explicit declaration, carried on every observation, of the coordinate frame an action target lives in (e.g. raw device pixels vs normalized 0–1 vs an element ref). Required by **D2** so that the pixels the model reasons over equal the pixels the runtime acts on; it removes the resolution-sensitivity and clamping bugs seen in pixel-coordinate computer-use models.

### Fleet Manager

The *Control Plane* component that owns *Sandbox* supply (**D9**): per-image/region/tier warm pools, fork-on-demand from the warm-parent snapshot, and cold-pool replenish. It is shaped like the OSS *kubernetes-sigs/agent-sandbox* CRD. It also runs the dual-timer session lifecycle (idle ~15 min reset-on-activity; max-lifetime ~4–8 h; auto-suspend-to-snapshot on idle, since idle is the dominant cost).

### fork-from-snapshot

Shinken's reset and branching primitive (**D1**, **D5**): instead of terminate-and-reboot (OSWorld's model), a new *Sandbox* is forked from a warm parent snapshot via *CoW* + `userfaultfd` lazy paging, with a post-fork uniqueness hook (reseed RNG / MAC / hostname / boot-id). Target <30 ms VMM restore and sub-second time-to-first-action. **Instant reset and replay-branching are the same primitive** — forking a snapshot node. Published reference point: Morph Infinibranch reports P99 ~1.3 ms fork with ~93% shared pages (vendor-published, unverified, [morph.so/blog/infinibranch](https://www.morph.so/blog/infinibranch/)).

### GPU-TEE

A GPU trusted execution environment — confidential computing extended to the GPU so guest data and model weights stay encrypted/attested even from the host. It is the trusted-substrate option for Shinken's optional GPU tier (**D1**, **D11**), used together with *Confidential Containers* and *NRAS* — a public NVIDIA technology option, not a Shinken dependency. Reference: [NVIDIA GPU Operator confidential-containers guide](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/24.9.2/gpu-operator-confidential-containers.html).

### Guest Runtime (`shinkend`)

The in-Sandbox daemon (process name `shinkend`) that executes the *ACI* and emits the *event*-plane stream. It replaces OSWorld's Flask `main.py` full-PNG-polling server. There is **one** Guest Runtime contract across all guest OSes, with a per-OS handler-factory beneath it (**D10**). Host↔guest transport is *virtio-vsock*, never HTTP polling (**D4**).

### kubernetes-sigs/agent-sandbox

The OSS Kubernetes SIG **agent-sandbox** CRD: the emerging standard pattern for running agent sandboxes as pods under hardened runtime classes (gVisor / Kata) with pre-warmed pools. Shinken uses its shape for the Linux container-tier *Substrate* and the *Fleet Manager* (**D1**, **D9**) rather than reinventing sandbox orchestration. References: [agent-sandbox.sigs.k8s.io](https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/kata-containers/), [Agent Sandbox on GKE](https://cloud.google.com/blog/products/containers-kubernetes/bringing-you-agent-sandbox-on-gke-and-agent-substrate).

### MIG (Multi-Instance GPU)

An NVIDIA hardware partitioning feature (up to 7 slices on H100) that splits one physical GPU into isolated instances with dedicated memory and engines. In Shinken, **MIG-backed** is one of the two GPU pools (**D11**): the isolation-sensitive/trusted pool (paired with *Confidential Containers* / *GPU-TEE*), and the per-session GPU *Capability* the *Permission Panel* hands out. **Not used for the encode tier** (no MIG for *NVENC*). Reference: [nvidia.com Multi-Instance GPU](https://www.nvidia.com/en-us/technologies/multi-instance-gpu/), [MIG user guide](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest/).

### NICE DCV

A high-fidelity remote-desktop protocol (hardware encode + QUIC transport) available as a publicly shipped NVIDIA/AWS product. Shinken treats **NICE DCV as the build-vs-buy option for the high-fidelity pixel channel** on the *media plane* rather than mandating a custom pipeline (**D4**, **D11**). It is one option; the default path is a custom WebRTC + *NVENC* pipeline. Reference: [docs.aws.amazon.com/dcv](https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html), [NICE DCV QUIC/UDP 4K streaming](https://aws.amazon.com/blogs/gametech/stream-remote-environment-nice-dcv-quic-udp-4k-monitor-60-fps/).

### NRAS (remote attestation for GPU TEE)

NVIDIA's remote attestation service for GPU TEE state — it proves to a relying party that a GPU is in a genuine confidential mode before secrets or weights are released. It is the attestation half of Shinken's optional trusted GPU tier, alongside *GPU-TEE* and *Confidential Containers* (**D1**, **D11**) — a public technology option, not a dependency. Reference: [NVIDIA GPU Operator confidential-containers docs](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/24.9.2/gpu-operator-confidential-containers.html).

### NVENC

NVIDIA's dedicated hardware video-encode engine, used by Shinken for the on-demand H.264/AV1 *media plane* (**D4**, **D11**). Critical constraint: **the encode tier never runs on A100/H100/H200/B200 (zero NVENC engines)** — use **Ada L4** (density) or **L40S** (premium 4K/AV1 + render). The consumer 8-session cap does not apply to datacenter GPUs. AV1 NVENC saves ~40% bitrate vs H.264 at high frame rates on Ada (vendor-published, unverified). References: [NVENC application note](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html), session-cap discussion at [Nvidia NVENC (Wikipedia)](https://en.wikipedia.org/wiki/Nvidia_NVENC).

### ocap / caretaker

The object-capability layer (layer 2 of the permission model, **D6**) sitting between *Cedar* and the OS. A *caretaker* (membrane) wraps a granted capability handle so it can be revoked in O(1) — instant revoke without re-evaluating policy. Cedar decides; the caretaker holds the handle and can sever it. Background: [object-capability model](https://en.wikipedia.org/wiki/Object-capability_model), [caretaker/revocation patterns](https://tersesystems.github.io/ocaps/guide/management.html).

### Operator

The client-side adapter that drives a *Sandbox* for a given agent/model, and the seam for human takeover. The agent loop is **provider-agnostic behind the Operator contract** — version-pinned bidirectional adapters (Anthropic, OpenAI, UI-TARS, OSWorld `computer_13`) are the only model-facing surface (**D2**). The contract is roughly: observe/screenshot, `execute(action)`, `supportedActions()`, `screenContext()`, with coordinate normalization done in the Operator boundary rather than in the model.

### OSWorld / OSWorld-Verified

**OSWorld** is the de-facto computer-use benchmark (369 tasks, ~50× parallel on cloud VMs, ~1 h runs) and Shinken's spiritual predecessor — Shinken positions itself as a "production-grade, streaming-first successor to OSWorld." **OSWorld-Verified** is the cleaned-up release that fixed v1's 300+ grader/task bugs; it is the eval bar Shinken matches and ships as built-in conformance (**D7**). SOTA at date of record is reported around ~83% (above the human reference ~72.4%) (vendor-published, unverified). Lesson carried forward: graders are *tested artifacts*, versioned with their task and env. References: [OSWorld-Verified overview](https://benchlm.ai/blog/posts/osworld-verified-computer-use-benchmark), [cua OSWorld-Verified integration](https://cua.ai/docs/cua/guide/integrations/benchmarks/osworld-verified).

### pass@k / pass^k

Eval metrics for the *Control Plane* eval layer (**D7**). **pass@k** — the probability that at least one of k attempts succeeds (a capability ceiling). **pass^k** — the probability that *all* k attempts succeed (a reliability floor). Shinken computes both over **N≥5 *CoW*-forked replicas per task** with confidence intervals; the forking is what makes high-N statistics cheap.

### Permission Panel

The human-facing *Capability*-unlock/approval UI inside the *Control Panel* (**D6**). It surfaces a live human-in-the-loop approval card with a Run / Escalate / Deny shape, escalation-on-failure by default, and source-attributed rules (which rule/scope produced the decision). Approvals and denials are first-class *replay* events. This is one of Shinken's four headline features ("unlock advanced image features").

### Provider — see *Substrate / Provider*

### Sandbox

One isolated guest computer — Linux, Windows, or macOS desktop (Android is roadmap, **D10**). The *Substrate* under a Sandbox is pluggable. Reset and branching of a Sandbox are the same *fork-from-snapshot* primitive (**D1**). A *Session* is a live attach/run against a Sandbox.

### Session

A live attach/run against a *Sandbox*. Sessions have dual timers (idle reset ~15 min; max-lifetime ~4–8 h) and auto-suspend-to-snapshot on idle (**D9**). Multiple sessions may fork from one warm parent.

### Set-of-Marks (SoM)

A grounding technique: overlay numbered/labeled marks on the screen so the model emits a stable mark ID instead of regressing raw pixel coordinates. In Shinken's layered observation (**D3**), SoM is **Rung 1** — a server-side GPU microservice (OmniParser-style), invoked on demand when the *accessibility tree* (Rung 0) is insufficient, before falling back to region/zoom pixels (Rung 2) or full frame (Rung 3). ID-based grounding is the single biggest accuracy lever. Reference: [OmniParser V2](https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/), [github.com/microsoft/OmniParser](https://github.com/microsoft/OmniParser).

### SFU (Selective Forwarding Unit)

A WebRTC media topology that encodes a stream once and forwards it to many subscribers (LiveKit / neko-style), instead of re-encoding per viewer. Shinken uses SFU fan-out on the *media plane* so one *NVENC* encode serves all *Control Panel* viewers and recorders (**D4**). Background: [LiveKit simulcast/SFU](https://blog.livekit.io/an-introduction-to-webrtc-simulcast-6c5f1f6402eb/).

### Shinken

The platform/product: an AI-native, cross-platform **sandbox runtime + control plane + control panel** for computer-use agents — a production-grade, streaming-first successor to OSWorld. North star: **one** platform serving both production agent deployment *and* evaluation, layered. It boots *Sandboxes*, drives them through one typed *ACI*, streams live, records a scrubbable/forkable event-sourced *replay*, and gates privileged actions through a *Permission Panel*. Shinken is a public, vendor-neutral open-source project; NVIDIA GPUs are a supported acceleration *option*, not a dependency.

### `.skn` bundle

The on-disk *replay*/trajectory container (**D5**). A ZIP (Playwright-trace model): `manifest.json` + append-only `events.jsonl` (two-level discriminated envelope, `kind ∈ {action, observation, decision, permission, marker, snapshot_ref, meta}`, logical-clock `seq` + wall-clock anchor, `action_id` pairing action→observation) + an immutable checkpoint DAG (parent-pointer, branchable, never mutated) + content-addressed media (fMP4). It doubles as replay-as-training-data (RL/SFT) — a concrete adoption wedge for teams building and evaluating computer-use agents (**D12**).

### Substrate / Provider

A pluggable virtualization backend under a *Sandbox*. Candidates: Firecracker (headless Linux), QEMU-microvm / crosvm (Linux desktop, virtio-gpu), Cloud Hypervisor (Windows / GPU), Apple Virtualization.framework (macOS on Apple hardware), or the OSS *kubernetes-sigs/agent-sandbox* CRD (container tier). Isolation is **tiered and routed by (OS × needs-GPU × needs-fast-fork)** (**D1**). Firecracker has no display/GPU, so desktop and GPU tiers use heavier VMMs. References: [Firecracker](https://aws.amazon.com/blogs/opensource/firecracker-open-source-secure-fast-microvm-serverless/), [Kata vs Firecracker vs gVisor](https://northflank.com/blog/kata-containers-vs-firecracker-vs-gvisor).

### taint-tracking

Propagating an "untrusted-derived" label through the dataflow so that action parameters built from untrusted content (e.g. web text → a shell command) get **promoted to a stricter risk tier** in the *Permission Panel* (**D6**). It is the mitigation for the prompt-injection → exfiltration kill chain. See the [Threat Model](08-threat-model.md) for the full chain and rules.

### tool_runner boundary

The generic policy/execution boundary pattern Shinken adopts: the agent loop runs *outside* the sandbox, and tool calls route through a controlled API that enforces the egress allowlist and capability policy *before* executing (**D2**, **D6**). Code-as-action (`exec`/`bash`/`edit`) is a separate, off-by-default *Capability* class behind this boundary. It is the same idea behind out-of-sandbox egress proxies in agentic-coding tools — described here generically, not as any vendor's product.

### vGPU

NVIDIA virtual GPU — time-sliced sharing of one physical GPU across many guests. In Shinken it is the **density** GPU pool for light desktops (**D11**), the counterpart to the isolation-focused *MIG*-backed pool: time-sliced vGPU maximizes tenant count; MIG maximizes isolation. A public NVIDIA technology option for the optional GPU tier. References: [vGPU user guide](https://docs.nvidia.com/vgpu/latest/grid-vgpu-user-guide/index.html), [time-sliced vs MIG-backed vGPU](https://docs.nvidia.com/ai-enterprise/release-8/latest/infra-software/vgpu/features/mig-backed-vgpu.html).

### virtio-vsock

The paravirtualized host↔guest socket transport. Shinken uses virtio-vsock for the *Guest Runtime* (`shinkend`) control/event channel — **never HTTP polling** (**D4**). It carries the structured action/observation protocol and frame deltas with minimal overhead. (Background on the virtio device family: [QEMU virtio-gpu docs](https://www.qemu.org/docs/master/system/devices/virtio/virtio-gpu.html).)

### WHIP / WHEP

The standard HTTP-based WebRTC signaling protocols. **WHIP** (WebRTC-HTTP Ingestion Protocol) ingests media from the guest into the *SFU*; **WHEP** (WebRTC-HTTP Egress Protocol) delivers it to *Control Panel* viewers. Shinken uses them for *media plane* signaling on its single-PeerConnection, dual-transport design (**D4**), targeting ~50–120 ms glass-to-glass same-region (vendor-published, unverified). References: [Cloudflare WHIP/WHEP](https://blog.cloudflare.com/webrtc-whip-whep-cloudflare-stream/), [IETF WHEP draft](https://www.ietf.org/archive/id/draft-ietf-wish-whep-02.html).

---

## Quick reference: which decision owns each term

| Term | Owning decision(s) |
|------|--------------------|
| ACI, action schema, CoordinateSpace, model adapters | D2 |
| accessibility tree, SoM, CDP, observation rungs | D3 |
| control/event/media planes, NVENC, SFU, WHIP/WHEP, virtio-vsock, NICE DCV | D4 |
| `.skn` bundle, fork-from-snapshot (as replay), CoW | D5 |
| Capability, Permission Panel, Cedar, ocap/caretaker, taint-tracking, tool_runner boundary | D6 |
| pass@k / pass^k, OSWorld / OSWorld-Verified eval | D7 |
| Action Gateway, Fleet Manager, Control Plane, Session timers | D9 |
| Substrate/Provider, fork-from-snapshot, kubernetes-sigs/agent-sandbox, Guest Runtime (cross-OS) | D1, D10 |
| GPU-TEE, NRAS, Confidential Containers, MIG, vGPU, NVENC tier | D11 |
| Open-core positioning, Shinken, Operator | D12 |

> **Unverified-claims reminder.** Every latency/density/cost figure above is vendor-published and unverified. A first-party measurement plan is required before any number here is load-bearing. See [05 — Tech Decisions](05-tech-decisions.md), [06 — Roadmap](06-roadmap.md), and [notes/open-questions.md](../notes/open-questions.md).
