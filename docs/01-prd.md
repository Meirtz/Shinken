# Shinken — Product Requirements Document (PRD)

> **Status:** drafting · **Date:** 2026-05-30
> **Siblings:** [00 Vision](00-vision.md) · [02 Architecture](02-architecture.md) · [03 OSWorld teardown](03-osworld-analysis.md) · [04 Landscape](04-landscape.md) · [05 Tech decisions / ADRs](05-tech-decisions.md) · [06 Roadmap](06-roadmap.md) · [07 Glossary](07-glossary.md) · [08 Threat model](08-threat-model.md) · [09 Economics & build-vs-buy](09-economics-and-build-vs-buy.md)

Shinken is an AI-native, cross-platform **Sandbox runtime + Control Plane + Control Panel** for computer-use agents — a production-grade, streaming-first successor to OSWorld that serves *both* production agent deployment *and* evaluation on one runtime. This PRD enumerates the personas and their top journeys, then the **functional requirements** grouped by subsystem (Sandbox lifecycle, ACI actions [D2], observation [D3], streaming [D4], replay [D5], permission panel [D6], eval [D7], control plane [D9], interfaces/SDK/MCP [D8]), the **non-functional requirements** (concurrency, latency budgets, cost, security/isolation, availability, multi-tenancy, compliance), explicit **in/out-of-scope**, and **success KPIs**. Every requirement carries an ID (`FR-<SUBSYS>-N` / `NFR-<CLASS>-N`) and is reconciled to its governing decision **D1–D12** (see [05 Tech decisions](05-tech-decisions.md)). Numeric speed/density/cost figures are marked **(vendor-published, unverified)** unless first-party; these gate a first-party measurement plan and are *not* load-bearing for v1 commitments.

---

## 1. Personas & top user journeys

Shinken's north star (D12) is *one* platform with a production runtime and an eval layer layered on top. The first adopters are teams **building and evaluating** computer-use agents — model labs, eval researchers, and RPA builders — for whom the event-sourced replay (`.skn`) doubles as RL/SFT trajectory data, a concrete adoption wedge. The personas below span both halves of the north star.

| # | Persona | Goal | Primary surface | Governing decisions |
|---|---------|------|-----------------|---------------------|
| P1 | **Agent developer** | Build and ship a computer-use agent against a stable, provider-agnostic runtime | py/ts SDK over the streaming ACI (D8); the Operator contract (§ below) | D2, D3, D8, D9 |
| P2 | **CUA eval researcher** (who also harvests replay as training data) | Run reproducible, massively-parallel benchmarks *and* mine the resulting trajectories as SFT/RL data | Eval service + Control Panel leaderboard (D7); `.skn` bundles + branch/fork (D5) | D1, D5, D7, D9 |
| P3 | **Platform admin / operator (SRE)** | Run the fleet at ultra-high concurrency within cost and SLOs; administer tenants and policy | Control plane telemetry, Fleet Manager, Action Gateway, managed policy (D9) | D1, D6, D9, D11 |
| P4 | **Human supervisor** | Watch a running agent, approve/deny privileged actions, take over, and later audit | Control Panel: live view + Permission Panel + replay/scrub (D4, D6, D5) | D4, D5, D6 |
| P5 | **MCP-host integrator** *(supporting)* | Drive Shinken from a model-agnostic agent host | MCP facade at two altitudes (D8) | D8 |

The four primary personas (P1–P4) map directly to the brief: an *agent developer*; a *CUA eval/researcher who also harvests replay as training data*; a *platform admin/operator*; and a *human supervisor*. P5 (MCP-host integrator) is a supporting persona that exercises the optional model-agnostic facade. The human supervisor (P4) absorbs two adjacent roles — the **live approver** (real-time permission gating, takeover) and the **auditor/compliance reviewer** (post-hoc reconstruction) — because both operate over the same event-sourced replay surface and the same permission timeline.

```
        ┌─────────────────────────── Shinken ───────────────────────────┐
 P1 ───▶ │  SDK / Operator ──▶ Action Gateway ──▶ ACI ──▶ Guest Runtime  │
 P5 ───▶ │  MCP facade ───────────┘                  (Sandbox: L/W/mac)  │
         │                                ┌── event plane (replay log) ──┐│
 P4 ───▶ │  Control Panel  ◀── streaming ─┤   control · event · media    ││
         │   (live view, Permission Panel, replay/scrub, takeover)       ││
 P2 ───▶ │  Eval service ──▶ N×CoW forks ──▶ verifier DAG ──▶ .skn data  ││
 P3 ───▶ │  Fleet Manager · warm pools · budgets · telemetry · policy    ││
         └────────────────────────────────────────────────────────────────┘
```

### Top journeys

**J1 — Deploy a production agent (P1).** The developer authors an **Operator** (the client-side adapter that drives a Sandbox for a given agent/model and is the human-takeover seam) against the generated SDK. `create_session` claims a warm Sandbox (D9); the agent loop drives the typed ACI (D2) over the streaming transport; observation defaults to the structured a11y/DOM tier (D3); privileged actions surface as Permission Panel cards (D6); and the whole run is recorded as a `.skn` replay (D5). The agent loop stays **provider-agnostic** behind the Operator contract — no vendor lock-in.

**J2 — Run a benchmark suite (P2).** The eval researcher picks a pinned conformance suite (OSWorld-Verified 369 tasks, WindowsAgentArena ~154, AndroidWorld 116, WebArena 812). Each task forks N≥5 CoW replicas from an immutable golden snapshot (D7, D1); a typed verifier DAG grades end + milestone state (D7); and the Control Panel reports Average / pass@k / pass^k with confidence intervals — never single-run pass@1, which hides 10–30 points of variance.

**J3 — Capture training data (P2, second hat).** The same researcher (or a colleague on the training side) runs rollouts whose every step is recorded to `events.jsonl` with the decision channel in OpenTelemetry GenAI semantic conventions (D5). Replay-as-training-data and branch-from-step-N counterfactuals feed RL/SFT pipelines. Because instant reset and replay-branching are the *same* primitive (D1, D5), generating diverse counterfactual rollouts from a single golden state is cheap.

