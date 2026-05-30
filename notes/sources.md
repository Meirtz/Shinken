# Sources — annotated bibliography

> Working note. The external references that ground Shinken's design decisions (D1–D12) and
> the four headline features (replay, the capability-unlock permission panel, bandwidth
> optimization, real-time streaming). Entries are **deduplicated** and grouped by topic;
> each has a one-line annotation explaining *why it matters to Shinken*. Sibling design docs
> are linked by relative path (e.g. [05-tech-decisions.md](../docs/05-tech-decisions.md)).
>
> **Conventions.** All performance / density / cost figures cited from these sources are
> **vendor-published and unverified** unless they are first-party measurements; see
> [open-questions.md](open-questions.md) for the measurement plan. URLs are external public
> pages only. Cloned prior-art repositories are listed separately under
> *Cloned reference repos*. Today's date for currency claims is 2026-05-30.

---

## 1. Agent frameworks, computer-use models & benchmarks

The competitive landscape Shinken slots into (see [../docs/04-landscape.md](../docs/04-landscape.md)
and [eval-benchmarks.md](eval-benchmarks.md)) and the canonical action/observation interfaces it
must natively speak (D2).

**Models & operator products**
- OpenAI — Computer-Using Agent announcement: https://openai.com/index/computer-using-agent and Introducing Operator: https://openai.com/index/introducing-operator — the vision-in / pixel-coordinate-out CUA loop + consumer remote-browser agent (human-takeover/"watch mode") Shinken's Operator seam generalizes.
- OpenAI — Operator System Card: https://openai.com/index/operator-system-card — safety-check / acknowledgement model that informs the permission gate (D6).
- OpenAI — Computer use (CUA) API guide: https://developers.openai.com/api/docs/guides/tools-computer-use — `computer_call` / `computer_call_output` wire format the adapter must emit (D2).
- OpenAI — API deprecations (computer-use-preview shutdown 2026-07-23, Operator sunset): https://developers.openai.com/api/docs/deprecations — why Shinken pins adapters to dated schema versions rather than a single live model. Model card: https://platform.openai.com/docs/models/computer-use-preview.
- Anthropic — Computer use tool (API docs): https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/computer-use-tool — the `computer_20241022/20250124/20251124` hosted-tool grammar Shinken must MATCH to be Claude-compatible (D2).
- Anthropic — Bash tool: https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/bash-tool and Text editor (`str_replace_based_edit_tool`): https://platform.claude.com/docs/en/docs/agents-and-tools/tool-use/text-editor-tool — the off-by-default code-as-action class + third hosted tool the ACI adapter normalizes (D2).
- Anthropic — Vision / image token math (tokens ≈ w·h/750; 1568/2576 px caps): https://platform.claude.com/docs/en/build-with-claude/vision — drives "send the pixels the model reasons over" downscaling (D3/D4).
- Anthropic — Claude Opus 4.8 announcement (strongest computer-use/browser model, ~84% on OSWorld-class evals — vendor, unverified): https://www.anthropic.com/news/claude-opus-4-8 — current SOTA anchor for the eval bar (D7).
- Anthropic — Writing effective tools for AI agents: https://www.anthropic.com/engineering/writing-tools-for-agents and Effective context engineering: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents — token-efficiency/namespacing for the MCP facade (D8) + structured-first, prune-stale-frames observation (D3).

**Agent architectures & grounding research**
- SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering: https://arxiv.org/abs/2405.15793 — origin of the "ACI" framing Shinken adopts as a first-class typed protocol.
- UI-TARS / UI-TARS-2 (native GUI agents; multi-turn RL): https://arxiv.org/abs/2501.12326 and https://arxiv.org/html/2509.02544v1 — typed action schema (normalized + pixel coordinates) the adapter speaks (D2) + replay-as-RL-data thesis (D5).
- OmniParser for Pure Vision Based GUI Agent: https://arxiv.org/abs/2408.00203 — Set-of-Marks element list = Shinken's Rung-1 observation (D3).
- Agent S2 / Agent S3 (compositional generalist-specialist; scaling agents): https://arxiv.org/abs/2504.00906 and https://arxiv.org/html/2510.02250v1 — Mixture-of-Grounding + proactive re-planning + test-time scaling (D3/D7).
- UFO2: The Desktop AgentOS (UIA + vision fusion, IoU dedup): https://arxiv.org/abs/2504.14603 — hybrid a11y+pixel observation precedent for Windows (D3/D10).
- OS-ATLAS / UGround (foundation action + universal visual grounding): https://arxiv.org/pdf/2410.23218 and https://arxiv.org/html/2410.05243v1 — pixel-grounding models that motivate the pluggable grounding service (D3).
- OpenCUA: Open Foundations for Computer-Use Agents: https://arxiv.org/abs/2508.09123 — open recording→state-action-CoT pipeline; the recording-schema reference for replay-as-training-data.
- A11y-CUA Dataset — characterizing the accessibility gap in computer use: https://arxiv.org/html/2602.09310 — direct evidence on the load-bearing a11y-coverage assumption (see [open-questions.md](open-questions.md)).
- Just Do It!? Computer-Use Agents Exhibit Blind Goal-Directedness: https://arxiv.org/pdf/2510.01670 — failure-mode taxonomy motivating the HITL approval card (D6).

