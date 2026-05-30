# Shinken — Roadmap

> Status: drafting · Date: 2026-05-30
> Sibling docs: [00 Vision](00-vision.md) · [01 PRD](01-prd.md) · [02 Architecture](02-architecture.md) · [03 OSWorld teardown](03-osworld-analysis.md) · [04 Landscape](04-landscape.md) · [05 Tech decisions / ADRs](05-tech-decisions.md) · [07 Glossary](07-glossary.md) · [08 Threat model](08-threat-model.md) · [09 Economics & build-vs-buy](09-economics-and-build-vs-buy.md)

Shinken's roadmap is deliberately **de-risk-first, not feature-first**. The completeness review behind this design is blunt: the *qualitative* architecture is sound, but the *load-bearing* assumptions — that a normalized accessibility (a11y) tree covers enough real applications to make the structured-first observation model worthwhile, that copy-on-write (CoW) fork density is economically real, and that the dual-channel WebRTC latency budget actually closes — are **all vendor-quoted and unverified** as of 2026-05-30. So the phases below are gated by **spikes that can kill an architectural bet before it becomes load-bearing**, and the build order front-loads the cheapest tier (Linux CPU-only fast-fork, per [D1](05-tech-decisions.md)) and the highest-leverage adoption wedge (replay-as-training-data for the first computer-use agent (CUA) eval and model-training users, per [D12](05-tech-decisions.md)).