**J4 — Supervise a privileged run (P4 as live approver).** The supervisor attaches to a live Session; the structured event stream and an on-demand video track render glass-to-glass (D4). When the agent hits a capability boundary, the session pauses and streams a blocking approval card (**Run / Escalate / Deny**); on takeover, the human drives the *same* Sandbox through the *same* Operator seam.

**J5 — Audit / debug a run (P4 as auditor).** The supervisor opens a `.skn` in the replay panel, scrubs the master logical clock, inspects the Thought–Action–Observation step list and the permission/approval markers (the highest-priority marker class), and forks from any checkpoint to re-run a counterfactual.

**J6 — Operate the fleet (P3).** The SRE watches per-(image, region, tier) warm pools, Action Gateway rate-limit/budget telemetry, and circuit-breaker kill-and-replace events (D9), tuning warm-pool depth against the idle-cost driver, and administers tenant budgets and managed policy.

**J7 — Integrate via MCP (P5).** The integrator points a model-agnostic host at the MCP facade — either granular tools (`create_session` / `act` / `observe` / `snapshot` / `grant_permission`) or the agent-task altitude (`run_task` → streamed steps), over OAuth 2.1 — but the high-frequency action/observation/video loop **never** routes through MCP (D8).

---

## 2. Functional requirements

### 2.1 Sandbox lifecycle (D1, D9, D10)