**Benchmarks (the conformance suite Shinken hosts — D7)**
- OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks: https://arxiv.org/abs/2404.07972 — the prior-art runtime Shinken supersedes; project site: https://os-world.github.io
- OSWorld-Human — efficiency of computer-use agents: https://arxiv.org/html/2506.16042v1 — step-efficiency metric; argues for low-latency observation.
- WindowsAgentArena — multi-modal OS agents at scale: https://arxiv.org/abs/2409.08264 — Windows conformance target (D7/D10).
- AndroidWorld / AndroidEnv (dynamic Android benchmarking; RL platform): https://arxiv.org/abs/2405.14573 and https://arxiv.org/pdf/2105.13231 — Android roadmap eval pattern (deterministic state checks); low-level action space rejected as too low-level for LLM agents.
- WebArena / VisualWebArena / WebVoyager: https://arxiv.org/html/2307.13854v4, https://arxiv.org/abs/2401.13649, https://arxiv.org/abs/2401.13919 — web-agent conformance (text + multimodal + live web).
- Mind2Web 2 (Agent-as-a-Judge): https://arxiv.org/abs/2506.21506 — agentic-search eval; informs the constrained model-verifier fallback (D7).
- GAIA / TheAgentCompany / Terminal-Bench: https://arxiv.org/pdf/2311.12983, https://arxiv.org/html/2412.14161v2, https://arxiv.org/html/2601.11868v1 — general-assistant, long-horizon enterprise, and hard-CLI/code-as-action evals.
- ScreenSpot-Pro — high-resolution GUI grounding: https://arxiv.org/pdf/2504.07981 — grounding stress test at pro resolutions.
- On Randomness in Agentic Evals (pass@k vs pass^k): https://arxiv.org/pdf/2602.07150 and Stochasticity in Agentic Evaluations: https://arxiv.org/pdf/2512.06710 — statistical basis for N≥5 forked replicas + CIs (D7).
- OpenComputer — verifiable software worlds for CUAs: https://arxiv.org/html/2605.19769v1 — verifiable-environment construction for the eval layer.

---

## 2. Sandbox, microVM, container isolation & fast fork

Substrate options (D1) and the fast-fork primitive that makes both instant reset and
replay-branching the same operation (D5). See [sandbox-infra.md](sandbox-infra.md).

