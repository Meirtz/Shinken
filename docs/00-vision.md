# 00 — Vision & Positioning

> Status: drafted · Last updated 2026-05-30
>
> Sibling docs: [01 PRD](01-prd.md) · [02 Architecture](02-architecture.md) · [03 OSWorld teardown](03-osworld-analysis.md) · [04 Landscape](04-landscape.md) · [05 Tech decisions (ADRs)](05-tech-decisions.md) · [06 Roadmap](06-roadmap.md) · [07 Glossary](07-glossary.md) · [08 Threat model](08-threat-model.md) · [09 Economics & build-vs-buy](09-economics-and-build-vs-buy.md) · Sources: [`../notes/sources.md`](../notes/sources.md)

**Shinken is an AI-native, cross-platform sandbox runtime, control plane, and control panel for computer-use agents — a production-grade, streaming-first successor to OSWorld.** It boots isolated desktop **Sandboxes** (Linux/Windows/macOS; Android on the roadmap), drives them through a single typed **Agent-Computer Interface (ACI)** with a **structured-first, pixels-on-demand** observation model, streams every operation live and records it as a **scrubbable, forkable, event-sourced replay**, provisions the **sandbox capabilities / OS entitlements** each run needs, and exposes an **eval layer** (OSWorld-Verified and friends) on the *same* runtime. The north star is **one platform serving both production agent deployment and evaluation, layered.** Design decisions are referenced as **D1–D12** and detailed in [05 Tech decisions](05-tech-decisions.md).

---

## 1. The problem: today's computer-use stacks are research artifacts