Every phase reconciles to the twelve authoritative decisions **D1–D12** in [`05-tech-decisions.md`](05-tech-decisions.md), and the plan reuses generally-available building blocks — the OSS `kubernetes-sigs/agent-sandbox` CRD, [HashiCorp Vault](https://www.hashicorp.com/en/blog/vault-enterprise-1-21-spiffe-auth-with-spire-cross-namespace-secret-import), and (for the optional pixel channel) [NICE DCV](https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html) or a custom WebRTC + hardware-encode pipeline — rather than rebuilding undifferentiated infrastructure. **Every speed/density/cost figure below is tagged (vendor-published, unverified)** unless a spike has produced a first-party number; see [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md) for the measurement plan that retires those tags.

---

## Phasing philosophy

Three rules govern the sequence.

1. **Spikes gate phases.** A *spike* is a throwaway measurement built to confirm or kill an assumption, with a pass/fail metric attached. Three spikes recur as gates: **a11y-coverage** (kills or confirms structured-first observation, [D3](05-tech-decisions.md)), **CoW-fork density** (kills or confirms the Linux fork-tier economics, [D1](05-tech-decisions.md)/[D9](05-tech-decisions.md)), and **dual-channel latency** (kills or confirms the WebRTC streaming budget, [D4](05-tech-decisions.md)). Each runs *before* the phase that depends on it ships, not after.
2. **Buy the substrate, build only the differentiators.** Per [D12](05-tech-decisions.md), reuse the OSS `kubernetes-sigs/agent-sandbox` CRD (gVisor/Kata runtime classes + warm pools) as the Linux container substrate, Vault (or any cloud KMS / SPIFFE-SPIRE) as the secret broker, NICE DCV as the build-vs-buy option for the high-fidelity pixel channel, and a generic `tool_runner` policy boundary for code-as-action egress control. Build the ACI, dual-channel streaming, event-sourced replay/branching, the capability-unlock permission panel, and the eval layer — the five things none of those building blocks deliver.
3. **Land the eval/training users first.** Per [D12](05-tech-decisions.md), the first users are teams building and evaluating computer-use agents — model labs, researchers, RPA builders — who already buy or build OSWorld-style datasets. The roadmap reaches *eval + replay-as-training-data* (Phase 2) as fast as is responsible, because that is the workload where Shinken earns adoption: every eval run and every production session becomes versioned, branchable RL/SFT trajectory data on the same `.skn` substrate.

```mermaid
gantt
    title Shinken phased roadmap (indicative quarters from 2026-Q3; durations are planning estimates, not commitments)
    dateFormat  YYYY-MM-DD
    axisFormat  %Y-Q%q

    section Spikes (gates)
    SPIKE a11y-coverage            :crit, sp1, 2026-07-01, 60d
    SPIKE CoW-fork density         :crit, sp2, 2026-08-15, 60d
    SPIKE dual-channel latency     :crit, sp3, 2026-10-15, 75d

    section Phase 0 - Local PoC
    Single-Sandbox, ACI v0, screenshot+a11y, basic .skn :p0, 2026-07-01, 120d

    section Phase 1 - Linux fast-fork + stream + panel
    Fork tier + dual-channel + permission-panel MVP + Control Panel :p1, after p0, 180d

    section Phase 2 - Eval + training-data users
    Eval layer + OSWorld-Verified + replay-as-training-data :p2, after p1, 150d

    section Phase 3 - Cross-OS
    Windows + macOS tiers          :p3, after p2, 180d

    section Phase 4 - Cloud scale
    Ultra-high concurrency + multi-tenant + optional GPU/NVENC + GPU-TEE :p4, after p3, 240d

    section Later
    Android + multi-player         :p5, after p4, 180d
```

| Phase | Theme | Primary decisions exercised | External dependency | Gating spike(s) |
|-------|-------|-----------------------------|---------------------|-----------------|
| **0** | Local single-Sandbox PoC | D2 (ACI), D3 (observation), D5 (`.skn`) | none (local Docker/QEMU) | produces the a11y-coverage spike |
| **1** | Linux fast-fork + streaming + panel | D1, D3, D4, D6, D9 | `agent-sandbox` CRD, Vault, NICE DCV or WebRTC+NVENC | a11y-coverage, CoW-fork density, dual-channel latency |
| **2** | Eval + OSWorld-Verified + first eval/training users | D5, D7, D8 | `agent-sandbox` CRD (parallel replicas) | reuses Phase 1 spikes |
| **3** | Cross-OS (Windows + macOS) | D1, D10 | Apple hardware pool; Windows licensing | macOS-reset feasibility, Windows-licensing |
| **4** | Cloud ultra-high-concurrency + optional GPU/TEE | D4, D9, D11 | NICE DCV, GPU-TEE/NRAS/Confidential Containers, vGPU/MIG | NVENC-density, GPU-TEE-attestation |
| **Later** | Android + multi-player | D10 (Android), scope decision | redroid/Cuttlefish | touch-schema; multi-cursor |

---

## Phase 0 — Local single-Sandbox PoC

**Objective:** Prove the *seams* end-to-end on a developer laptop, at concurrency = 1, with no cloud, no GPU, no warm pools. This phase exists to make the ACI, the observation model, and the `.skn` format **concrete and falsifiable** — and to host the first and most important spike (a11y-coverage), because if structured-first observation fails here the entire architecture changes before anything expensive is built.

Per [D1](05-tech-decisions.md), dev/test starts **local, small concurrency**. The substrate is whatever boots on a laptop: a local **Docker** Linux desktop (the [E2B-desktop](https://github.com/e2b-dev/desktop) pattern) or a **QEMU-microvm** with virtio-gpu. No cluster substrate dependency yet.

### Goals

- One **Guest Runtime** (`shinkend`) inside one Linux Sandbox, replacing OSWorld's Flask `main.py` full-screenshot polling model ([D2](05-tech-decisions.md); see the OSWorld teardown in [03](03-osworld-analysis.md)).
- **ACI v0:** the canonical typed tagged-union action schema ([D2](05-tech-decisions.md)), discriminated by `verb`, with `target = oneof{point_px | point_norm | element_ref}` and an explicit `CoordinateSpace` on every observation. Ship *one* model adapter end-to-end — the Anthropic computer-use tool (`computer_2025xxxx` + `bash` + text-editor) — to prove the bidirectional-adapter seam ([D2](05-tech-decisions.md)). The adapter contract is documented at the [Claude computer-use tool reference](https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool).
- **Observation = screenshot + a11y** ([D3](05-tech-decisions.md)): Rung 0 normalized AT-SPI tree mapped to the `Element{ref,role,name,value,states,bbox,source}` schema, with the Rung 3 full-frame screenshot as the always-available fallback. This is the minimum that lets the a11y-coverage spike run; the browser path uses CDP `Accessibility.getFullAXTree` ([Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/tot/Accessibility/)).
- **Basic `.skn` replay** ([D5](05-tech-decisions.md)): write `manifest.json` + an append-only `events.jsonl` (the two-level discriminated envelope, logical-clock `seq` + wall-clock anchor, `action_id` pairing each action to its observation) plus a single snapshot reference. Replay = re-read the event log into a local viewer. No branching yet.
- Transport: host↔guest over **virtio-vsock** (or a local socket for the Docker path), never HTTP polling ([D4](05-tech-decisions.md)) — even at concurrency 1, to lock the contract.

### Deliverables

- `shinkend` daemon (Linux) emitting the event stream.
- ACI v0 schema (IDL) + a generated Python SDK ([D8](05-tech-decisions.md): one IDL → SDKs) + the Anthropic adapter.
- `.skn` writer + a minimal CLI replay viewer (scrub by `seq`).
- The **a11y-coverage spike harness** (below) and its first dataset.

### 🔬 SPIKE — a11y-coverage *(the load-bearing assumption)*

The whole structured-first / pixels-on-demand thesis ([D3](05-tech-decisions.md), headline feature #3) rests on cross-platform a11y trees being **reliably populated, cheap to diff, and unifiable** into one schema. The known failure modes are Electron, Qt, canvas/WebGL surfaces, and games, which can yield empty or shallow trees.

- **Method:** instrument a representative application set — Firefox/Chromium (CDP + AT-SPI), an Electron app (e.g. VS Code), a Qt native app, a canvas/WebGL page, and a 2D game — and measure, per application: (a) the fraction of *actionable* elements exposed in the tree, (b) serialized tree size and diff CPU at 1080p and 4K, and (c) bytes/step and tokens/step for structured-vs-pixel observation **net of pixel fallback**.
- **Pass/fail:** PASS if, across the set, structured observation delivers a *net* token reduction approaching the published anchor (~6×, roughly 25k vs 150k tokens/task — vendor-published, unverified) **after** accounting for applications that fall back to pixels. FAIL (→ re-architect to pixels-first with structured enrichment) if the median real application needs the pixel fallback anyway. Background on the structured-vs-pixel token economics is summarized in [`../notes/streaming-bandwidth.md`](../notes/streaming-bandwidth.md).

### Success criteria

- A single Anthropic-driven agent completes 3–5 scripted Linux desktop tasks (open a browser, fill a form, save a file) through ACI v0, producing a replayable `.skn`.
- First-party a11y-coverage numbers exist and the structured-first bet is explicitly **CONFIRMED** or **KILLED** (this output feeds [D3](05-tech-decisions.md)).
- Replay round-trips: an event log re-renders deterministically in the viewer (state-snapshot + event-log model, [D5](05-tech-decisions.md) — *not* bit-deterministic).

### Dependencies & exit gate

- Dependencies: none external; local Docker/QEMU only.
- **Exit gate:** the a11y-coverage spike has a verdict. If KILLED, revise D3 before Phase 1 starts.

---

## Phase 1 — Linux fast-fork tier + dual-channel streaming + permission-panel MVP + Control Panel

**Objective:** Stand up the **default v1 tier** ([D1](05-tech-decisions.md): Linux fast-fork, CPU-only) on a real cluster substrate, with the two headline differentiators that beat the field — **dual-channel streaming** ([D4](05-tech-decisions.md)) and the **capability-unlock permission panel** ([D6](05-tech-decisions.md)) — and the **Control Panel** human UI. This is the phase that turns the PoC into a runtime.

### Goals

- **Linux fast-fork tier ([D1](05-tech-decisions.md)):** a container fast-path on the OSS `kubernetes-sigs/agent-sandbox` CRD (gVisor/Kata runtime classes, warm pools via the `SandboxWarmPool` CRD shape — see [Agent Sandbox on Kubernetes](https://northflank.com/blog/agent-sandbox-on-kubernetes) and the [GKE Agent Sandbox blog](https://cloud.google.com/blog/products/containers-kubernetes/bringing-you-agent-sandbox-on-gke-and-agent-substrate)), plus a VM tier on **Firecracker** (headless) and **QEMU-microvm/crosvm** (desktop, virtio-gpu, since Firecracker exposes no display or GPU device — [D1](05-tech-decisions.md)). Reset = **fork-from-snapshot**: MAP_PRIVATE CoW + userfaultfd + a warm parent pool ([Firecracker snapshot docs](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md)), with the **post-fork uniqueness hook** that reseeds RNG/MAC/hostname/boot-id — the documented ["Restoring Uniqueness in MicroVM Snapshots"](https://ar5iv.labs.arxiv.org/html/2102.12892) pitfall.
- **Dual-channel streaming ([D4](05-tech-decisions.md)):** a single-PeerConnection WebRTC session — a reliable-ordered **data channel** carrying the structured action/observation/permission event stream (this *is* the replay log, [D5](05-tech-decisions.md)), plus an **on-demand hardware-encoded media track** (H.264/AV1; NVENC on the optional GPU tier). WHIP/WHEP signaling ([RFC 9725](https://datatracker.ietf.org/doc/html/rfc9725), [WebRTC DataChannel/SCTP RFC 8831](https://datatracker.ietf.org/doc/html/rfc8831)). At Phase 1 a single-tenant SFU is fine; encode-once fan-out is a Phase 4 concern. The open-source [neko](https://github.com/m1k1o/neko) GStreamer→`webrtcbin` design and the [Selkies-GStreamer](https://github.com/selkies-project/selkies-gstreamer) `ximagesrc → nvcodec → webrtcbin` pattern are the starting references.
- **Permission-panel MVP ([D6](05-tech-decisions.md)):** the 3-layer model in minimum form — a [Cedar](https://docs.cedarpolicy.com/policies/syntax-policy.html) declarative decision layer (sub-ms, formally verifiable; deliberately **not** OPA/Rego), an object-capability caretaker/membrane handle layer for O(1) instant revoke, and **OS enforcement** on Linux only (bubblewrap + seccomp network-gate + [Landlock](https://docs.kernel.org/userspace-api/landlock.html) + cgroups + an **out-of-VM egress proxy**, deny-by-default and scoped-domain). Implement the 8 capability classes and 4 risk tiers (Auto/Notify/Ask/Block), the live **HITL approval card**, and **Vault**-brokered secrets via proxy header-injection so the model never sees plaintext. Code-as-action is the off-by-default capability class, gated through a generic `tool_runner` egress-allowlist boundary (the pattern proven by the [OpenAI Codex network-proxy](https://github.com/openai/codex/blob/main/codex-rs/network-proxy/src/proxy.rs) and [Anthropic's sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)).
- **Control Panel (headline #1/#2):** a web UI with a live structured + on-demand video view, the permission approval cards, `.skn` replay/scrubbing, and basic cross-session search. Human takeover routes through the **Operator** seam.
- **Action Gateway ([D9](05-tech-decisions.md)):** the single request-path choke point doing tenant-auth → token-bucket rate-limit → Cedar policy → dispatch, wired in even at low concurrency so nothing ever bypasses it.

### Deliverables

- Fleet Manager v1 (warm pool per image/tier on the `agent-sandbox` CRD; fork-on-demand; cold-pool replenish).
- WebRTC dual-transport stack (data channel + hardware-encoded media track) over virtio-vsock host↔guest.
- Cedar policy engine + ocap membrane + Linux OS-enforcement + egress proxy + Vault broker.
- Control Panel (live view, approvals, replay scrub, search).
- Branchable `.skn` ([D5](05-tech-decisions.md)): a checkpoint DAG with CoW-fork branching — branch and instant-reset are the **same primitive** ([D1](05-tech-decisions.md)).

### 🔬 SPIKE — CoW-fork density

- **Method:** on Firecracker and Cloud Hypervisor/QEMU, fork N children from one warm parent and measure **private-RSS-bound concurrent guests per host** (the real density metric, not total image size), fork P99, time-to-first-action, and correctness of the uniqueness reseed (RNG/MAC/clock/TLS). Compare against the published anchors: [Morph Infinibranch](https://cloud.morph.so/docs/documentation/instances/branch) fork P99 ~1.3 ms with ~93% shared pages; Firecracker VMM-side restore 5–30 ms; the OSS [forkd](https://github.com/deeplethe/forkd) reference ~1 ms/child and ~150 ms live branch — **all vendor-published, unverified**.
- **Pass/fail:** PASS if first-party fork P99 and density land within ~2× of the published anchors *and* the uniqueness hook is correct (no cross-fork RNG/MAC collisions). FAIL → the Linux fork-tier economics (and the Phase 4 cost model in [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md)) need rework; fall back to snapshot-restore-only.

### 🔬 SPIKE — dual-channel latency PoC

- **Method:** a thin PoC of the WebRTC dual-channel: a structured event on the data channel + an on-demand NVENC track on an **Ada L4** (the encode tier — never A100/H100/H200/B200, which carry zero NVENC engines, [D11](05-tech-decisions.md)). Measure glass-to-glass latency same-region for (a) structured-only Tier 0 and (b) Tier 2 video, decomposed by stage (capture → encode → SFU → decode → render). NVENC capability is documented in the [NVIDIA Video Codec SDK application note](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html).
- **Pass/fail:** PASS if same-region glass-to-glass lands in the ~50–120 ms target (vendor-published target) for video and structured Tier 0 stays in the ~20 kbps class. FAIL → revisit codec/transport choices in [D4](05-tech-decisions.md) before committing the Control Panel UX.

### Success criteria

- A warm pool serves CoW-forked Linux Sandboxes with sub-second time-to-first-action (first-party measured).
- An agent runs a task while a human watches live in the Control Panel, approves an `install.privileged` unlock via the HITL card, and the approval is recorded as a first-class `permission` replay event ([D6](05-tech-decisions.md)/[D5](05-tech-decisions.md)).
- A `.skn` can be **branched** from step N and re-run (counterfactual), proving instant-reset == branch ([D1](05-tech-decisions.md)/[D5](05-tech-decisions.md)).
- All three Phase-1 spikes have first-party verdicts feeding the ADRs in [05](05-tech-decisions.md).

### Dependencies

- The OSS `kubernetes-sigs/agent-sandbox` CRD (gVisor/Kata + warm pools) — Linux substrate. Note that, as of mid-2026, this SIG project is in active development and not yet production-hardened for every workload; the warm pool is mandatory, not optional, especially for the Kata runtime class.
- **Vault** (or any cloud KMS / SPIFFE-SPIRE) — the secret broker.
- **NICE DCV** — evaluated as the build-vs-buy option for the high-fidelity pixel channel ([D11](05-tech-decisions.md)); Phase 1 may ship a self-built NVENC track and defer the DCV decision to Phase 4.
- A generic `tool_runner` policy boundary — the egress-allowlist seam the permission model aligns with.

---

## Phase 2 — Eval layer + OSWorld-Verified conformance + first CUA eval/model-training users

**Objective:** Layer the **eval service** on the same runtime (the north-star inversion of OSWorld, [D7](05-tech-decisions.md)) and **land the first users** — CUA eval and model-training teams — via **replay-as-training-data** ([D12](05-tech-decisions.md)). This is where Shinken proves the "one platform, eval layered on production" thesis and earns its first real adoption.

### Goals

- **Eval layer = thin orchestration on the runtime ([D7](05-tech-decisions.md)):** a typed **verifier DAG** (not OSWorld's stringly-typed `getattr` evaluators — see the teardown in [03](03-osworld-analysis.md)); **programmatic-primary verification + a constrained model-verifier fallback**; a **golden snapshot per task**; **N≥5 CoW-forked replicas → pass@k / pass^k with confidence intervals** (cheap *because* Phase 1's fork works); and **readiness probes, not sleeps** (verify-then-retry and explicit wait-for-actionability over fixed sleeps, the production-ops reliability pattern documented for [Playwright auto-wait](https://www.qabash.com/playwright-auto-waits-selenium-flake-killer/)).
- **OSWorld-Verified conformance ([D7](05-tech-decisions.md)):** ship it as a built-in suite with **task + grader + environment versioned together**, treating the grader as a *tested artifact* — explicitly heeding the 300+ grader/task bugs that [OSWorld-Verified](https://xlang.ai/blog/osworld-verified) fixed over 15 months. SOTA anchor for calibration: the leaderboard reports a top score of ~83% on OSWorld-Verified, above the ~72.4% human baseline (vendor/leaderboard, unverified). Add an independent-verification policy so Shinken does not republish unreproduced vendor scores.
- **Replay-as-training-data ([D5](05-tech-decisions.md)/[D7](05-tech-decisions.md)/[D12](05-tech-decisions.md)):** a `.skn` capture pipeline producing RL/SFT trajectories. The wedge is that every eval run *and* every production session becomes versioned, branchable training data on the same `.skn` substrate — the synchronized screen+input+a11y → state-action-CoT recording pattern proven by [OpenCUA](https://github.com/xlang-ai/OpenCUA). This is the concrete adoption argument for the first eval/model-training users, who otherwise pay to acquire OSWorld-style datasets.
- **MCP facade ([D8](05-tech-decisions.md)):** the optional MCP facade at two altitudes (granular `create_session/act/observe/snapshot/grant_permission`; agent-task `run_task`) for model-agnostic hosts, with OAuth 2.1 ([MCP authorization spec](https://modelcontextprotocol.io/specification/draft/basic/authorization)) — but **never** routing the high-frequency action/observation/video loop or media through MCP. This is what lets MCP-native agent hosts and toolkits drive Shinken without taking on the hot loop.

### Deliverables

- Eval service (verifier DAG, golden snapshots, pass@k/pass^k harness, CI runner on warm pools).
- The OSWorld-Verified suite, versioned task+grader+env, with the independent-verification policy.
- A `.skn` → training-data exporter (RL trajectory + SFT formats).
- The MCP facade (granular + agent-task altitudes) + py/ts SDKs over the streaming transport ([D8](05-tech-decisions.md)).

### Success criteria

- OSWorld-Verified runs end-to-end on Shinken with N≥5 forked replicas per task and reports pass@k/pass^k with confidence intervals, reproducibly.
- At least one grader bug is caught by the "grader-as-tested-artifact" gate before it corrupts a score (proving the OSWorld-Verified lesson is internalized).
- At least one external CUA eval or model-training user consumes Shinken `.skn` output as eval results and/or training data — the concrete "land the first users" milestone from [D12](05-tech-decisions.md). Eval-benchmark background lives in [`../notes/eval-benchmarks.md`](../notes/eval-benchmarks.md).

### Dependencies

- Warm pools (parallel eval replicas) on the `agent-sandbox` CRD substrate from Phase 1.
- Reuses all Phase-1 spikes; no new gating spike.

---

## Phase 3 — Cross-OS (Windows + macOS tiers)

**Objective:** Deliver the cross-platform desktop promise ([D10](05-tech-decisions.md)) by adding the **heavier, longer-lived** Windows and macOS tiers behind the *same* control plane, the *same* Guest Runtime contract, and the *same* ACI ([D10](05-tech-decisions.md)). These tiers come after the eval layer because they are licensing-gated and low-density, and the earliest users (Linux/OSWorld-first) do not need them on day one.

### Goals

- **Windows tier ([D1](05-tech-decisions.md), heavier):** Cloud Hypervisor/QEMU + virtio-win + the Guest Runtime (UIA → the unified `Element` schema, [D3](05-tech-decisions.md)). Longer-lived and snapshot-light. **Licensing-gated** — resolve the open question of whether commodity multi-tenant Windows is even permissible (no commodity multi-tenant desktop Windows exists without specific licensing programs; the realistic options are per-core Datacenter licensing for density vs customer-supplied/BYOL). This is a *gate*, not an assumption. The Windows golden-image pipeline (cloudbase-init + sysprep) follows the [Cloud Hypervisor Windows guide](https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/windows.md).
- **macOS tier ([D1](05-tech-decisions.md), scarce premium):** **Apple Virtualization.framework on Apple hardware** (the tart/lume pattern). Hard caps acknowledged: Apple-hardware-only, **2 VMs per host** (Apple EULA, [confirmed by the Apple containerization issue tracker](https://github.com/apple/containerization/issues/737)), TCC pre-grant. Plan as **low-density standing pools**, not a fork tier — macOS/Windows fast-reset is largely infeasible with today's tooling.
- **Per-OS handler-factory beneath one ACI ([D10](05-tech-decisions.md)):** AT-SPI (Linux) / UIA (Windows) / AX (macOS) / CDP (browser) all normalize into the one `Element` schema; the action schema and event stream are unchanged. macOS enforcement = Seatbelt + TCC; Windows = restricted token + per-workspace capability-SID ([D6](05-tech-decisions.md)).

### 🔬 SPIKE — macOS-reset feasibility & Windows-licensing

- **macOS:** measure whether Apple Virtualization.framework supports any usefully-fast snapshot/restore (the expectation is that it largely does not). PASS = a standing-pool model with acceptable warm-attach latency; FAIL-as-expected = document macOS as a *managed bare-metal standing pool only*, with no fork. Note the standing-pool economics: a leased Apple host carries a long minimum-billing window (e.g. cloud Mac dedicated hosts bill a 24-hour minimum, ~$0.65/hr → ~$4,700/mo, vendor-published, unverified).
- **Windows-licensing:** a legal/procurement spike producing a yes/no on commodity multi-tenant Windows and the per-core-vs-BYOL cost shape. This gate decides whether Windows ships as a hosted tier or as customer-licensed-only.

### Deliverables

- A Windows Guest Runtime (UIA handler) + Cloud Hypervisor/QEMU + a virtio-win image pipeline.
- A macOS Guest Runtime (AX handler) + a Virtualization.framework standing pool on Apple hardware.
- Per-OS OS-enforcement (Seatbelt/TCC; restricted token/capability-SID) wired into the [D6](05-tech-decisions.md) permission model.
- A [WindowsAgentArena](https://arxiv.org/abs/2409.08264) conformance suite added to the eval layer ([D7](05-tech-decisions.md)).

### Success criteria

- The same agent + same ACI drives a task on Linux, Windows, and macOS with no client-side branching ([D10](05-tech-decisions.md) proven: one ACI, per-OS factory beneath).
- The macOS-reset and Windows-licensing spikes have explicit verdicts in [05](05-tech-decisions.md); the roadmap and cost model are updated for whatever they return.
- WindowsAgentArena runs on the Shinken eval layer.

### Dependencies

- An Apple hardware pool (macOS); a Windows licensing resolution (legal/procurement).
- Reuses the `agent-sandbox` substrate, Vault, the Control Panel, and the eval layer from Phases 1–2.

---

## Phase 4 — Cloud ultra-high-concurrency + multi-tenant + optional GPU/NVENC + GPU-TEE

**Objective:** Scale from "works in one region for one team" to the **ultra-high-concurrency, multi-tenant cloud platform** the scope promises, and light up the **optional NVIDIA GPU tier** ([D11](05-tech-decisions.md)) — including the trusted-multi-tenant GPU-TEE substrate that no competitor holds.

### Goals

- **Multi-tenant control plane hardening ([D9](05-tech-decisions.md)):** per-(tenant, workload, model) **token-bucket + weighted-fair-queuing + a global ceiling** in Redis/Lua at the Action Gateway ([rate-limiting pattern](https://www.truefoundry.com/blog/rate-limiting-ai-agents-preventing-llm-api-exhaustion)); **dual-timer sessions** (idle ~15 min reset-on-activity; max-lifetime ~4–8 h) with **auto-suspend-to-snapshot on idle** — *idle is the dominant cost line* (naive minimum-billing keep-alives inflate compute up to ~5×, vendor-published, unverified; the dual-timer defaults of 900 s idle / 28,800 s max come from [Bedrock AgentCore lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)). A heartbeat-based **reaper** GCs orphans; sandbox health is a **circuit-breakable dependency** (kill + replace from the warm pool).
- **OTel-GenAI telemetry, native ([D9](05-tech-decisions.md)):** one span stream (`invoke_agent` / `chat` / `execute_tool`, per the [OpenTelemetry GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/)) with `gen_ai.conversation.id` = session, plus custom `gui.*` and tenant-id attributes, driving tracing + cost attribution + per-tenant metering from a single source. Meter **tokens + sandbox-seconds + egress** separately (the three real cost lines), and gate full prompt/screenshot capture behind a per-tenant PII flag.
- **SFU fan-out streaming at scale ([D4](05-tech-decisions.md)):** an encode-once SFU ([LiveKit-style](https://docs.livekit.io/reference/internals/livekit-sfu/)). Egress/TURN — not codec — dominates cost: at 100k concurrent, roughly ~$0.8M/mo (AV1 with screen-content tuning) vs ~$4.9M/mo (H.264 office video) vs negligible for the structured channel (vendor-derived from public egress tiers, unverified). The structured-first default ([D3](05-tech-decisions.md)) is what makes ultra-high concurrency affordable; see [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md).
- **GPU encode + accel tiers ([D11](05-tech-decisions.md)):** the encode tier on **Ada L4** (density) / **L40S** (premium 4K, AV1 + render) — **never** on A100/H100/H200/B200, which carry zero NVENC engines (public NVIDIA fact). Two GPU pools: **time-sliced vGPU** (light desktops, density) + **MIG-backed / Confidential Containers** (isolation-sensitive). GPU is **opt-in** — most agent and browser tasks ride the CPU-only Linux fork tier from Phase 1. AV1 on Ada saves ~40% bitrate vs H.264 at equal quality (vendor-measured, unverified), and the consumer 8-session encode cap does not apply to qualified datacenter GPUs.
- **GPU-TEE trusted tier ([D11](05-tech-decisions.md)):** **GPU-TEE + remote attestation ([NVIDIA NRAS](https://docs.nvidia.com/attestation/index.html)) + Confidential Containers** for confidential multi-tenant GPU agents, building on publicly-available NVIDIA Confidential Computing (Hopper H100 CC, Blackwell TEE-I/O). The permission panel ([D6](05-tech-decisions.md)) hands out **MIG-backed GPU** as the per-session capability for isolation-sensitive workloads, with an attestation verified *before* the `gpu` capability unlock.

### 🔬 SPIKE — NVENC-density & GPU-TEE-attestation

- **NVENC-density:** measure concurrent NVENC streams per **L4/L40S** at agent resolutions (1080p/1440p) — the consumer "8-session cap" does **not** apply to datacenter GPUs, but the real number is unverified. PASS = a density that makes the ~$0.8M/mo AV1 anchor plausible.
- **GPU-TEE-attestation:** confirm an end-to-end NRAS attestation + Confidential-Containers flow for one GPU agent session on B200/H100. PASS = a verifiable attestation handed to the permission panel before a `gpu` capability unlock.

### Deliverables

- A hardened multi-tenant Action Gateway (rate-limit, WFQ, combined budget, Cedar policy) + reaper + circuit breakers.
- An OTel-GenAI pipeline with three-axis cost metering.
- SFU fan-out streaming; the NICE-DCV-vs-custom-WebRTC build-vs-buy decision finalized for the pixel channel.
- vGPU/MIG GPU pools + the NVENC encode tier; the GPU-TEE / NRAS / Confidential-Containers trusted tier.
- Multi-region warm-pool autoscaling on arrival rate.

### Success criteria

- Sustained multi-tenant load with per-tenant fairness (noisy-neighbor contained), and first-party $/sandbox-hour and NVENC-streams/GPU numbers replacing the vendor anchors.
- A confidential GPU agent session runs with NRAS attestation gating the `gpu` capability unlock ([D6](05-tech-decisions.md) + [D11](05-tech-decisions.md)).
- The cost model is validated: idle-suspend + structured-first deliver the projected concurrency economics ([`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md)).

### Dependencies

- **NICE DCV** (the pixel-channel build-vs-buy, finalized), **NRAS** + **Confidential Containers** + **GPU-TEE** (trusted tier), **vGPU/MIG** (density/isolation), the `agent-sandbox` substrate, and Vault.
- The consolidated threat model in [`08-threat-model.md`](08-threat-model.md) must be GA-ready before multi-tenant launch (multi-tenant noisy-neighbor, CoW page-dedup leakage, and shared-GPU/NVENC side channels are Phase-4-blocking risks).

---

## Later — Android, multi-player

These are **explicitly post-v1** (Android is roadmap, not v1; multi-player is an open scope decision).

- **Android ([D10](05-tech-decisions.md) roadmap):** redroid/Cuttlefish/emulator quick-boot snapshots. The hard problem is reconciling the **touch/gesture action space** with the desktop-pointer ACI under one schema. Gate: a touch-schema spike deciding whether touch extends the [D2](05-tech-decisions.md) tagged-union or needs a sibling verb set. An [AndroidWorld](https://arxiv.org/abs/2405.14573) conformance suite is added to the eval layer.
- **Multi-player / non-exclusive computer-use (open scope decision):** separate human + agent cursors/focus on one desktop (the per-window-streaming and multi-participant-WebRTC patterns from tools like Xpra and neko). This **breaks the single-cursor assumption baked into the current ACI**, so it is a deliberate in/out decision, not a feature backlog item. Recommendation: document as a **non-goal for v1** and revisit only if a user demands it (e.g. a collaborative-annotation workflow), because it changes the input/streaming architecture. Tracked in [`../notes/open-questions.md`](../notes/open-questions.md).

---

## Cross-phase: the three recurring de-risking spikes

| Spike | What it kills/confirms | Phase | Pass metric (vs vendor anchor) | Reconciles to |
|-------|------------------------|-------|--------------------------------|---------------|
| **a11y-coverage** | Structured-first observation (the bandwidth differentiator) | 0 (gate for 1) | Net ~6× token reduction *after* pixel fallback (~25k vs ~150k tok/task, unverified) | D3 |
| **CoW-fork density** | Linux fork-tier economics + the cost model | 1 | Fork P99 and private-RSS density within ~2× of the Morph/Firecracker anchors; uniqueness reseed correct | D1, D9 |
| **dual-channel latency** | The WebRTC streaming budget + Control Panel UX | 1 | Same-region glass-to-glass ~50–120 ms (video); Tier 0 ~20 kbps | D4 |
| macOS-reset feasibility | macOS tier shape (standing pool vs fork) | 3 | Acceptable warm-attach latency, or documented bare-metal-pool-only | D1, D10 |
| Windows-licensing | Whether Windows is a hosted tier at all | 3 | Legal yes/no + per-core-vs-BYOL cost | D1, D10 |
| NVENC-density | The cloud streaming cost model | 4 | Concurrent streams/L4-L40S making the ~$0.8M/mo AV1 anchor plausible | D11, D4 |
| GPU-TEE-attestation | The trusted multi-tenant GPU tier | 4 | End-to-end NRAS + Confidential-Containers attestation gating a `gpu` unlock | D11, D6 |

Every spike has a **kill condition**. If a11y-coverage fails, D3 flips to pixels-first before Phase 1 builds on it. If CoW-fork density fails, the Phase 4 cost model and the Linux-default-tier positioning (D1) are revised. This is the entire point of the phasing: the architecture's load-bearing bets are tested *before* the platform leans on them.

---

## Open questions carried into the roadmap (do not paper over)

These remain unresolved and are tracked as roadmap risks rather than hidden; details and tracking live in [`../notes/open-questions.md`](../notes/open-questions.md).

- **a11y coverage on Electron/Qt/canvas/games** — the load-bearing unverified assumption; the Phase 0 spike resolves it.
- **macOS/Windows fast-reset** — largely infeasible with today's tooling; the Phase 3 spike sets expectations.
- **No first-party perf numbers yet** — every speed/density/cost figure in this document is **vendor-published, unverified** until the Phase 0/1/4 spikes replace them; see the measurement plan in [`09-economics-and-build-vs-buy.md`](09-economics-and-build-vs-buy.md).
- **Consolidated threat model** — prompt-injection → exfiltration, sandbox escape, multi-tenant noisy-neighbor / side-channel, CoW page-dedup leakage; needed before Phase 4 multi-tenant GA. The Rule-of-Two + stripped-input injection classifier + default-deny egress controls ([D6](05-tech-decisions.md), drawing on [Meta's Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/) and [Anthropic's prompt-injection defenses](https://www.anthropic.com/research/prompt-injection-defenses)) are the starting baseline, but a ~1% prompt-injection attack-success-rate is *large* at ultra-high concurrency. Full analysis in [`08-threat-model.md`](08-threat-model.md).
- **Windows-in-cloud licensing & macOS 2-VM/host economics** — these shape cost and the Phase 3 roadmap; resolved by the Phase 3 spikes.
- **Protocol/event-schema versioning + upcasting** — must be specified before `.skn` files outlive a single ACI version ([D5](05-tech-decisions.md)).
- **Multi-player / non-exclusive computer-use** — an explicit in/out scope decision, defaulted to non-goal for v1 above.