A **Sandbox** is one isolated guest computer; a **Session** is a live attach/run against it. Isolation is tiered and substrate-pluggable, routed by *(OS × needs-GPU × needs-fast-fork)* (D1). The default Linux container fast-path is the open-source [`kubernetes-sigs/agent-sandbox`](https://github.com/kubernetes-sigs/agent-sandbox) CRD pattern running pods under [gVisor](https://gvisor.dev/) / [Kata](https://katacontainers.io/) runtime classes with pre-warmed pools.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-SBX-1 | The control plane MUST route each Sandbox request to a substrate tier by the triple *(OS × needs-GPU × needs-fast-fork)*: Linux fork tier, Linux/Windows/macOS longer-lived tiers, or GPU tier G. | D1, D10 |
| FR-SBX-2 | The Linux fork tier MUST support **fork-from-snapshot** reset (MAP_PRIVATE CoW + userfaultfd, warm parent pool), targeting <30 ms VMM restore and sub-second time-to-first-action *(vendor-published, unverified: [Firecracker snapshot restore 5–30 ms](https://firecracker-microvm.github.io/); Morph Infinibranch fork P99 ~1.3 ms at 1,000 concurrent)*. | D1, D9 |
| FR-SBX-3 | The headless Linux fast-path MAY run on [Firecracker](https://github.com/firecracker-microvm/firecracker); the Linux **desktop** path (display / virtio-gpu) MUST run on QEMU-microvm or crosvm, since Firecracker ships no display/GPU device model (exactly 5 virtio devices, zero graphics/PCIe/VFIO). | D1 |
| FR-SBX-4 | Every fork MUST run a **post-fork uniqueness hook**: reseed the kernel CSPRNG and userspace PRNGs (drive VMGenID), regenerate MAC/IP/hostname/boot-id, and resync the clock — the documented hard part of CoW forking. | D1, D7 |
| FR-SBX-5 | Windows Sandboxes MUST run on [Cloud Hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor)/QEMU + virtio-win as longer-lived, snapshot-light instances and MUST be **licensing-gated** (Windows Server Datacenter per-core, or BYOL on dedicated hosts); image build MUST use a sysprep / cloudbase-init golden-image pipeline. | D1, D10 |
| FR-SBX-6 | macOS Sandboxes MUST run on Apple Virtualization.framework on Apple hardware, enforce the **≤2 macOS VMs/host** hard cap and Apple-HW-only constraint, pre-grant TCC at image-build time, and be capacity-planned as scarce low-density standing pools. | D1, D10 |
| FR-SBX-7 | GPU tier G Sandboxes MUST use VFIO passthrough or vGPU/MIG on Cloud Hypervisor/QEMU, MUST be treated as longer-lived (no fast snapshot — VFIO/vGPU device state is non-snapshottable), and the trusted variant SHOULD use a GPU TEE + remote attestation + Confidential Containers. | D1, D11 |
| FR-SBX-8 | One **Guest Runtime** contract (the in-Sandbox daemon `shinkend`) MUST execute the ACI and emit the event stream identically across all OSes, with a per-OS handler-factory beneath. The host↔guest transport MUST be **virtio-vsock**, never HTTP polling. | D4, D10 |
| FR-SBX-9 | Sessions MUST use the **dual-timer** model: idle timeout ~15 min (reset-on-activity), max-lifetime ~4–8 h (non-resetting), with **auto-suspend-to-snapshot on idle** (idle is the dominant cost driver). State MUST be snapshotted before max-lifetime reap so long tasks resume seamlessly. | D9 |
| FR-SBX-10 | At Session end, a per-session microVM MUST be destroyed and its memory sanitized to eliminate cross-tenant contamination. | D9, NFR-SEC |
| FR-SBX-11 | **Instant reset and replay-branching MUST be the same primitive** — forking a snapshot node serves both. | D1, D5 |

### 2.2 ACI actions (D2)

The **ACI** is the versioned protocol plus typed action/observation schema. The action schema is one canonical typed tagged-union discriminated by `verb`. OSWorld proves the need: it accepts five incompatible action representations and string-translates all of them; Shinken hoists that translation into typed adapters.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-ACI-1 | The action schema MUST be a single tagged union of ~16 `verb`s, expressed as a versioned JSON Schema / protobuf with `schema_version` (semver), defined as a near-superset of the Anthropic `computer` tool grammar and OpenAI's `computer_call`. | D2 |
| FR-ACI-2 | Every spatial verb's `target` MUST be a discriminated union `oneof{ point_px{x,y} \| point_norm{x,y∈0..1} \| element_ref{handle,source} }`, so one verb serves pixel models, normalized models, and ref-based models. | D2, D3 |
| FR-ACI-3 | Coordinate normalization MUST live in the protocol, once: every observation carries an explicit **`CoordinateSpace`** `{origin, logical_width/height, device_pixel_ratio, image_width/height, scale_factor, mode}`. | D2, D3 |
| FR-ACI-4 | Shinken MUST ship **version-pinned, bidirectional adapters** as the *only* model-facing surface: [Anthropic computer-use](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool) (`computer_20241022` / `20250124` / `20251124`, plus `bash` + `text_editor`), [OpenAI computer-use](https://developers.openai.com/api/docs/guides/tools-computer-use) (`computer_call` / `computer_call_output`), UI-TARS, and OSWorld `computer_13`. | D2 |
| FR-ACI-5 | **Code-as-action** (`exec` / `bash` / `edit`) MUST be a separate, **off-by-default** capability class behind the `tool_runner` policy boundary (the agent loop runs *outside* the Sandbox; tool calls route through a controlled API that enforces the egress allowlist before executing). | D2, D6 |
| FR-ACI-6 | The session handshake MUST perform **capability negotiation**: the server advertises `{schema_version, supported_verbs[], supported_targets[], coordinate_modes[], max_long_edge, escape_hatch_caps[], observation_types[]}` and the client/adapter selects a compatible subset. | D2 |
| FR-ACI-7 | Key/modifier normalization MUST use one tested canonical table (W3C key names; `meta` → Cmd/Win/Super per OS); every key validated against an allowlist; modifiers carried orthogonally, not overloaded into the verb. | D2, D10 |
| FR-ACI-8 | An action message MUST be an ordered `list[Action]` with one correlation `action_id` (call_id), executed in order; `mouse_down/up`, `key_down/up`, `hold_key`, and `drag` MUST be modeled explicitly. | D2, D5 |
| FR-ACI-9 | The runtime MUST bundle a **wait → act → verify** semantic step: explicit wait-for-actionability, execute, then verify the expected post-state via observation diff before reporting success; retry only on verified failure. | D2, D9 |
| FR-ACI-10 | Manipulation verbs MUST accept element refs by default; raw `(x,y)` is reserved for the pixel rung only (element-ref actions are replay-stable and deterministic). | D2, D3 |

### 2.3 Observation (D3)

Observation is **structured-first, layered escalation** — the bandwidth/cost/latency crux of the platform. The default is a normalized accessibility/DOM tree, never pixels; pixels are a metered escalation.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-OBS-1 | Default observation (Rung 0) MUST be a **normalized cross-OS a11y/DOM tree diff** (AT-SPI / UIA / AX / CDP) projected onto one `Element{ref,role,name,value,states,bbox,source,backend_id,parent_ref,children_refs}` schema with stable per-session refs. | D3 |
| FR-OBS-2 | Escalation rungs MUST be explicit and requestable: Rung 1 = **Set-of-Marks / OmniParser** (server-side, on-demand); Rung 2 = region/zoom pixels; Rung 3 = full frame. | D3, D11 |
| FR-OBS-3 | The [OmniParser](https://github.com/microsoft/OmniParser)/Set-of-Marks parser MUST run server-side **on demand** (triggered on low a11y coverage or explicit agent request), never per-frame by default; budget ~0.6 s/frame *(vendor-published, unverified)*. | D3, D11 |
| FR-OBS-4 | Observations MUST stream **full-snapshot + typed delta**: emit `a11y_full` / `screenshot_full` periodically, between them `a11y_delta` (added/removed/changed nodes), triggered on change/focus — never on a fixed clock. | D3, D5 |
| FR-OBS-5 | The structured/pixel duality MUST be a property of the action grammar (one verb takes a ref OR a coord), never two parallel APIs. | D2, D3 |
| FR-OBS-6 | Structured-only MUST NOT ship alone: vision + grounding is a first-class fallback because a11y goes blind on Electron/Qt/canvas/WebGL — **the load-bearing unverified assumption**, requiring a first-party a11y-coverage measurement spike before any density/cost commitment. | D3 |
| FR-OBS-7 | Each observation MUST carry `{obs_id, ts, session_id, cause(action_id\|push), display, tree_mode, elements\|delta, marks?, CoordinateSpace}` and be `action_id`-correlated to its causing action. | D3, D5 |
| FR-OBS-8 | The diff-based observation stream MUST BE the same append-only event stream used for live view, replay, and permission-audit (one source of truth). | D3, D4, D5 |
| FR-OBS-9 | Sensitive element values MUST be maskable at capture, before they enter the stream or replay. | D3, D6, NFR-COMP |

### 2.4 Streaming (D4)

Streaming is a **single-PeerConnection WebRTC, dual-transport** design. The reliable-ordered data channel carries structured events (and *is* the replay log); the media track carries on-demand hardware-encoded video.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-STR-1 | Each Session MUST use one [WebRTC](https://github.com/pion/webrtc) PeerConnection carrying (a) a **reliable-ordered data channel** = the structured action/observation/permission event stream (this IS the replay log), and (b) an **on-demand media track**. | D4, D5 |
| FR-STR-2 | The media track MUST be hardware-encoded ([NVENC](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html) on the GPU tier) H.264/AV1, screen-content-tuned; [AV1](https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/) negotiated only where the client advertises HW AV1 decode, else HEVC/H.264. | D4, D11 |
| FR-STR-3 | The encode tier MUST run on encode-capable GPUs (Ada **L4** for density / **L40S** for premium 4K/AV1 + render), and **NEVER** on A100/H100/H200/B200 (which ship zero NVENC engines — [public NVIDIA fact](https://en.wikipedia.org/wiki/Nvidia_NVENC)); no MIG for the encode tier. | D11 |
| FR-STR-4 | Fan-out MUST be **encode-once at an SFU** ([LiveKit](https://docs.livekit.io/reference/internals/livekit-sfu/)-style), never per viewer; encode count = number of distinct desktops, not number of reviewers. | D4 |
| FR-STR-5 | Signaling MUST be **WHIP** ([RFC 9725](https://www.rfc-editor.org/rfc/rfc9725.html), Sandbox→SFU ingest) / **WHEP** (SFU→browser egress), extended to negotiate the bidirectional data channel; the receiver jitter buffer MUST be minimized (playout-delay min≈0). | D4 |
| FR-STR-6 | The three observation tiers MUST map to bandwidth tiers: Tier 0 structured (~20 kbps), Tier 1 Set-of-Marks, Tier 2 video; structured is the always-on default. | D3, D4 |
| FR-STR-7 | The system MUST **record-while-stream**: a GStreamer `tee` (or equivalent track egress) after the encoder writes a fragmented MP4 / CMAF on IDR boundaries so the recording is crash-safe and MSE-replayable. | D4, D5 |
| FR-STR-8 | Reconnection MUST be deliberate: monitor `iceConnectionState`, call `restartIce()` on failure with a full re-offer fallback, and request a PLI keyframe on every (re)connect so the viewer paints immediately. | D4 |
| FR-STR-9 | TURN relay MUST be budgeted and contained (expect ~18–35% of connections to relay); video MUST be event-driven to cut idle relay egress. | D4, NFR-COST |
| FR-STR-10 | The platform MUST support [NICE DCV](https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html) (NVENC + QUIC/UDP, browser client, auto-adaptation) as a **build-vs-buy** option for the high-fidelity pixel channel, behind the same media-plane contract as the custom WebRTC + NVENC pipeline. | D4, D11 |

### 2.5 Replay (D5)

Replay is the event stream + bisected snapshots, packaged as a self-contained `.skn` bundle (a ZIP, on the Playwright-trace model). It is explicitly **not bit-deterministic** — full-desktop determinism is impractical; Shinken uses pragmatic state-snapshot + event-log + observation-log replay.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-RPL-1 | A run MUST serialize to a `.skn` bundle (ZIP, [Playwright trace](https://playwright.dev/docs/trace-viewer) model): `manifest.json` + append-only `events.jsonl` + an immutable checkpoint DAG + content-addressed media (fragmented MP4, SHA content addressing). | D5 |
| FR-RPL-2 | `events.jsonl` MUST be the source of truth: line 1 = a Meta header `{v, session_id, run_id, t0_wall, t0_mono, tz}`; each row a **two-level discriminated envelope** `kind ∈ {action, observation, decision, permission, marker, snapshot_ref, meta}` with a per-kind `src`, a logical-clock `seq`, and an interval `dt`. | D5 |
| FR-RPL-3 | Each action event MUST pair to its observation via `action_id`, carrying before/after snapshot refs. | D5, D3 |
| FR-RPL-4 | The **decision channel** MUST emit [OpenTelemetry GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/) semantic-convention records (with OpenInference fields as compatibility aliases). | D5, D9 |
| FR-RPL-5 | Snapshots MUST be **bisected** (an ENV CoW-fork + a serialized AGENT checkpoint) and anchored to **semantic step boundaries** (permission gates, side-effecting tool calls, model decisions), each storing the exact `(run_id, step_id, event_log_offset)`. The agent snapshot MUST use a language-neutral schema, **not Python pickle**. | D5 |
| FR-RPL-6 | The fork tree MUST be an **immutable git-style parent-pointer DAG**; a branch = a new child checkpoint; the original timeline is NEVER mutated; re-convergence (multiple parents) is permitted. | D5 |
| FR-RPL-7 | **Branch** = CoW-fork the ENV snapshot + deserialize the AGENT checkpoint → re-run from step N (counterfactual eval/debug); seek-to-T and re-run-from-T MUST be O(nearest snapshot + tail), not O(whole history). | D1, D5 |
| FR-RPL-8 | Every LLM/tool result MUST be a recorded event with a per-event mode flag `{replay-stub \| live-reinference \| mock}`; pure-stub replay MUST reproduce the agent core bit-for-bit (enforced as a CI determinism test). | D5 |
| FR-RPL-9 | On a **live re-inference branch**, side-effecting tool calls MUST default to a record/mock proxy and require explicit opt-in to go live. | D5, D6 |
| FR-RPL-10 | Permission requests / grants / denials / overrides MUST be **first-class replay events** (the highest-priority marker class). | D5, D6 |

### 2.6 Permission panel (D6)

Permission is a **3-layer, capability-unlock** model that aligns with the generic `tool_runner` policy boundary (D2): a declarative decision layer, an object-capability handle layer, and OS enforcement.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-PRM-1 | The decision layer MUST be **[Cedar](https://docs.cedarpolicy.com/)** (formally verifiable via SMT/Lean, sub-ms), **NOT** OPA/Rego, evaluating `deny → ask → allow` first-match-wins, deny-wins-at-any-scope, with managed > project > session precedence. | D6 |
| FR-PRM-2 | A separate **[object-capability](https://en.wikipedia.org/wiki/Object-capability_model) caretaker/membrane handle layer** MUST provide O(1) instant, synchronous revoke (fail-closed at next use), independent of any policy-cache window. | D6 |
| FR-PRM-3 | OS enforcement MUST bind per guest: Linux = bubblewrap + seccomp (network-gate) + [Landlock](https://docs.kernel.org/userspace-api/landlock.html) + cgroups + an **out-of-VM egress proxy**; macOS = Seatbelt + TCC; Windows = restricted token + per-workspace capability-SID. | D6 |
| FR-PRM-4 | The egress proxy MUST be deny-by-default, scoped-domain (`host` / `*.host` / `**.host`, rejecting a bare global `*`), anti-domain-fronting, and fail-closed, with optional TLS-terminating MITM for high-risk sessions, and MUST harden DNS as a first-class egress channel (block raw port 53, force a controlled resolver). | D6, NFR-SEC |
| FR-PRM-5 | The capability grammar MUST be **8 typed, default-empty classes**: `net.egress`, `fs.scope`, `clipboard`, `gpu`, `install.privileged/sudo` (the "unlock"), `persistence`, `credentials`, `peripheral` — each carrying `{scope, risk_tier, lifecycle, enforcement_binding}`. | D6 |
| FR-PRM-6 | Actions MUST be classified into **4 risk tiers** — Auto / Notify / Ask / Block — and the classifier MUST be **taint-aware**: any action whose parameters derive from untrusted input (a web page, an untrusted file, a tool output) is promoted up a tier regardless of the tool's base risk. | D6 |
| FR-PRM-7 | On an `Ask`, the session MUST pause and stream a typed, blocking **approval card** showing the actor (agent id + version), the verb, the resource, a computed blast-radius/preview, and **Run / Escalate / Deny**; the default interaction model MUST be **escalation-on-failure** (start every session at least-authority). | D6 |
| FR-PRM-8 | Grants MUST be scoped, time-boxed, and lifecycle-revocable: `once` / `session` / persisted policy amendment; an agent MUST never be able to widen its own authority (the policy store is write-protected under managed precedence). | D6 |
| FR-PRM-9 | Secrets MUST be brokered at the proxy via header-injection from a secret broker ([HashiCorp Vault](https://www.hashicorp.com/products/vault), any cloud KMS, or [SPIFFE/SPIRE](https://spiffe.io/)) with JIT short-lived credentials; the model MUST never see plaintext, and credentials MUST never enter the agent context or the replay. | D6, NFR-SEC |
| FR-PRM-10 | The system MUST **fail closed on ambiguity**: an unmatched action → Ask; a human/reviewer timeout → the action does NOT run (record `timed_out`); a Critical action → auto-deny; and a denial-threshold circuit breaker (e.g. 3 consecutive denials) trips the session. | D6, NFR-SEC |
| FR-PRM-11 | A **Watch-Mode** presence requirement MUST be available for the Block-unless-watched edge of the Ask tier (production systems, bulk deletes, sensitive sites): require an attached, active human and pause if they detach. | D6 |

### 2.7 Eval (D7)

The eval layer is **thin orchestration on the runtime**, inverting OSWorld rather than forking it.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-EVL-1 | The eval layer MUST be a thin, stateless orchestration service ON TOP of the runtime + replay log (three lifecycle phases: init / run / verify), NOT a fork of OSWorld's `DesktopEnv`. | D7, D9 |
| FR-EVL-2 | Task success specs MUST be a **typed, schema-validated verifier DAG** (ordered/weighted check nodes: channel, query, assertion, optional milestone-step), NOT OSWorld's stringly-typed `getattr` evaluators. | D7 |
| FR-EVL-3 | Grading MUST be **programmatic-primary with a constrained model-verifier strictly as a fallback/tie-breaker** *(programmatic verifiers ~94% vs LLM-judge ~79% human agreement — vendor-published, unverified)*; a model must never judge its own thoughts. | D7 |
| FR-EVL-4 | Deterministic setup MUST be an **immutable golden snapshot per task** (seeded files/config/profile DBs, gold artifacts with checksums baked at task-build time); each replica forks it. | D7, D1 |
| FR-EVL-5 | Each task MUST run **N≥5 CoW-forked replicas** and report Average Score, pass@k, pass^k, confidence intervals, and ICC — never single-run pass@1 (variance hides 10–30 points). | D7, D1 |
| FR-EVL-6 | A mandatory **post-fork uniqueness/normalization hook** MUST run on every replica (shared with FR-SBX-4). | D7, D1 |
| FR-EVL-7 | All fixed sleeps MUST be replaced by **readiness probes** (poll guest app-ready / quiescence signals with a timeout; on timeout fail-fast, never silently proceed). | D7, D9 |
| FR-EVL-8 | Built-in conformance suites MUST ship with **task + grader + environment versioned together**: [OSWorld-Verified](https://xlang.ai/blog/osworld-verified) (369), [WindowsAgentArena](https://arxiv.org/abs/2409.08264) (~154), [AndroidWorld](https://arxiv.org/abs/2405.14573) (116, a roadmap guest), [WebArena](https://arxiv.org/abs/2401.13649) (812), [VisualWebArena](https://arxiv.org/abs/2401.13919) (910), WebVoyager. | D7 |
| FR-EVL-9 | Graders MUST be **tested, versioned artifacts** (heeding OSWorld-Verified's 300+ historical grader bugs) with a self-repair regression loop. | D7 |
| FR-EVL-10 | The agent scaffold/harness MUST be a first-class **versioned object** held constant across model comparisons (≈30 absolute points can come from scaffolding alone, per GAIA), so comparisons are fair. | D7 |
| FR-EVL-11 | Independent re-runs MUST be supported with a **"verified by Shinken"** label that surfaces the gap versus self-reported vendor scores. | D7 |
| FR-EVL-12 | Eval MUST support theme/font/language/resolution task **variations** and report score distributions, not single numbers (UI-variance can swing results ~10×). | D7 |
| FR-EVL-13 | All three grading paradigms MUST be natively composable: execution/state-based, trajectory LLM/VLM-judge, and rubric-based Agent-as-a-Judge ([Mind2Web-2](https://github.com/OSU-NLP-Group/Mind2Web-2)-style). | D7 |
| FR-EVL-14 | Verifiers MUST run **inside the guest via the ACI** over reliable channels (CDP / D-Bus / CLI), as one cross-platform introspection abstraction. | D7, D2 |

### 2.8 Control plane (D9)

Orchestration: a Fleet Manager + an Action Gateway + a scheduler + a replay store + the eval service + telemetry.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-CTL-1 | The **Sandbox Fleet Manager** MUST run per-(image, region, tier) **warm pools** + fork-on-demand + cold-pool replenish, adopting the open-source [`kubernetes-sigs/agent-sandbox`](https://github.com/kubernetes-sigs/agent-sandbox) CRD shape (Sandbox / SandboxTemplate / SandboxClaim / SandboxWarmPool). | D9, D1 |
| FR-CTL-2 | The **Action Gateway** MUST be the single request-path choke point doing, in order: tenant-auth → per-(tenant, workload, model) token-bucket / WFQ rate-limit → combined budget check → **Cedar policy** → dispatch. | D9, D6 |
| FR-CTL-3 | Warm starts MUST use snapshot-**restore**; parallel rollouts MUST use snapshot-**fork** from a per-task golden snapshot (this is what makes Best-of-N rollouts cheap). | D9, D1 |
| FR-CTL-4 | Sessions MUST implement the **dual-timer** model and auto-suspend-to-snapshot (shared with FR-SBX-9); the platform MUST bill on sandbox-seconds with idle as the primary cost lever. | D9, NFR-COST |
| FR-CTL-5 | Telemetry MUST be **OpenTelemetry GenAI semconv native** (`invoke_agent` / `chat` / `execute_tool` spans; `gen_ai.conversation.id = session`, `gen_ai.agent.id = agent`, plus `gui.*` and `shinken.tenant_id` attributes). | D9, D5 |
| FR-CTL-6 | Sandbox/guest health MUST be a **circuit-breakable dependency**: on a hung or crashed guest, kill-and-replace from the warm pool rather than waiting; each Sandbox is isolated so one failure does not cascade. | D9 |
| FR-CTL-7 | The control plane MUST expose per-tenant / per-workload presets for warm-pool depth, session timers, and budgets. | D9, NFR-MT |
| FR-CTL-8 | The substrate router (FR-SBX-1) MUST live in the control plane and dispatch by *(OS × needs-GPU × needs-fast-fork)*. | D1, D9 |

### 2.9 Interfaces / SDK / MCP (D8)

A native streaming SDK core plus an optional MCP facade.

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| FR-IFC-1 | One IDL/schema for the streaming session (control / action / observation / media planes) MUST generate **py and ts SDKs**; the native SDK is the single source of truth and the first thing built. | D8 |
| FR-IFC-2 | The native transport MUST be browser-reachable: raw WebSocket (or gRPC bidi + grpc-web) over the bidirectional streaming protocol; the Control Panel and first-party agents consume it directly. | D8, D4 |
| FR-IFC-3 | An optional **[MCP](https://modelcontextprotocol.io/) facade** MUST be exposed at **two altitudes**: (a) granular tools `create_session` / `act` / `observe` / `snapshot` / `grant_permission` / `list_sessions` + screenshot Resources; (b) an agent-task `run_task` → streamed steps. | D8 |
| FR-IFC-4 | The high-frequency action/observation loop and media MUST **never** route through MCP (MCP lacks a bidirectional/media transport and may force SSE→polling). | D8, D4 |
| FR-IFC-5 | The granular MCP facade's default observation MUST be a pruned a11y tree with stable element IDs; screenshots exposed only as on-demand Resources with a scale parameter. | D8, D3 |
| FR-IFC-6 | The MCP facade MUST implement **OAuth 2.1** Resource Server semantics ([RFC 9728](https://www.rfc-editor.org/rfc/rfc9728.html) Protected Resource Metadata, PKCE, Bearer-per-request, 403 + WWW-Authenticate, Origin validation); stdio for local, Streamable HTTP + SSE for remote. | D8, NFR-SEC |
| FR-IFC-7 | The granular MCP facade MUST keep the agent loop OUT of its tools so the Control Panel retains per-step reasoning and permission gating. | D8, D6 |
| FR-IFC-8 | The **Operator** contract MUST be the documented seam for human takeover and the provider-agnostic boundary; the agent loop is open and self-hostable (no lock-in). | D8, D12 |

---

## 3. Non-functional requirements

### 3.1 Ultra-high concurrency (NFR-SCALE) — D1, D9, D11

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| NFR-SCALE-1 | The platform MUST be designed for cloud ultra-high concurrency (≥100k concurrent Sandboxes as the planning anchor); dev/test starts local at small concurrency. | D9 |
| NFR-SCALE-2 | The Linux fork tier MUST sustain high fan-out via CoW *(~93% shared pages, ~1 ms/child — vendor-published, unverified)*; prefer many single-vCPU forks over a few fat VMs for parallel rollouts. | D1 |
| NFR-SCALE-3 | Sandbox allocation MUST target sub-second from a warm pool *(reference: ~300 sandboxes/s, 90% of allocations <200 ms — vendor-published, unverified)*. | D9 |
| NFR-SCALE-4 | The encode tier MUST be sized by **encoder throughput / pixel-rate**, not session-count myths (the 8-session cap is consumer-only; qualified datacenter GPUs are uncapped). | D11 |
| NFR-SCALE-5 | GPU MUST be **opt-in**: the vast majority of agent/browser tasks ride the CPU-only fork tier; only GPU-needing tasks route to tier G. | D11 |

### 3.2 Latency budgets (NFR-LAT) — D3, D4, D5, D6

| ID | Requirement | Target | Reconciles |
|----|-------------|--------|-----------|
| NFR-LAT-1 | Glass-to-glass video, same region | ~50–120 ms; cross-region <200 ms via PoPs *(unverified; RTT/2 is the hard floor)* | D4 |
| NFR-LAT-2 | Sandbox time-to-first-action (fork tier) | sub-second | D1, D9 |
| NFR-LAT-3 | Cedar permission decision | sub-ms *(Cedar reportedly 42–60× faster than OPA — vendor-published, unverified)* | D6 |
| NFR-LAT-4 | ocap revoke | O(1) synchronous, fail-closed at next use | D6 |
| NFR-LAT-5 | Replay scrub-seek / re-run-from-T | O(nearest snapshot + tail) | D5 |
| NFR-LAT-6 | Structured observation diff | event-driven (on change/focus), ~5–80 kbps on Tier 0 | D3 |

### 3.3 Cost (NFR-COST) — D3, D4, D9, D11

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| NFR-COST-1 | Structured-first observation MUST deliver the ~150× bandwidth win versus H.264 office video *(≈20 kbps vs ≈3 Mbps — vendor-published, unverified)*. | D3, D4 |
| NFR-COST-2 | The dominant streaming cost (egress/TURN, not codec) MUST be budgeted explicitly: at 100k concurrent 24×7, ≈$4.9M/mo (H.264 office) vs ≈$0.8M/mo (AV1 screen-content) vs tiny (structured) — *all vendor-derived, unverified, gating a first-party measurement plan*. See [09 Economics](09-economics-and-build-vs-buy.md). | D4 |
| NFR-COST-3 | Idle MUST be treated as the primary cost driver: auto-suspend-to-snapshot, host-memory overcommit/ballooning, billing on sandbox-seconds. | D9 |
| NFR-COST-4 | macOS and Windows MUST be priced as scarce/heavier premium tiers (macOS ≤2 VMs/host; Windows per-core licensing) and capacity-planned as standing pools. | D1, D10 |

### 3.4 Security & isolation (NFR-SEC) — D1, D6

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| NFR-SEC-1 | Isolation MUST be tiered: gVisor/Kata containers (the default Linux fast-path) up to a per-session microVM (its own guest kernel) for untrusted/sensitive workloads. | D1 |
| NFR-SEC-2 | Network MUST be **deny-by-default**, with the out-of-VM egress proxy the only path out, backed by OS netns/firewall so an agent that ignores proxy env vars still cannot egress. | D6 |
| NFR-SEC-3 | All credentials MUST be JIT short-lived and brokered at the proxy (Vault dynamic secrets / SPIFFE SVIDs); never in the agent context or the replay. | D6 |
| NFR-SEC-4 | Risk classification MUST be taint-aware, and the design MUST respect the **Agents Rule of Two** (at most two of {untrusted input, sensitive-data access, state-change/external communication}; if all three are needed, require a human in the loop). | D6 |
| NFR-SEC-5 | A consolidated **threat model** (prompt-injection → exfiltration, sandbox escape, multi-tenant noisy-neighbor) MUST be authored before scaling — see [08 Threat model](08-threat-model.md). *(Static prompt-injection defenses are reportedly 0–62% robust; adaptive attacks 80–100% successful — published research, unverified.)* | D6 |
| NFR-SEC-6 | Every capability denial (Landlock audit, egress-proxy decision event, Windows audit) MUST feed the replay/audit timeline as a structured event. | D5, D6 |
| NFR-SEC-7 | The trusted GPU substrate MUST use a GPU TEE + remote attestation + Confidential Containers for isolation-sensitive tenants; prefer MIG-backed vGPU / Kata GPU passthrough over raw time-slicing/MPS for untrusted GPU agents. | D11, D1 |

### 3.5 Availability (NFR-AVAIL) — D9

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| NFR-AVAIL-1 | Sandbox health MUST be circuit-breakable (kill-and-replace from the warm pool); a single Sandbox failure MUST NOT cascade. | D9 |
| NFR-AVAIL-2 | Long-running Sessions MUST snapshot before max-lifetime reap so tasks resume seamlessly. | D9 |
| NFR-AVAIL-3 | Recordings MUST be crash-safe (fragmented MP4 / CMAF, no moov-at-end) and immediately replayable. | D4, D5 |
| NFR-AVAIL-4 | WebRTC sessions MUST self-heal via ICE restart + re-offer fallback + PLI on reconnect. | D4 |
| NFR-AVAIL-5 | Cold-pool replenish MUST keep warm pools at target depth under load. | D9 |

### 3.6 Multi-tenancy (NFR-MT) — D6, D9, D11

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| NFR-MT-1 | Rate limiting and budgets MUST be per-(tenant, workload, model) at the Action Gateway. | D9 |
| NFR-MT-2 | GPU scheduling MUST use Equal Share or Fixed Share (per-agent SLA), **never** Best Effort for untrusted/noisy multi-tenant (one runaway agent must not monopolize the GPU). | D11 |
| NFR-MT-3 | Per-session microVMs MUST be destroyed + memory-sanitized at end (no cross-tenant contamination). | D9 |
| NFR-MT-4 | Policy/config MUST follow managed > project > session precedence so a tenant or agent cannot widen its own authority. | D6 |
| NFR-MT-5 | Telemetry MUST carry `shinken.tenant_id` for per-tenant attribution and cost allocation. | D9 |

### 3.7 Compliance / auditability (NFR-COMP) — D5, D6

| ID | Requirement | Reconciles |
|----|-------------|-----------|
| NFR-COMP-1 | All actions, observations, decisions, and permission events MUST be append-only and auditable (the `.skn` log IS the audit record). | D5, D6 |
| NFR-COMP-2 | Approvals / denials / overrides / attenuations / revocations MUST be recorded as first-class events with actor + version. | D5, D6 |
| NFR-COMP-3 | Sensitive values MUST be maskable before entering the stream/replay; secrets must never persist in plaintext. | D3, D6 |
| NFR-COMP-4 | The protocol / event-schema MUST be versioned, with an upcasting story specified (currently an open gap). | D2, D5 |
| NFR-COMP-5 | The decision channel MUST be OpenTelemetry GenAI semconv conformant for interoperable audit tooling. | D5, D9 |

---

## 4. In-scope / out-of-scope

### In scope (v1)

- **Guests:** Linux (first-class fork tier), Windows + macOS (heavier longer-lived tiers) — D1, D10.
- **One** control plane + **one** Guest Runtime contract + **one** ACI across all OSes (D10).
- Structured-first observation, dual-channel streaming, event-sourced branchable replay, the capability-unlock permission panel, the eval layer, and the native SDK + MCP facade (D2–D9).
- GPU as an **opt-in** acceleration wedge (encode tier + accelerated guest tier); NICE DCV as a build-vs-buy pixel channel (D11).
- An open, self-hostable core + a reusable Operator + an open, provider-agnostic agent loop; an optional hosted Control Panel / observability / permission-audit / eval as a commercial layer (D12).

### Out of scope (v1) / roadmap

- **Mobile (Android) guests = roadmap, not v1** (D1, D10): redroid / Cuttlefish / emulator quick-boot snapshots; AndroidWorld eval ships as a roadmap guest (FR-EVL-8). Mobile is the concrete post-v1 track.
- **Bit-deterministic full-desktop replay = explicitly out** (impractical; pragmatic snapshot + event-log + observation-log instead — D5).
- **Fast reset on macOS / Windows = out of v1** (largely infeasible today); those tiers are longer-lived and snapshot-light.
- **Firecracker for desktop/GPU = out** (no display/GPU device model; its GPU MVP was paused in Feb 2026) — desktop/GPU run on QEMU-microvm / crosvm / Cloud Hypervisor.

### Open decisions (carried forward, not papered over)

- **Multi-player / non-exclusive computer-use** (a separate human cursor + agent cursor sharing one Sandbox concurrently): an **OPEN** in/out-scope decision, to be resolved in [05 Tech decisions](05-tech-decisions.md); **not committed for v1**.
- **Mobile timing** within the roadmap (which Android substrate, when) — open; see [06 Roadmap](06-roadmap.md).
- **First-party perf/cost numbers** — every figure above is marked unverified; a measurement spike is a prerequisite to any density/cost commitment, including the **a11y-coverage spike** on Electron/Qt/canvas/games (the load-bearing assumption behind FR-OBS-6).

---

## 5. Success metrics / KPIs

These KPIs gate the phased rollout (see [06 Roadmap](06-roadmap.md)). Targets referencing external figures are anchors pending first-party measurement.

### Product / adoption

| KPI | Target | Tied to |
|-----|--------|---------|
| KPI-ADO-1 | A CUA eval/training team adopts Shinken as its primary runtime (eval + replay-as-training-data in production) | D12 |
| KPI-ADO-2 | First-party agents shipped on the SDK (P1) without touching the raw transport | D8 |
| KPI-ADO-3 | MCP-host integrations live at both altitudes (P5) | D8 |
| KPI-ADO-4 | `.skn` bundles consumed downstream as SFT/RL training data (P2) | D5, D7 |

### Performance (first-party measurement plan required)

| KPI | Target | Tied to |
|-----|--------|---------|
| KPI-PERF-1 | Fork-tier reset / time-to-first-action | sub-second p99 | D1, NFR-LAT-2 |
| KPI-PERF-2 | Glass-to-glass video latency, same region | ≤120 ms p95 | D4, NFR-LAT-1 |
| KPI-PERF-3 | Sandbox allocation from warm pool | ≥90% <500 ms | D9, NFR-SCALE-3 |
| KPI-PERF-4 | Cedar decision latency | sub-ms p99 | D6, NFR-LAT-3 |

### Efficiency / cost

| KPI | Target | Tied to |
|-----|--------|---------|
| KPI-COST-1 | Observation token reduction vs the screenshot loop | ~6× (a11y ~25k vs ~150k tokens/task) | D3 |
| KPI-COST-2 | Streaming bandwidth reduction vs H.264 office video | ~150× on Tier-0 structured | D3, D4 |
| KPI-COST-3 | Idle-Sandbox cost reduction via auto-suspend | a measured $/sandbox-hour decrease | D9, NFR-COST-3 |

### Eval quality

| KPI | Target | Tied to |
|-----|--------|---------|
| KPI-EVAL-1 | Verifier human-agreement | ≥94% programmatic-primary (the published OpenComputer bar) | D7 |
| KPI-EVAL-2 | Reliability reporting | every task reports pass@k / pass^k + CIs (N≥5), never bare pass@1 | D7 |
| KPI-EVAL-3 | Conformance suites | OSWorld-Verified bar matched; "verified by Shinken" re-runs published | D7 |
| KPI-EVAL-4 | Grader correctness | zero known grader bugs at suite cut (vs OSWorld-Verified's 300+ historical) | D7 |

### Safety / trust

| KPI | Target | Tied to |
|-----|--------|---------|
| KPI-SAFE-1 | Auto-grant classifier false-positive rate | ≤0.4% *(the published Claude Code auto-mode bar — vendor-published, unverified)* | D6 |
| KPI-SAFE-2 | Permission-event completeness | 100% of grants/denials recorded as replay events | D5, D6 |
| KPI-SAFE-3 | Credential leakage | zero plaintext secrets in the agent context or the replay | D6, NFR-SEC-3 |
| KPI-SAFE-4 | Threat model | a consolidated threat model authored before any GA scaling | NFR-SEC-5 |

---

### Requirement → decision coverage map

Every requirement above reconciles to at least one decision; the inverse mapping confirms all twelve decisions are covered.

| Decision | Covered by |
|----------|-----------|
| **D1** Isolation | FR-SBX-1…11, FR-CTL-3/8, FR-EVL-4/6, NFR-SCALE-2/5, NFR-SEC-1/7, KPI-PERF-1 |
| **D2** ACI schema | FR-ACI-1…10, FR-OBS-5, FR-IFC-1…8, FR-EVL-14, NFR-COMP-4 |
| **D3** Observation | FR-OBS-1…9, FR-STR-6, FR-IFC-5, KPI-COST-1 |
| **D4** Streaming | FR-STR-1…10, NFR-LAT-1, NFR-COST-1/2, NFR-AVAIL-3/4, KPI-PERF-2 |
| **D5** Replay | FR-RPL-1…10, FR-OBS-8, NFR-COMP-1…5, NFR-AVAIL-3 |
| **D6** Permission | FR-PRM-1…11, FR-ACI-5, NFR-SEC-2…6, NFR-MT-4, KPI-SAFE-* |
| **D7** Eval | FR-EVL-1…14, KPI-EVAL-* |
| **D8** Interfaces | FR-IFC-1…8, KPI-ADO-2/3 |
| **D9** Control plane | FR-CTL-1…8, FR-SBX-9, NFR-SCALE-1/3, NFR-AVAIL-1/2/5, NFR-MT-1/3/5, NFR-COST-3 |
| **D10** Cross-platform | FR-SBX-1/5/6/8, FR-ACI-7, scope §4 |
| **D11** GPU/NVIDIA | FR-STR-2/3/10, FR-OBS-2/3, FR-SBX-7, NFR-SCALE-4/5, NFR-MT-2, NFR-SEC-7 |
| **D12** Business | FR-IFC-8, scope §4, KPI-ADO-1/4 |

External sources cited above are consolidated in [`../notes/sources.md`](../notes/sources.md).