In 2026, frontier models can drive a desktop better than a median human on the standard benchmark — the top OSWorld-Verified score has crossed **~83%** versus a **~72.4%** human baseline ([xlang.ai/blog/osworld-verified](https://xlang.ai/blog/osworld-verified); leaderboard figures vendor-published, unverified). The *models* have crossed a threshold. The *infrastructure under them has not.* Every widely used runtime is, structurally, a research demo wearing production clothes:

- **OSWorld** — the de-facto benchmark and the thing Shinken succeeds — drives the guest through a Flask server on port 5000 that the agent **polls for full-frame PNG screenshots**, reverts a whole VM between tasks, and writes a `traj.jsonl` + `recording.mp4` with **no scrub, no fork, no re-run from step N**. OSWorld v1 shipped with **300+ grader/task bugs** later fixed in OSWorld-Verified ([github.com/xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld)) — the eval layer was stringly-typed and untested.
- **Anthropic's reference container** ([github.com/anthropics/anthropic-quickstarts](https://github.com/anthropics/anthropic-quickstarts)) feeds the model a fresh full PNG per step after a hardcoded settle delay, streams humans via **stock x11vnc + noVNC** on a duplicated pixel path, and has no durable replay or in-loop permission gate.
- **OpenAI Operator / computer-use-preview** re-sends a full-resolution PNG every turn, is explicitly "not for production," and exposes only a `pending_safety_checks` acknowledgement seam ([developers.openai.com/api/docs/guides/tools-computer-use](https://developers.openai.com/api/docs/guides/tools-computer-use)).
- **trycua/cua** — the closest, best-engineered analog — still observes by **pulling a base64 PNG per step over HTTP/SSE**, serializes one command at a time, has no live video streaming, and its permission story is effectively a TODO ([github.com/trycua/cua](https://github.com/trycua/cua)).
- **E2B Desktop** spawns an `xdotool`/`scrot` process **per click/keystroke/screenshot**, streams raw VNC over WebSocket, and has no action/observation replay ([github.com/e2b-dev/desktop](https://github.com/e2b-dev/desktop)).

The pattern is consistent: **poll a screenshot, click a pixel, throw the trace away, and trust the sandbox boundary to be the entire product story.** That is fine for a paper. It is not fine for running thousands of concurrent agents, handing one a real desktop with `sudo`, credentials, network, or GPU, or generating defensible training data and eval scores. The bandwidth is wasteful, the observation is brittle (pixel coordinates drift with resolution and DPI), the capability model is binary, and the trajectory is unforkable.

Shinken's thesis: **the bottleneck has moved from the model to the runtime, and the runtime needs rebuilding as production infrastructure** — not a polling loop.

## 2. Why now (2026)

Three curves crossed in the last ~18 months that make this the right moment:

1. **Models are good enough that infra is the limiter.** Above-human OSWorld-Verified scores (vendor-published, unverified) mean the marginal task is now bottlenecked by reset speed, observation fidelity, bandwidth cost, and safety gating — all infrastructure concerns, not model concerns.
2. **The enabling primitives have matured.** Sub-millisecond copy-on-write VM fork is real (Morph Infinibranch reports fork P99 ~1.3 ms and ~93% shared pages — vendor-published, unverified; [cloud.morph.so/docs](https://cloud.morph.so/docs/documentation/instances/branch)). Firecracker snapshot-restore is 5–30 ms VMM-side ([firecracker-microvm.github.io](https://firecracker-microvm.github.io/); vendor-published, unverified). Data-center NVENC on Ada GPUs (L4/L40S) is uncapped versus the consumer 8-session limit, with AV1 saving ~40% bitrate vs H.264 ([developer.nvidia.com — AV1 and Ada Lovelace](https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/); vendor-published, unverified). WebRTC SFU fan-out, Set-of-Marks/OmniParser grounding ([github.com/microsoft/OmniParser](https://github.com/microsoft/OmniParser)), and Cedar policy evaluation ([docs.cedarpolicy.com](https://docs.cedarpolicy.com/policies/syntax-policy.html)) are all production-ready.
3. **The standard agent-sandbox pattern is now public and converging.** The community has codified the warm-pool + fork-on-demand shape as an open Kubernetes CRD — `kubernetes-sigs/agent-sandbox` (`Sandbox` / `SandboxTemplate` / `SandboxClaim` / `SandboxWarmPool`) — so the orchestration layer no longer has to be invented from scratch. Meanwhile the market has voted on what *not* to build: browser-only single-modality players are under pressure (Scrapybara sunset), open self-hostable cores win (E2B, cua, OSWorld, HUD), and DOM-based (rrweb) replay was largely abandoned for video — yet pixel-polling remains the universal incumbent nobody has displaced. That displaceable incumbent is the opening.

## 3. What Shinken is (one crisp definition)

> **Shinken is the operating system for computer-use agents:** a single typed interface and streaming runtime that lets any model drive an isolated Linux/Windows/macOS desktop, gives that desktop the capabilities it needs, watches and records everything it does as a forkable timeline, and runs the same way whether you are deploying it in production or scoring it on a benchmark.

Concretely, Shinken is **three layers**:

```
┌──────────────────────────────────────────────────────────────────────┐
│  CONTROL PANEL  (human web UI)                                         │
│  live structured + on-demand video · capability configuration ·        │
│  replay scrub / fork · cross-session search · human takeover           │
├──────────────────────────────────────────────────────────────────────┤
│  CONTROL PLANE                                                         │
│  Fleet Manager (warm pools + fork-on-demand) · Action Gateway          │
│  (auth → rate-limit → policy choke point) · scheduler ·                │
│  replay store · EVAL SERVICE · telemetry (OTel-GenAI)                  │
├──────────────────────────────────────────────────────────────────────┤
│  SANDBOX  (one isolated guest computer; substrate-pluggable)           │
│  Guest Runtime `shinkend` → executes the ACI, emits the event stream   │
│   ▲ control plane (lifecycle) │ event plane (actions/obs/permissions = │
│     the replay log) │ media plane (on-demand NVENC video)              │
└──────────────────────────────────────────────────────────────────────┘
        ▲ Operator (client-side adapter; the seam for human takeover;
          provider-agnostic agent loop behind it)
```

The vocabulary (full definitions in [07 Glossary](07-glossary.md)): a **Sandbox** is one isolated guest computer (substrate-pluggable); a **Session** is a live attach/run; the **Guest Runtime** (`shinkend`) is the in-Sandbox daemon that executes the ACI and emits the event stream (the structured successor to OSWorld's Flask `main.py`); the **ACI** is the versioned protocol plus the typed action/observation schema; the **Operator** is the client-side adapter that drives a Sandbox and is the human-takeover seam, with a provider-agnostic agent loop behind it. The **three planes** are *control* (lifecycle/signaling), *event* (actions + observations + permissions — the reliable data channel that **is** the replay log), and *media* (an on-demand video track).

What makes it AI-native rather than a remote desktop: the **primary observation is a normalized cross-OS accessibility/DOM tree**, not pixels (D3); the **agent acts on stable element refs**, not raw `x,y`; **pixels and video are escalation tiers requested on demand**; and the **live structured event stream the model consumes is the same stream that becomes the replay** (D4, D5). It is deliberately the inversion of OSWorld: structured-first, event-sourced, capability-managed.

## 4. The four headline outcomes (as user value)

These map one-to-one to the design decisions; the *value* framing is what a user actually gets.

| Outcome | What the user gets | Backed by |
|---|---|---|
| **1. Replay** | "Scrub any agent run like a video, branch from step N, and re-run a counterfactual — without re-running the whole task." A `.skn` bundle is a forkable, event-sourced trajectory, not a write-only log; the same bundle harvests as RL/SFT training data. | **D5** — `.skn` (Playwright-trace model), immutable checkpoint DAG, append-only event log; instant-reset and replay-branching are the *same* CoW-fork primitive (D1). |
| **2. Sandbox capability manager** | "Give this Sandbox network egress, credentials, GPU, persistence, privileged installs, clipboard, screen capture, or OS automation — and keep those powers scoped, revocable, replayed, and isolated." Sandbox-internal dangerous work is allowed by design; crossing the boundary is explicit. | **D6** — capability/entitlement policy + object-capability handles + OS enforcement; secrets brokered via Vault/KMS + egress proxy so plaintext never reaches the model. |
| **3. Bandwidth optimization** | "Run thousands of agents without a six-figure monthly egress bill." Structured a11y/DOM observation is **~150× cheaper** than H.264 office video (~20 kbps vs ~3 Mbps) and **~6× cheaper** in tokens (~25k vs ~150k/task) (vendor-published, unverified). Pixels flow only when the model or a human asks. | **D3** (structured-first) + **D4** (dual-channel, NVENC-on-demand). |
| **4. Real-time streaming** | "Watch and take over a live agent with sub-second, glass-to-glass lag — over a single WebRTC connection in the browser, no native client." Reliable data channel for the event stream + on-demand media track; host↔guest over virtio-vsock, never HTTP polling; target ~50–120 ms same-region. | **D4** — single-PeerConnection WebRTC dual-transport, SFU fan-out, WHIP/WHEP. |

Every competitor leaks on at least three of these. Replay and sandbox capability management are **greenfield** across the field; streaming/bandwidth is the clear **beat** axis where every competitor is screenshot-poll or VNC/pixel. See [04 Landscape](04-landscape.md) for the per-axis comparison.

## 5. Who it's for

| Audience | What they need | How Shinken serves them |
|---|---|---|
| **Agent developers** | A clean SDK to drive a real desktop with their existing model loop, without writing virtualization or streaming glue. | Native streaming py/ts SDK + optional MCP facade (D8); version-pinned adapters for the Anthropic, OpenAI, UI-TARS, and OSWorld schemas (D2) so an off-the-shelf agent drives Shinken unchanged. Open, self-hostable core (D12) — no lock-in. |
| **Eval engineers & CUA researchers** | Reproducible, massively parallel, *defensible* evaluation — plus trajectory data to train on. | Eval is thin orchestration on the same runtime (D7): a typed verifier DAG, N≥5 CoW-forked replicas → pass@k / pass^k with confidence intervals, task+grader+env versioned together. `.skn` replay doubles as RL/SFT training data — the adoption wedge (D5, D12). |
| **Platform admins** | Fleet operability, cost control, multi-tenant safety, audit. | A control plane with warm pools + fork-on-demand, an Action Gateway choke point, dual-timer auto-suspend (idle dominates cost), circuit-breakable Sandbox health, and OTel-GenAI telemetry (D9). Permission grants/denials are first-class, auditable replay events. |
| **Human supervisors** | To watch, configure sandbox capabilities, and take over mid-run. | The Control Panel: live structured + on-demand pixel view, capability configuration, replay scrubbing, cross-session search, and takeover via the Operator seam (D6, D8). |

These four roles share one runtime: the developer's agent, the researcher's eval, the admin's fleet, and the supervisor's panel all see the same Sandboxes, the same ACI, and the same `.skn` replays.

## 6. Positioning: the four-camp intersection

The field is **four largely non-overlapping camps, and Shinken sits at their unclaimed intersection.** See [04 Landscape](04-landscape.md) for the full survey.

```mermaid
graph TD
    A["Cross-platform runtimes<br/>(trycua/cua — closest competitor)"]
    B["Linux fast-sandboxes<br/>(E2B · Morph)"]
    C["Browser-only<br/>(Browserbase · Scrapybara)"]
    D["Models + evals<br/>(Anthropic · OpenAI · HUD · OSWorld)"]
    S(("SHINKEN<br/>unclaimed<br/>intersection"))
    A --> S
    B --> S
    C --> S
    D --> S
```

Shinken's stance toward each axis is **match the leaders, then exceed their scope**:

- **MATCH:** Morph-class sub-ms CoW fork/reset on the Linux tier; the model ecosystem via thin version-pinned adapters; the OSWorld-Verified eval bar; cua's clean `Image → Runtime → Transport → Interfaces → Sandbox` layering and developer experience.
- **BEAT:** streaming and bandwidth — everyone else polls screenshots or pushes VNC/full pixels; Shinken's dual-channel ACI is the literal thing it is built to win.
- **DIFFERENTIATE:** event-sourced replay/branching; sandbox capability and OS-entitlement management; an optional GPU-accelerated tier no Firecracker-based competitor can hold (Firecracker has *zero* GPU passthrough by design); and full cross-platform *desktop* (not browser-only — even Scrapybara never did macOS, and it sunset).

The unclaimed center is **"full-spectrum desktop computer-use, structured-streamed, replayable, capability-gated, at cloud concurrency."** cua owns breadth ("one API, any OS, cloud or local"); Shinken does not out-breadth them on day one — it wins on **streaming, replay, permissions, and GPU**, the four things cua's own design flags as gaps.

## 7. Ecosystem & build-vs-buy

Shinken is **composed, not monolithic.** Each layer is pluggable, and for each there is a public OSS-or-product build-vs-buy option so a self-hoster can stand the whole thing up without proprietary dependencies. The reconciliation table is in [09 Economics & build-vs-buy](09-economics-and-build-vs-buy.md); the substrate matrix is in [02 Architecture](02-architecture.md).

| Layer | Build-vs-buy options (public OSS / public product) | Shinken's stance |
|---|---|---|
| **Container substrate (Linux)** | The OSS **`kubernetes-sigs/agent-sandbox`** CRD under **gVisor / Kata** with pre-warmed pools — the standard K8s agent-sandbox pattern. Alternatives: **E2B**, **Morph**, **Daytona**, **trycua/cua**, **Kata + Firecracker**, or an in-house Cloud-Hypervisor/Firecracker fleet. | **Buy/adopt** the CRD shape as the Fleet Manager's default fast-path; pluggable so any of the above can back a tier (D1, D9). |
| **VM substrate (desktop / GPU / Windows)** | **Cloud Hypervisor / QEMU-microvm / crosvm** (display + virtio-gpu + VFIO that Firecracker lacks); **Apple Virtualization.framework** for macOS (Apple-HW-only, ~2 VMs/host). | **Build thin** on these per-OS; one Guest Runtime contract across all (D1, D10). |
| **Secret broker** | **HashiCorp Vault**, any **cloud KMS**, or **SPIFFE/SPIRE**. | **Integrate.** The model never sees plaintext; secrets are header-injected at the egress proxy (D6). |
| **Policy boundary** | A generic **`tool_runner` pattern**: the agent loop runs *outside* the Sandbox; tool calls route through a controlled API enforcing a deny-by-default egress allowlist. | **Build** the boundary; computer-use verbs and the off-by-default code-as-action class both route through it (D2, D6). |
| **High-fidelity pixel channel** | **NICE DCV** (public remote-display product: NVENC + QUIC + browser client; [docs.aws.amazon.com/dcv](https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html)) **vs** a custom WebRTC + NVENC pipeline (GStreamer `nvcodec` → `webrtcbin`, the neko/Selkies pattern). | **Buy-or-build** per deployment; the media plane is an interface, not a hard dependency (D4). |
| **Optional GPU acceleration tier** | NVIDIA **vGPU/MIG** for density and isolation; **NVENC** on data-center Ada GPUs (**L4** density, **L40S** 4K/AV1 + render) for the encode tier; **GPU-TEE + Confidential Computing + remote attestation (NRAS)** for trusted multi-tenant workloads ([MIG](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html), [vGPU](https://docs.nvidia.com/vgpu/knowledge-base/latest/vgpu-features.html)). | **Opt-in.** Most tasks ride the CPU-only Linux fork tier; GPU is a gated capability the panel hands out (D11). |

Two opinionated constraints carry into the GPU tier (D11): the **encode tier never runs on A100/H100/H200/B200** (those data-center accelerators ship with **zero NVENC engines**, a public NVIDIA fact — [en.wikipedia.org/wiki/Nvidia_NVENC](https://en.wikipedia.org/wiki/Nvidia_NVENC)); it uses Ada L4/L40S instead. And the consumer **8-session NVENC cap does not apply to qualified data-center GPUs** (vendor-published, unverified). All speed/density/cost numbers here are vendor-published and **unverified** pending a first-party measurement plan; see [09 Economics & build-vs-buy](09-economics-and-build-vs-buy.md).

The posture is deliberate: **Shinken's value is the union and the seams — the ACI, the dual-channel streaming, the event-sourced replay, and the Sandbox Capability Manager — not the virtualization itself.** Where a mature OSS substrate or public product already exists, Shinken integrates it behind a pluggable interface rather than re-implementing it.

## 8. The north star: one platform, production + eval, layered

The single organizing principle: **production agent deployment and evaluation run on the same runtime, layered.** Eval is not a separate codebase — it is thin orchestration over the production Sandbox/ACI/replay stack (D7). The same CoW-fork that gives a production agent an instant clean desktop gives an eval N≥5 replicas for pass@k; the same `.skn` event log a supervisor scrubs is the training trajectory a model team ingests; the same Cedar policy that gates a production `sudo` is the deterministic setup/teardown contract a benchmark relies on.

```
              ┌─────────────────────────┐
              │      EVAL LAYER         │  verifier DAG · pass@k / pass^k ·
              │  (thin orchestration)   │  conformance suites · trajectory capture
              └───────────┬─────────────┘
                          │  (same contract)
   ┌──────────────────────┴──────────────────────┐
   │      PRODUCTION RUNTIME + CONTROL PLANE      │  Sandbox · ACI · dual-channel
   │   ONE Guest Runtime · ONE ACI · ONE plane    │  streaming · .skn replay ·
   └──────────────────────────────────────────────┘  capability manager
```

This inverts the OSWorld world, where the benchmark *was* the platform and production was an afterthought. Built-in conformance ships task + grader + environment **versioned together** — OSWorld-Verified, WindowsAgentArena ([arxiv.org/abs/2409.08264](https://arxiv.org/abs/2409.08264)), AndroidWorld ([arxiv.org/abs/2405.14573](https://arxiv.org/abs/2405.14573)), WebArena/VisualWebArena/WebVoyager — because graders are tested artifacts, not stringly-typed afterthoughts (the lesson of OSWorld's 300+ grader bugs). Phasing (see [06 Roadmap](06-roadmap.md)): **local single-VM v0 → Linux fast-fork cloud tier → eval layer at concurrency → Windows/macOS heavier tiers → GPU/trusted tiers.** Linux is first-class v1; Windows and macOS ship v1 as heavier, longer-lived tiers; Android is roadmap (D10).

## 9. Explicit non-goals

Being opinionated means saying no. As of 2026-05-30:

- **Not a new foundation model.** Shinken is provider-agnostic infrastructure (D2); the agent loop lives behind the Operator contract. We ship adapters, not a model — we serve model teams, not compete with them.
- **Not browser-only, and not Android-in-v1.** Full *desktop* cross-platform is the point (D10); Android is explicitly roadmap, not v1.
- **Not bit-deterministic replay.** Full-desktop record/replay via tools like rr or Hermit is impractical for cross-OS GUI; Shinken does pragmatic **state-snapshot + event-log + observation-log**, not cycle-accurate determinism (D5). Re-running side-effecting tool calls on a branch is a known concern, not a solved guarantee.
- **Not a re-implementation of the substrate.** Where a mature OSS substrate or public product already exists — the agent-sandbox CRD, Vault/KMS, NICE DCV — Shinken integrates it behind a pluggable interface (D12, §7).
- **Not OPA/Rego for policy.** The decision layer is **Cedar** (formally verifiable, sub-ms), deliberately not OPA/Rego (D6).
- **Not MCP for the hot loop.** MCP is a facade at two altitudes; the high-frequency action/observation/video loop and media **never** route through MCP (D8).
- **Not a stealth/anti-bot scraping tool.** We target legitimate desktop automation and eval, not anti-detection scraping — the legal/reputational trap the browser cohort fell into.
- **GPU is opt-in, not default** (D11), and the encode tier never runs on accelerators with no NVENC engines (§7).
- **Not (yet) decided: multi-player / non-exclusive computer-use.** Whether one desktop can host separate human and agent cursors simultaneously is an explicit open in/out decision, not a committed feature.

### Open questions carried forward (do not paper over)

The **a11y-tree coverage** assumption on Electron/Qt/canvas/games is the load-bearing unverified bet behind the bandwidth and replay-stability claims (it needs a measurement spike); macOS/Windows fast-reset is largely infeasible today; Windows-in-cloud licensing and the macOS ~2-VM/host cap shape cost and roadmap; **all speed/density/cost numbers in this document are vendor-published and unverified** pending a first-party measurement plan; and a consolidated threat model and the multi-player in/out decision are still open. These flow into [01 PRD](01-prd.md), [05 Tech decisions](05-tech-decisions.md), [08 Threat model](08-threat-model.md), and [09 Economics & build-vs-buy](09-economics-and-build-vs-buy.md).