**microVM / VMM substrate**
- Firecracker — site: https://firecracker-microvm.github.io, GitHub: https://github.com/firecracker-microvm/firecracker, design doc (minimal device model, no GPU/display — drives the QEMU-microvm/crosvm desktop split): https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md — the default Linux headless fast-fork tier (D1).
- Firecracker snapshotting — snapshot support: https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md, page faults on resume (UFFD lazy paging, sub-30 ms — vendor, unverified): https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/handling-page-faults-on-snapshot-resume.md, random-for-clones (VMGenID/VMClock/PRNG reseed): https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/random-for-clones.md — the snapshot/restore core + post-fork uniqueness hook (reseed RNG/MAC/hostname) Shinken requires (D1).
- Firecracker — rootfs CoW (#3061): https://github.com/firecracker-microvm/firecracker/discussions/3061 and GPU/PCIe tracking (#4845): https://github.com/firecracker-microvm/firecracker/discussions/4845 — disk-side CoW for N parallel clones; confirms GPU is out-of-scope (D11).
- Restoring Uniqueness in MicroVM Snapshots (arXiv 2102.12892): https://arxiv.org/abs/2102.12892 — the foundational uniqueness-on-fork paper.
- Announcing Firecracker (AWS Open Source Blog): https://aws.amazon.com/blogs/opensource/firecracker-open-source-secure-fast-microvm-serverless — origin & threat-model framing; Seven Years of Firecracker (Marc Brooker, 2025): https://brooker.co.za/blog/2025/09/18/firecracker.html — snapshot/jailer maturity retrospective.
- Cloud Hypervisor — GitHub: https://github.com/cloud-hypervisor/cloud-hypervisor — richer device model VMM for Windows-desktop + GPU-VFIO tiers (D1/D11).
- crosvm — virtio-gpu (virgl/gfxstream, headless): https://crosvm.dev/book/devices/gpu.html — virtio-gpu path for Linux desktop guests where Firecracker can't.
- Taming Serverless Cold Starts Through OS Co-Design (sub-5 ms restore): https://arxiv.org/pdf/2509.14292 — research frontier for restore latency (academic, unverified).
- Northflank — substrate comparisons: https://northflank.com/blog/firecracker-vs-cloud-hypervisor, https://northflank.com/blog/firecracker-vs-qemu, https://northflank.com/blog/kata-containers-vs-firecracker-vs-gvisor — device-model + startup/isolation trade-off matrices.

**Container / K8s sandbox pattern**
- kubernetes-sigs Agent Sandbox — Kata example: https://agent-sandbox.sigs.k8s.io/docs/use-cases/examples/kata-containers — the OSS CRD shape Fleet Manager mirrors (D9).
- gVisor — what is gVisor: https://gvisor.dev/docs and checkpoint/restore: https://gvisor.dev/docs/user_guide/checkpoint_restore — software-isolation fast path + demand paging.
- gVisor — GPU support (nvproxy): https://gvisor.dev/docs/user_guide/gpu — notes the host-driver-ioctl risk (D11 caveat).
- gVisor — optimizing seccomp usage: https://gvisor.dev/blog/2024/02/01/seccomp — seccomp surface reduction.

**Fast snapshot / fork / branch platforms (build-vs-buy)**
- Morph — Infinibranch (snapshot/branch/restore <250 ms, ~150 ms start-from-snapshot — vendor, unverified): https://cloud.morph.so/docs/blog/developers, branch API: https://cloud.morph.so/docs/documentation/instances/branch, blog (non-linear computing): https://www.morph.so/blog/infinibranch — the "branch a live VM" primitive Shinken's Replay mirrors (D5).
- E2B — computer-use docs: https://e2b.dev/docs/use-cases/computer-use and Firecracker integration (DeepWiki): https://deepwiki.com/e2b-dev/infra/3.2-firecracker-integration — Firecracker sandbox-as-a-service; the orchestrator + per-host VMM shape to mirror (D9). Scaling with OverlayFS: https://e2b.dev/blog/scaling-firecracker-using-overlayfs-to-save-disk-space — disk-CoW density.
- E2B — sandbox persistence (pause/resume memory + FS): https://e2b.dev/docs/sandbox/persistence — auto-suspend-to-snapshot lifecycle (D9); internet access (allow/deny): https://e2b.dev/docs/sandbox/internet-access — egress-allowlist model (D6).
- Modal — memory snapshots (sub-second checkpoint/restore): https://modal.com/blog/mem-snapshots and docs: https://modal.com/docs/guide/memory-snapshots — lazy/background restore pattern (D1).
- Modal — GPU memory snapshots: https://modal.com/blog/gpu-mem-snapshots and sandbox snapshots: https://modal.com/docs/guide/sandbox-snapshots — GPU-checkpoint frontier (alpha; D11 caveat) + restore-as-new-sandbox semantics.
- Daytona — architecture: https://www.daytona.io/docs/en/architecture and sandboxes (sub-90 ms create, CoW fork tree — vendor, unverified): https://www.daytona.io/docs/en/sandboxes — control-plane + fork-tree reference.
- forkd (CoW VM fork, ~1 ms/child, branch regression to 2.7 s by 6th branch): https://github.com/deeplethe/forkd — the snapshot-chain compaction lesson (D5).
- CRIUgpu — transparent checkpointing of GPU workloads: https://arxiv.org/html/2502.16631v1 and CRIU project: https://criu.org — userspace checkpoint/restore basis for GPU- and container-tier replay.

**Cross-platform guest constraints (Windows / macOS — D1/D10)**
- Tart — Apple Virtualization.framework VMs (OCI registry, Orchard cluster): https://tart.run — the macOS substrate model and 2-VM/host reality.
- AWS — BYOL on dedicated hosts (Windows/SQL licensing): https://docs.aws.amazon.com/prescriptive-guidance/latest/optimize-costs-microsoft-workloads/byol-ded-hosts.html — Windows-in-cloud licensing gate (D1).
- Amazon Bedrock AgentCore — lifecycle settings (idle 900 s / max 28800 s): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html and isolated per-session microVMs: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html — dual-timer session model + fresh-instance-per-run precedent (D7/D9).

---

## 3. Streaming, WebRTC & NVENC hardware video encode

The dual-channel transport (D4) and the optional NVENC-accelerated media tier (D11). See
[streaming-bandwidth.md](streaming-bandwidth.md).

**WebRTC architecture & protocols**
- RFC 8831 — WebRTC Data Channels (SCTP/DTLS, reliability/ordering): https://datatracker.ietf.org/doc/html/rfc8831 — the reliable-ordered data channel that *is* the replay log (D4/D5).
- RFC 9725 — WHIP (WebRTC-HTTP Ingestion): https://datatracker.ietf.org/doc/rfc9725, WHEP draft: https://datatracker.ietf.org/doc/html/draft-ietf-wish-whep-01, and orientation: https://webrtc.ventures/2024/08/understanding-whip-whep-and-media-over-quic — signaling for the media track.
- Cloudflare — WHIP/WHEP at scale (sub-second, unlimited viewers): https://blog.cloudflare.com/webrtc-whip-whep-cloudflare-stream — fan-out scaling precedent.
- BlogGeek.me glossary — SFU (forward without re-encode): https://bloggeek.me/webrtcglossary/sfu, SVC: https://bloggeek.me/webrtcglossary/svc, TWCC: https://bloggeek.me/webrtcglossary/transport-cc, jitter buffer: https://bloggeek.me/webrtcglossary/jitter-buffer — the encode-once + ABR + latency-minimization levers (D4).
- W3C — SVC for WebRTC: https://www.w3.org/TR/webrtc-svc and UI Events KeyboardEvent key/code: https://www.w3.org/TR/uievents-key — scalability spec + cross-platform key normalization for the input channel.
- LiveKit — SFU internals: https://docs.livekit.io/reference/internals/livekit-sfu, distributed mesh: https://livekit.com/blog/scaling-webrtc-with-distributed-mesh, and Track Egress (export as-is, no transcode): https://docs.livekit.io/transport/media/ingress-egress/egress/track — production SFU scaling + record-while-stream (D5 media capture).
- WebCodecs (Chrome): https://developer.chrome.com/docs/web-platform/best-practices/webcodecs — browser-side encode/decode for the Control Panel viewer.

**NVENC & GPU encode (optional GPU tier — D11)**
- NVIDIA — NVENC Application Note (datacenter GPUs uncapped; GeForce session cap): https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html — confirms datacenter GPUs have no per-session limit (public NVIDIA product fact).
- NVIDIA forums — increasing NVENC concurrent sessions: https://forums.developer.nvidia.com/t/how-to-increase-number-of-nvenc-concurrent-sessions/169367 — the consumer 8-session cap detail.
- NVIDIA — AV1 + Ada Lovelace video quality/perf (~40% bitrate save vs H.264 — vendor, unverified): https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture — basis for AV1-SCC egress savings.
- NVIDIA — 8K60 split-frame encoding on Ada: https://developer.nvidia.com/blog/video-encoding-at-8k60-with-split-frame-encoding-and-nvidia-ada-lovelace-architecture — high-res encode headroom (L40S premium tier).
- NVIDIA — Capture SDK / NvFBC (Linux, GPU-buffer capture): https://developer.nvidia.com/capture-sdk — zero-copy GPU-resident capture path.
- NVIDIA — GDN cloud gaming/rendering: https://developer.nvidia.com/blog/revolutionizing-cloud-gaming-and-graphics-rendering-with-nvidia-gdn — large-scale streamed-desktop precedent.
- Evaluation of GPU Video Encoder for Low-Latency Real-Time 4K UHD: https://arxiv.org/abs/2511.18688 — independent NVENC latency measurement.
- Overview of Screen Content Coding (SCC): https://arxiv.org/pdf/2011.14068 — why screen-content tuning beats camera-video presets for desktops (D4).

**OSS streaming stacks & GStreamer (Linux streaming reference)**
- Selkies (GStreamer WebRTC, NVENC/VA-API): https://github.com/selkies-project/selkies-gstreamer and design: https://selkies-project.github.io/selkies/design — the Linux streaming-pipeline starting point (ximagesrc → NVENC → webrtcbin → browser).
- GStreamer nvcodec plugins (NVENC element family): https://gstreamer.freedesktop.org/documentation/nvcodec/index.html and mp4mux (fragmented MP4): https://gstreamer.freedesktop.org/documentation/isomp4/mp4mux.html — encode-tier elements + fMP4 muxing for content-addressed replay media (D5); record-while-stream recipe: https://github.com/crearo/gstreamer-cookbook/blob/master/README.md.
- LizardByte Sunshine: https://github.com/LizardByte/Sunshine and Moonlight docs: https://github.com/moonlight-stream/moonlight-docs — capture-backend abstraction + zero-copy GPU pipeline (native-client only, not browser-deliverable).
- pion/webrtc: https://github.com/pion/webrtc and pion/interceptor: https://github.com/pion/interceptor — the Go WebRTC stack neko uses; GCC/TWCC building blocks. ion-sfu: https://github.com/ionorg/ion-sfu — reference open-source SFU.

**Build-vs-buy pixel channel (NICE / Amazon DCV)**
- AWS — What is Amazon DCV (NICE DCV): https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html and streaming modes (adaptive H.264 vs pixel-perfect): https://docs.aws.amazon.com/dcv/latest/userguide/using-streaming.html — the commercial NVENC+QUIC remote-display option (D4/D11).
- NI-SP — NICE/Amazon DCV (QUIC, AES-256) and performance guide (NVENC codec selection): https://www.ni-sp.com/products/nice-dcv and https://www.ni-sp.com/knowledge-base/dcv-general/performance-guide — DCV tuning details.

---

## 4. Replay, recording & on-disk trajectory formats

The event-sourced `.skn` bundle and branchable checkpoint DAG (D5). See [replay.md](replay.md).

- Playwright — Tracing API (screenshots/snapshots/trace.zip): https://playwright.dev/docs/api/class-tracing, Trace Viewer: https://playwright.dev/docs/trace-viewer, and the `TraceEvent` union: https://github.com/microsoft/playwright/blob/main/packages/trace/src/trace.ts — the ZIP-of-events model and two-level event envelope `.skn` is patterned on (D5).
- Playwright Trace Viewer internals (DeepWiki): https://deepwiki.com/microsoft/playwright/5.3-trace-viewer and visual comparisons (slider/diff): https://playwright.dev/docs/test-snapshots — scrub-UI + diff-view UX for the Control Panel.
- rrweb — record and replay the web: https://github.com/rrweb-io/rrweb, event taxonomy: https://github.com/rrweb-io/rrweb/blob/master/docs/recipes/dive-into-event.md, and rrweb-player controller API: https://github.com/rrweb-io/rrweb/blob/master/packages/rrweb-player/README.md — the DOM-snapshot+incremental + scrubbable-player model (which streaming competitors abandoned; informs why Shinken uses video+event-log, not DOM replay).
- asciinema — asciicast v2/v3 (markers): https://docs.asciinema.org/manual/asciicast/v2 and https://docs.asciinema.org/manual/asciicast/v3 — minimal append-only event format + marker-channel precedent.
- Chrome DevTools — Recorder (JSON export, @puppeteer/replay): https://developer.chrome.com/docs/devtools/recorder/overview — record→re-grounded-replay precedent.
- Counterfactual Trace Auditing of LLM Agent Skills: https://arxiv.org/html/2605.11946v1, Tree-GRPO: https://arxiv.org/abs/2509.21240, ProRL (rollout-as-a-service): https://arxiv.org/html/2603.18815v1 — branching/counterfactual replay + fork-as-RL-rollout, the replay-as-training-data wedge (D5/D12).
- OpenTelemetry — GenAI semantic conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai, agent spans: https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans, observability (2026): https://opentelemetry.io/blog/2026/genai-observability — the decision-channel schema for replay's `decision` events (D5) and telemetry stack (D9).

---

## 5. Permissions, capability models & security

The three-layer capability-unlock permission system: Cedar + ocap + OS enforcement, plus
egress/secret brokering (D6). See [permissions.md](permissions.md) and
[../docs/08-threat-model.md](../docs/08-threat-model.md).

**Declarative policy engine (Cedar — D6 layer 1)**
- Cedar — policy structure: https://docs.cedarpolicy.com/policies/syntax-policy.html, operators: https://docs.cedarpolicy.com/policies/syntax-operators.html, templates: https://docs.cedarpolicy.com/policies/templates.html — the declarative decision layer Shinken chooses over OPA/Rego.
- Cedar papers — A New Language (formal verifiability): https://arxiv.org/pdf/2403.04651 and How We Built Cedar (verification-guided): https://arxiv.org/pdf/2407.01688 — backing for "sub-ms, formally verifiable."
- AWS Verified Permissions — policy templates: https://docs.aws.amazon.com/verifiedpermissions/latest/userguide/policy-templates.html — managed-Cedar precedent for per-tenant policy.
- Oso — OPA vs Cedar vs Zanzibar: https://www.osohq.com/learn/opa-vs-cedar-vs-zanzibar and Natoma — MCP access control OPA vs Cedar: https://natoma.ai/blog/mcp-access-control-opa-vs-cedar-the-definitive-guide — engine trade-off + Cedar-for-agent-tooling argument (D6/D8).
- HashiCorp Sentinel — enforcement levels: https://developer.hashicorp.com/sentinel/docs/concepts/enforcement-levels — the advisory/soft/hard tiering analog to Auto/Notify/Ask/Block.

**OS-level sandboxing (D6 layer 3)**
- Landlock — unprivileged sandboxing: https://landlock.io and news #5 (ABI 6 IPC scoping, ABI 7 audit): https://landlock.io/news/5 — Linux FS-scope enforcement.
- OpenAI Codex — sandbox concepts (Seatbelt/Landlock, modes): https://developers.openai.com/codex/concepts/sandboxing, approvals & security: https://developers.openai.com/codex/agent-approvals-security, internet access (GET/HEAD/OPTIONS allowlist): https://developers.openai.com/codex/cloud/internet-access — the three-axis (admissibility × confinement × approval) model + escalation-prompt UX + exfil mitigation Shinken mirrors.
- OpenAI — building a safe sandbox for Codex on Windows: https://openai.com/index/building-codex-windows-sandbox — Windows restricted-token/cap-SID enforcement (D6 Windows path).
- Claude Code — sandboxing: https://code.claude.com/docs/en/sandboxing, security: https://code.claude.com/docs/en/security, permissions (allow/ask/deny, deny-wins, source-attributed): https://code.claude.com/docs/en/permissions — the out-of-sandbox egress proxy + OS backstop, and the deny>ask>allow grammar the Permission Panel adopts.
- Anthropic — Claude Code sandboxing: https://www.anthropic.com/engineering/claude-code-sandboxing and auto mode (safer skip-permissions): https://www.anthropic.com/engineering/claude-code-auto-mode — two-layer "what it CAN touch" vs "when to ASK" split (D6).

**Prompt injection, egress & secret brokering**
- Anthropic — mitigating prompt injection in browser use (~1% attack success — vendor, unverified): https://www.anthropic.com/research/prompt-injection-defenses and OpenAI — understanding prompt injections: https://openai.com/index/prompt-injections — quantifies the taint-aware risk the permission tiers address.
- Defeating Prompt Injections by Design (CaMeL): https://arxiv.org/pdf/2503.18813 — capability/taint-tracking design motivating taint-aware approvals (D6).
- HashiCorp — SPIFFE for agentic AI identity: https://www.hashicorp.com/en/blog/spiffe-securing-the-identity-of-agentic-ai-and-non-human-actors and Vault SPIFFE auth: https://www.hashicorp.com/en/blog/vault-enterprise-1-21-spiffe-auth-fips-140-3-level-1-compliance-granular-secret-recovery — the secret-broker (Vault/SPIFFE) so the model never sees plaintext (D6).

---

## 6. ACI, observation model & MCP

The structured-first observation rungs (D3), the typed action schema and version-pinned
adapters (D2), and the native-SDK-core-plus-MCP-facade interface posture (D8). See
[ai-native-interface.md](ai-native-interface.md).

**Accessibility tree / structured observation (D3 Rung 0)**
- Chrome DevTools — full accessibility tree: https://developer.chrome.com/blog/full-accessibility-tree and a11y reference: https://developer.chrome.com/docs/devtools/accessibility/reference — a11y tree as structured UI state (the CDP source).
- GNOME at-spi2-core: https://github.com/GNOME/at-spi2-core — AT-SPI, the Linux a11y backend for the cross-OS `Element` schema.
- Playwright — ARIA/accessibility snapshots: https://playwright.dev/docs/aria-snapshots and Playwright MCP — snapshots (`[ref=e2]` element refs): https://playwright.dev/mcp/snapshots, vision mode: https://playwright.dev/mcp/vision-mode, repo: https://github.com/microsoft/playwright-mcp — the act-on-element-ref-by-default, pixels-on-demand escalation Shinken adopts (D3).
- WebDriver BiDi (W3C spec): https://www.w3.org/TR/webdriver-bidi and browser-use — leaving Playwright for CDP: https://browser-use.com/posts/playwright-to-cdp — bidirectional automation direction + kill-9/reconnect resilience for the browser observation path.
- A11y-Compressor — visual context reconstruction + redundancy reduction: https://arxiv.org/html/2605.00551v1 — token-savings evidence for structured-first observation (~6× claim, unverified).

**MCP facade (D8)**
- MCP — transports (stdio, Streamable HTTP/SSE): https://modelcontextprotocol.io/specification/2025-11-25/basic/transports, tools: https://modelcontextprotocol.io/specification/2025-11-25/server/tools, progress utility (no progress in responses): https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress — confirms why the high-frequency action/observation/video loop must NOT run over MCP (D8) + facade tool surface.
- MCP — elicitation (form/structured prompts): https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation — host-side approval prompts mapping to the permission card; authorization (OAuth 2.1 Resource Server, RFC 9728): https://modelcontextprotocol.io/specification/draft/basic/authorization — the OAuth 2.1 facade requirement (D8).
- modelcontextprotocol — python-sdk: https://github.com/modelcontextprotocol/python-sdk and typescript-sdk: https://github.com/modelcontextprotocol/typescript-sdk — official SDKs for the generated-facade layer; MCP Apps (interactive UIs): https://blog.modelcontextprotocol.io/posts/2025-11-21-mcp-apps — protocol-direction context.

---

## 7. GPU virtualization & confidential compute (optional GPU tier)

Public NVIDIA product facts framed as options for the opt-in GPU tier (D11). See
[sandbox-infra.md](sandbox-infra.md) and [../docs/09-economics-and-build-vs-buy.md](../docs/09-economics-and-build-vs-buy.md).

- NVIDIA — MIG User Guide: https://docs.nvidia.com/datacenter/tesla/mig-user-guide/latest, supported profiles (media engines per slice): https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html, MIG on DGX A100 (up to 7 instances): https://docs.nvidia.com/dgx/dgxa100-user-guide/using-mig.html — MIG slicing = the per-session GPU quota the panel hands out (D11).
- NVIDIA — time-slicing GPUs in K8s (no memory/fault isolation): https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html, vGPU features: https://docs.nvidia.com/vgpu/knowledge-base/latest/vgpu-features.html, scheduling policies: https://docs.nvidia.com/ai-enterprise/release-8/latest/infra-software/vgpu/features/scheduling.html — the time-sliced vGPU density pool + density-vs-fairness knob (D11).
- NVIDIA — RTX vWS GPU sizing: https://docs.nvidia.com/vgpu/sizing/virtual-workstation/latest/right-gpu.html — GPU selection (Ada L4 density / L40S premium — D11).
- NVIDIA — GPU Operator with Kata: https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/25.3.1/gpu-operator-kata.html and Confidential Containers (SEV-SNP/TDX): https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/24.9.2/gpu-operator-confidential-containers.html — the GPU-TEE + Confidential Containers trusted-GPU variant (D11).
- NVIDIA — checkpointing CUDA apps with CRIU: https://developer.nvidia.com/blog/checkpointing-cuda-applications-with-criu and cuda-checkpoint repo: https://github.com/NVIDIA/cuda-checkpoint — GPU-state checkpoint feasibility (alpha; D11 caveat). k8s-device-plugin: https://github.com/nvidia/k8s-device-plugin — GPU scheduling for K8s.

---

## Cloned reference repos (`references/`)

These projects are **cloned/vendored under `references/`** (git-ignored; only
`references/README.md` is tracked) so we can study implementation details first-hand. Each is
prior art we MATCH, BEAT, or DIFFERENTIATE against (see [../docs/04-landscape.md](../docs/04-landscape.md)).
Re-clone steps live in [../references/README.md](../references/README.md).

| Repo (path) | Upstream | Why we keep it |
|-------------|----------|----------------|
| `OSWorld/` | https://github.com/xlang-ai/OSWorld | Primary prior-art runtime+benchmark Shinken supersedes: in-VM action server, gym-style client env, multi-cloud providers, getter/metric evaluators. We document where it is too primitive — single-platform, screenshot polling, no streaming/replay/permissions ([../docs/03-osworld-analysis.md](../docs/03-osworld-analysis.md), [osworld-teardown.md](osworld-teardown.md)). |
| `cua/` | https://github.com/trycua/cua | Closest cross-platform competitor. Source of the Image→Runtime→Transport→Interfaces→Sandbox layering, three-mode lifecycle, per-OS handler-factory, dual-altitude MCP, and trajectory recorder we adopt — and the pull-a-PNG-per-step + coarse-auth gaps we BEAT. |
| `codex/` | https://github.com/openai/codex | OpenAI Codex Rust CLI (`codex-rs`) sandboxing: the three-axis permission model (admissibility × confinement × approval), strictest-wins merge, ordered (path, Read/Write/Deny) FS rules, fail-closed egress — the most reusable permission design (D6). |
| `anthropic-quickstarts/` | https://github.com/anthropics/anthropic-quickstarts | `computer-use-demo` + best-practices: the canonical hosted tool schema (computer/bash/str_replace), sampling loop, image-resize/coordinate math, and prompt-caching discipline Shinken must MATCH — plus the stock Xvfb+x11vnc+noVNC stack it aims to BEAT (D2/D4). |
| `neko/` | https://github.com/m1k1o/neko | Self-hosted virtual desktop over WebRTC (GStreamer → VP8/VP9/AV1/H264 track + binary DataChannel control). Shared-encoder fan-out, lazy pipelines, runtime source-swap, keyframe-lobby, and GCC/TWCC+hysteresis ABR are the media-plane streaming reference (D4). |
| `OpenAdapt/` | https://github.com/OpenAdaptAI/OpenAdapt | Desktop record-and-replay RPA: timestamp-correlated multi-stream capture (input/frame/window-a11y/DOM), H.264 video sidecar, nestable action schema, Strategy/`get_next_action_event` replay abstraction — validates the replay thesis (D5). Its host-coupled, no-sandbox execution is the anti-pattern Shinken avoids. |
| `e2b-desktop/` | https://github.com/e2b-dev/desktop | ~600-LOC desktop domain layer over the generic E2B Firecracker sandbox. The "thin domain SDK over an isolated runtime" pattern + clean lifecycle API (create/connect/auto-pause/kill) — and the raw-VNC-push + one-process-per-action gaps Shinken BEATs (D1/D4). |
| `UI-TARS-desktop/` | https://github.com/bytedance/UI-TARS-desktop | Electron app + `@ui-tars`/`@tarko` SDK: closest public analog to Shinken's Operator layer — operator pluggability (local nutjs/CDP, remote VM/browser behind one interface), a typed `BaseAction` union carrying normalized+pixel coordinates, typed-event stream. Source for the Operator contract + coordinate-normalization-at-the-boundary lesson (D2). |
| `OmniParser/` | https://github.com/microsoft/OmniParser | Pure-vision screen parser: screenshot → labeled, ID-addressable, normalized-bbox interactable elements with Set-of-Marks overlay. Reference for Rung-1 observation (server-side, on-demand) and "never emit raw pixel coordinates" grounding (D3); per-frame re-parse with no caching is the inefficiency Shinken improves on. |

> Note: `references/README.md` currently documents only `OSWorld/` in its table; the other
> repos above are present in the working tree and should be added to that table (with upstream
> URL + one-line rationale) when the references manifest is next updated.

---

### Notes on currency & verification
Schema/version strings (Anthropic `computer_2025xxxx`, OpenAI `computer-use-preview`) move
fast — OpenAI has announced a `computer-use-preview` shutdown and Operator sunset — which is
exactly why Shinken pins **dated** adapters (D2). Every **latency / density / bitrate / cost**
number drawn from a vendor blog or product page above is **vendor-published and unverified**;
the first-party measurement plan (a11y coverage, fork P99, glass-to-glass latency, egress
$/concurrent) is tracked in [open-questions.md](open-questions.md).
