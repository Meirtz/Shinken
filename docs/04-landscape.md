# 04 — Competitive & Technology Landscape

> **Scope.** This survey maps the public field Shinken enters and the public technology Shinken is built from. It is written to be read alongside the design: [00 Vision](00-vision.md), [01 PRD](01-prd.md), [02 Architecture](02-architecture.md), [05 Technical Decisions / ADRs](05-tech-decisions.md), and the deep notes in [`notes/`](../notes/README.md). Every competitor and tool named here is public and open-source or publicly documented. Speed, density, and cost figures pulled from vendor material are tagged **(vendor-published, unverified)** — the first-party measurement plan that retires those tags lives in [09 Economics & Build-vs-Buy](09-economics-and-build-vs-buy.md). Full source URLs are consolidated in [`notes/sources.md`](../notes/sources.md).
>
> Date of survey: **2026-05-30.**

Shinken is an AI-native, cross-platform sandbox **runtime + control plane + control panel** for computer-use agents: a streaming-first successor to OSWorld that boots isolated desktops, drives them through one typed Agent-Computer Interface (ACI), streams operations live, records them as a scrubbable/forkable event-sourced replay, gates privileged actions behind a capability-unlock permission panel, and exposes an eval layer on the same runtime. The question this document answers is: **who else is in this space, what have they actually shipped, and where exactly does Shinken match, beat, or differentiate?**

---

## 1. The four-camp framing

The single most useful map of this market is that it is **four non-overlapping camps**, and Shinken's target sits at their *unclaimed intersection*. No public product spans all four; the closest (trycua/cua) spans two and a half.

```
                      MODELS + EVALS
              (Anthropic Computer Use, OpenAI
               Operator/CUA, HUD, UI-TARS, OmniParser)
                           ▲
                           │  define the ACI you must speak
                           │  + the eval bar you must clear
                           │
  CROSS-PLATFORM ◄─────────┼─────────► LINUX FAST-SANDBOXES
  RUNTIMES                 │           (E2B, Morph, Daytona,
  (trycua/cua —      ┌─────┴─────┐      Modal, forkd, Kata+FC)
   closest analog)   │  SHINKEN  │      "fork-from-snapshot,
   "one API, any OS" │  target   │       sub-ms CoW, density"
                     └─────┬─────┘
                           │
                           ▼
                     BROWSER-ONLY SaaS
            (Browserbase/Stagehand, Scrapybara,
             Kernel, Steel.dev, Hyperbrowser, Anchor)
            "rent a cloud Chromium over CDP, live view"
```

| Camp | What it owns | What it lacks vs. Shinken |
|---|---|---|
| **Cross-platform runtimes** | One SDK, any OS, cloud-or-local, model-agnostic loop, MCP everywhere (trycua/cua) | Pull-based screenshot observation, coarse permissions, no event-sourced replay, no fast snapshot-restore |
| **Linux fast-sandboxes** | Sub-second/sub-ms CoW fork, microVM density, warm pools (Morph, E2B, Daytona) | Linux-only, browser/desktop layer is thin or BYO, no replay timeline, no permission panel, **no GPU passthrough on Firecracker** |
| **Browser-only SaaS** | Sub-second provisioning, embeddable WebRTC live view, session record/replay, scoped tokens | Chromium-only — no native desktop apps, installers, OS dialogs, multi-window, no macOS |
| **Models + evals** | The action grammars (Anthropic/OpenAI), grounding (OmniParser/UI-TARS), the eval contract (HUD) and benchmark (OSWorld-Verified) | These are *clients and graders* of a runtime, not a runtime; each is one layer of the stack Shinken integrates |

The strategic read (reconciled to canon §3): **MATCH** the fork/branch leaders and the model ecosystem and the eval bar; **BEAT** everyone on streaming/bandwidth (every competitor polls screenshots or pushes raw VNC pixels); **DIFFERENTIATE** on event-sourced replay/branching, the capability-unlock permission panel, full cross-platform *desktop* (not browser-only), and an optional GPU-accelerated tier.

---

## 2. Competitor capsules

Each capsule: **What** it is · **Strengths** · **Weaknesses** · **Lesson** (what Shinken adopts/adapts/avoids, and which decision it touches).

### 2.1 trycua/cua — the closest analog

**What.** Open-source (MIT, YC X25, ~15K stars) framework to "build, benchmark, and deploy agents that use computers." One Python/TS SDK spins up isolated desktop sandboxes (Linux/macOS/Windows/Android; container or VM; cloud or local) and drives them with a model-agnostic loop. Clean `Image → Runtime → Transport → Interfaces → Sandbox` layering. In-guest `computer-server` (FastAPI/uvicorn, ~50 typed commands over a persistent WebSocket) plus a `cua-driver` Rust core that returns a per-window accessibility tree with stable `[N]` element handles. Dual MCP posture (granular primitives vs. agent-task). Virtualization spans `lume` (Apple Virtualization.framework on Apple Silicon), QEMU-in-Docker for Linux/Windows, and native Windows Sandbox. Source: <https://github.com/trycua/cua>.

**Strengths.** Owns the "one API, any OS, cloud or local" positioning with the deepest backend matrix in the field. Genuinely good DX: immutable chainable `Image` builder, three-mode lifecycle (ephemeral/create/connect), callback pipeline for telemetry. `cua-driver`'s snapshot-keyed AX-tree handles are exactly the structured-observation model Shinken wants. cua-bench ("cua gym") sits on top as a Gym-style eval harness with an oracle-solver regression check.

**Weaknesses.** Observation is **pull-based**: a fresh base64-PNG screenshot fetched per agent step over HTTP/SSE (one frame per `/cmd`); no real-time continuous pixel stream in the SDK (live viewing is offloaded to VNC, or H.265 in the CuaBot demo). A `commandLock` serializes one command at a time per interface — no input batching, RTT per action. Permissions are **coarse**: one API key per VM, no per-action authorization, no FS jail/egress controls in the transport — the safety-check path is literally a TODO. Two overlapping SDK families coexist (legacy `Computer`/`VMProviderFactory` vs. newer `cua_sandbox`). Reset is cold clone-from-golden; no live snapshot/restore.

**Lesson.** ADOPT the layering and three-mode lifecycle and the per-OS handler-factory abstraction wholesale (D2/D8/D10). ADOPT `cua-driver`'s stable element-handle model as the basis of structured observation (D3). **BEAT** them on streaming with the dual-channel ACI (D4) and on permissions with a real per-action policy/approval engine (D6) — both are their explicit gaps and Shinken's wedges.

### 2.2 E2B Desktop — the open Linux reference

**What.** Open-source SDK (`e2b-desktop`) that turns a generic E2B cloud sandbox into a full graphical Linux desktop: Xvfb framebuffer + Xfce4 + x11vnc + noVNC, ~600 LOC of thin domain layer over the base Firecracker-backed command/file API. Sources: <https://github.com/e2b-dev/desktop>, <https://e2b.dev/docs/sandbox/persistence>.

**Strengths.** Clean, isolated **Firecracker microVM** lifecycle is the bar for the Linux tier: `create(template, timeout, metadata, secure, allow_internet_access)` + `connect(id)` + auto-pause-on-timeout + indefinite resume, ~128 MB/sandbox, OverlayFS CoW disk, per-port public HTTPS proxying. Snapshot-restore via UFFD lazy paging + HugePages cited at **~28-200 ms restore** after warm-up (vendor-published, unverified; <https://dev.to/adwitiya/how-i-built-sandboxes-that-boot-in-28ms-using-firecracker-snapshots-i0k>).

**Weaknesses.** Streaming is **raw VNC/RFB over WebSocket** — no H.264/VP9/WebRTC, no delta/dirty-rect, re-sends full frames. Every action is a separate `commands.run` round-trip spawning an `xdotool`/`scrot` process (one process per click/keystroke/screenshot). Single concurrent stream per sandbox (a `pgrep` guard). **No built-in action/observation replay** — pause/resume is state-restore, not an event log.

**Lesson.** ADOPT the substrate pattern (thin desktop SDK over a generic isolated runtime) and the Firecracker snapshot-restore cold-start mechanism (D1). ADOPT the lifecycle API shape (D9). Do NOT copy raw VNC — build the encoded delta stream instead (D4). Build the replay primitive E2B lacks (D5). Use a persistent in-guest input agent, not one process per action (the Guest Runtime, D2).

### 2.3 Morph (Infinibranch) — owns the fork dimension

**What.** A microVM platform whose killer primitive is "fork the world at step N": capture full live VM state (RAM + disk + process tree + sockets) as an immutable snapshot, then CoW-fork it into one-or-many running children in sub-second time. Sources: <https://cloud.morph.so/docs/documentation/instances/branch>, <https://cloud.morph.so/docs/developers>.

**Strengths.** Best-in-class reset/fork numbers: CoW `mmap` **~4 µs**, end-to-end fork **P99 ~1.3 ms at 1000 concurrent**, ~93% pages shared (~3-27 MB private/instance), <250 ms snapshot/branch/restore, branch-to-hundreds, scale-to-zero-and-infinity (all vendor-published, unverified). The open `forkd` (<https://github.com/deeplethe/forkd>) demonstrates the same mechanism: spawn N=100 children in ~101 ms (~1 ms/child), BRANCH a running VM in ~150 ms, ~0.12 MB CoW metadata/child. Modal's memory snapshots and gVisor demand-paging restore round out the public technique set.

**Weaknesses.** Headline numbers ship without methodology or independent reproduction. Live-fork has a pause window proportional to dirty memory (`forkd` ~3 ms/GiB even optimized). Repeated branching regresses without compaction (`forkd` hit 2.7 s on the 6th branch before a fix). CUDA/GPU checkpoint requires specific NVIDIA driver branches and is alpha. Morph publishes little internal CoW/memory data; macOS/Windows guests are not on a clear public roadmap; the Q4-2025 GPU support state is unverified.

**Lesson.** **MATCH** the fork-from-snapshot primitive as the *core of both reset and replay-branching* — they are the same operation exposed two ways (D1, D5). ADOPT CoW everywhere (MAP_PRIVATE RAM + qcow2/overlay disk), lazy/background restore, a warm-parent template pool, and the per-fork uniqueness hook (reseed RNG, regenerate MAC/hostname/boot-id). ADOPT snapshot-chain compaction to keep deep replay trees fast.

### 2.4 Browser-only SaaS — Browserbase, Scrapybara, Kernel (and Steel.dev, Hyperbrowser, Anchor, Browserless)

**What.** A commercial cohort renting cloud browser (occasionally full-desktop) instances to agents, exposed over CDP/Playwright + REST, with a streamed live view, session record/replay, stealth/anti-bot, proxies, and usage billing. Browserbase ships Stagehand + an embeddable Session Live View and video Session Replay (<https://docs.browserbase.com/features/session-live-view>, <https://docs.browserbase.com/features/session-replay>); Kernel offers reserved pools for predictable latency (<https://www.kernel.sh/docs>); Scrapybara was the one near-desktop player (Linux/Windows, no macOS) and has effectively sunset; Anchor pairs with Cloudflare Web Bot Auth for *verified* agents (<https://anchorbrowser.io/blog/anchor-cloudflare-verified-browser-agents>).

**Strengths.** They define the human-in-the-loop and provisioning table stakes: embeddable iframe live view with read-only/interactive toggle, scoped/expiring tokens, yield/take-control, video-based deterministic replay (fMP4/HLS/MP4 — the market actively **abandoned** DOM-based rrweb replay because it diverged from reality), and sub-second provisioning at 100s-1000s concurrency with reserved pools.

**Weaknesses.** Almost all are **Chromium-only** — they cannot run native desktop apps, installers, OS dialogs, multi-window workflows, Office/IDE software, or non-web UIs. None offers cross-platform desktop (even Scrapybara had no macOS). Stealth focus skews the value prop toward scraping/evasion (legal/reputational risk). WebRTC OS-capture at ~25 fps and HLS are bandwidth-heavy; none publicly leads on aggressive delta/ROI bandwidth optimization. Mostly hosted/proprietary.

**Lesson.** ADOPT WebRTC live view + video (fMP4/HLS) replay and the full HITL primitive set (D4, D6, control panel). **DIFFERENTIATE** on full cross-platform *desktop* — the entire cohort's structural gap — and on bandwidth-optimized streaming for high-concurrency desktop fleets, where no competitor publicly leads (D3/D4). ADAPT the *verified-agent* path (Web Bot Auth) over racing on stealth.

### 2.5 Anthropic Computer Use — the reference to match-and-beat

**What.** Anthropic's official open-source reference (`anthropic-quickstarts/computer-use-demo`): the canonical hosted tool schema (`computer` / `bash` / `str_replace_based_edit_tool`), a minimal agentic sampling loop, and a Linux desktop streaming stack (Xvfb + x11vnc + noVNC + Streamlit). Docs: <https://code.claude.com/docs/en/computer-use>.

**Strengths.** It *is* the de-facto ACI a Claude-compatible runtime must match: hosted tool `type`s `computer_20241022 / 20250124 / 20251124` (+ `display_width_px`/`display_height_px`), the action grammar, coordinate space, the 28×28-patch / 1568px (now 2576px on Opus 4.7/4.8) image-resize math, prompt-caching discipline, and the `ToolResult → tool_result` observation block shape. Bundling `bash` + `text_editor` alongside GUI control is a proven score lever.

**Weaknesses.** Model feedback is **screenshot-per-step, not streaming** — a hardcoded 2.0 s settle delay plus a synchronous ImageMagick resize before every capture. noVNC streaming is stock x11vnc + websockify with **no tuning** (no adaptive quality, region encoding, H.264, or bandwidth negotiation) and re-sends full frames. Two parallel pixel paths (screenshots for the model, VNC for humans) double the capture cost. No durable structured replay in the canonical demo.

**Lesson.** **MATCH** (non-negotiable): the exact tool schema, version/beta-flag grammar, coordinate space, and the resize math (so the pixels sent equal the pixels the model reasons over) — via a version-pinned `AnthropicComputerAdapter` (D2). **BEAT** the screenshot-per-step model and the stock-VNC wire (D3/D4 — this is literally what Shinken exists to win). ADD what Anthropic lacks: durable event-sourced replay (D5) and an in-loop permission gate (D6). AVOID leaving `~/.ssh`/`~/.aws` readable by default (a Claude Code footgun) — deny credential dirs by default.

### 2.6 OpenAI Operator + computer-use-preview (CUA)

**What.** OpenAI's computer-use offering: Operator (consumer agent, later folded into ChatGPT Agent mode) driving a cloud-hosted remote browser in an OpenAI VM, and `computer-use-preview` (the CUA model) exposed via the Responses API `computer` tool — `computer_call` / `computer_call_output`, an `actions[]` array + `call_id`, and a `pending_safety_checks → acknowledged_safety_checks` approval seam. Docs: <https://developers.openai.com/api/docs/guides/tools-computer-use>, <https://openai.com/index/operator-system-card/>.

**Strengths.** Together with Anthropic, it defines the small stable action vocabulary Shinken's ACI must speak natively, the **batched `actions[]` per turn** pattern (cuts round-trips), and a machine-readable permission gate (`pending_safety_checks`) plus a takeover/watch mode so secrets never enter the observation stream. A prompt-injection screenshot classifier reports 99% recall / 98.4% accuracy (vendor-published, unverified).

**Weaknesses.** Pixel-coordinate clicking is **brittle** and resolution-sensitive (docs prescribe exact resolutions + coordinate clamping). Explicitly preview/experimental — not for production; reports of 3+ minute responses under load. Bandwidth-heavy: a fresh full-resolution PNG every turn (up to ~10.24M px), no native frame diffing. The safety acknowledgement is under-implemented in tooling — the official sample errors with `unsupported_safety_acknowledgement`.

**Lesson.** ADOPT the small stable action vocabulary and speak both OpenAI and Anthropic wire-formats natively via thin adapters (D2). ADOPT batched `actions[]`, the `pending_safety_checks`-style machine-readable gate, and the typed-SSE + persisted-replay-bundle shape (D5/D6). AVOID re-sending full-res PNG per turn (D3) and AVOID OpenAI's own gap — actually implement end-to-end operator approval.

### 2.7 HUD (hud.so / hud-evals) — the eval-on-top-of-runtime reference

**What.** A hosted platform + open-source Python SDK (`hud-python`, MIT, YC W25) for building/running/scaling agent evals and RL environments, especially for CUAs. Clean task contract `setup → run → evaluate → reward[0,1]`; default trace recording with replayable scorecards; hosts OSWorld-Verified + SheetBench-50 as MCP-based RL environments; model-agnostic agent factory. Sources: <https://github.com/hud-evals/hud-python>, <https://docs.hud.ai/>.

**Strengths.** The cleanest public eval-layer design: execution-based evaluation against runtime state (never string-match), runtime separated from the eval layer but bound by one thin contract, "fresh isolated instance per run" default, non-blocking trace upload, an OpenAI-compatible inference gateway. Owning canonical benchmarks is part of its moat.

**Weaknesses.** Heavy dependency on MCP/FastMCP and a constellation of hosted services — using it at scale effectively **locks you to the platform**. Sandboxing granularity is coarse (one container per run; cold-start + per-env-hour cost dominate). OSWorld-style VM tasks are slow and expensive; **local connections are explicitly not parallelizable**, forcing you onto hosted remote envs.

**Lesson.** ADOPT the `setup/run/evaluate → reward` contract and execution-based grading (D7), the "fresh isolated instance per run" default made affordable by cheap CoW fork (D1), and default non-blocking trace recording (D5). MATCH the one-line provider-agnostic eval bar. But where HUD routes the contract through MCP, Shinken binds eval to its **native streaming runtime** and treats MCP as an optional facade — **never** route the hot loop through MCP (D8).

### 2.8 UI-TARS-desktop — the Operator-layer analog

**What.** A ByteDance monorepo: (1) UI-TARS Desktop, an Electron app + `@ui-tars/*` SDK that drives the UI-TARS VLM against the local computer, local browser, or a remote/sandboxed computer/browser behind one interface; (2) the newer `@tarko`/`@gui-agent` stack with a typed action schema. Source: see <https://github.com/bytedance/UI-TARS-desktop>.

**Strengths.** The closest public analog to Shinken's **client/Operator layer**: it already solves operator pluggability (local nutjs / local CDP / remote VM / remote browser behind one target interface) and exposes an event stream (`run_start`, `tool_call`, `tool_result`, `screenshot`, `run_end`). The new typed `BaseAction<type, inputs>` + a `Coordinates` type carrying *both* normalized (0-1) and raw pixels is the right shape.

**Weaknesses.** Two parallel overlapping architectures (legacy `@ui-tars/sdk` vs. new `@tarko`) with duplicated Operator/action-parser concepts. **Vision-only** observation — no DOM/a11y-tree fusion by default, so the agent is fully dependent on screenshot quality and model grounding. The legacy stack doesn't stream tokens and runs one global singleton session. Permission model is coarse and macOS-centric (OS Accessibility/Screen-Recording gating only; no per-action confirmation, allowlist, or scoped capabilities).

**Lesson.** ADOPT the Operator contract — `{observe, execute(action), supportedActions(), screenContext()}` with a target-agnostic loop and the operator advertising its action space (D2 Operator). ADOPT the typed action schema with dual-space coordinates and **normalize coordinates in the operator boundary, not the model** (D2). Make remote == local by treating each action as a transport call. Improve on their gaps: feed execution failures back to the model as observations, fuse a11y with vision (D3), add a real permission layer (D6). UI-TARS is also one of the four ACI adapter targets (D2).

### 2.9 microsoft/OmniParser (+ OmniTool)

**What.** A pure-vision screen-parser that converts a raw GUI screenshot into a structured list of labeled, interactable elements (icons + text), each with a numeric ID, normalized bbox, functional caption, type, and interactivity flag — Set-of-Marks overlay + element list. Sources: <https://github.com/microsoft/OmniParser>, <https://www.microsoft.com/en-us/research/articles/omniparser-v2-turning-any-llm-into-a-computer-use-agent/>.

**Strengths.** The reference for the **escalation rung** when the a11y tree is empty: a structured, ID-addressable, normalized-coordinate element list as the model-facing observation, so the model emits a stable element ID and never regresses raw pixel coordinates. Pseudo-HTML serialization is token-cheap. V2 cites ~0.6 s/frame on A100 (vendor-published, unverified).

**Weaknesses.** Not low-bandwidth on the image channel — it still ships a full-resolution SoM screenshot; only the element *list* is compact (no tiling/diffing/ROI). Captioning (Florence-2 on small crops) is the latency + quality bottleneck and produces generic captions. **Stateless per-frame** — re-parses every frame from scratch, no temporal/diff caching. Detection quality is YOLO-bounded.

**Lesson.** ADOPT Set-of-Marks ID grounding and the structured element schema with provenance `{id, type, bbox(normalized), interactivity, content, source}` (D3). ADOPT the structured-list-primary, pixels-on-demand dual channel. ADAPT for incremental/diff observation (cache per-element results, re-parse only changed regions) and a lazy captioning policy — fix OmniParser's per-frame re-parse for the streaming case. Run it as a **stateless GPU microservice** so grounding scales independently (rung 1 of D3).

### 2.10 m1k1o/neko — the WebRTC delivery reference

**What.** Self-hosted virtual browser/desktop running any Linux GUI app in Docker and streaming it to the browser over WebRTC. A Go server captures X11 via GStreamer, encodes to VP8/VP9/AV1/H264 + Opus, sends media over WebRTC tracks and carries low-latency input over a binary DataChannel. Source: <https://github.com/m1k1o/neko>.

**Strengths.** Its core delivery design — desktop → GStreamer → WebRTC media track + binary DataChannel control — is essentially what Shinken needs for browser-delivered real-time streaming. **Shared-encoder/listener fan-out** (encode once per quality, attach many peers), lazy pipelines, runtime source-swap for quality changes (no SDP renegotiation), a keyframe-lobby for new viewers, a compact `{opcode,len}` binary input protocol, and pion GCC + TWCC ABR with hysteresis (<https://github.com/pion/webrtc>). Cites WebRTC glass-to-glass <300 ms.

**Weaknesses.** Server-side ABR switching is coarse (discrete hq/lq steps, multi-second hysteresis). No SVC (no temporal/spatial scalability) — switching forces a keyframe and a quality jump. All peers on one quality share keyframe timing. `ximagesrc use-damage=false` captures full frames at fixed fps regardless of screen change — wastes encode/bandwidth on static screens.

**Lesson.** ADOPT the shared-encoder fan-out, runtime source-swap, keyframe-lobby, media/control plane split, and the GCC+TWCC+hysteresis ABR recipe — verbatim where it fits (D4). Fix neko's gaps: damage-driven capture (don't encode static screens), and pair it with NVENC instead of software x264 on the GPU tier (D4/D11). neko is already a vendored reference for the streaming layer.

### 2.11 OpenAI Codex sandbox (codex-rs) + Claude Code — the permission/sandbox reference

**What.** The command-execution sandboxing of the leading agentic-coding tools. Codex (`codex-rs`) enforces FS + network isolation per shell command with an OS-native backend per platform (Linux: bubblewrap + seccomp; macOS: Seatbelt/`sandbox-exec`; Windows: restricted token + capability SIDs) plus an out-of-sandbox egress proxy. Claude Code adds a deny>ask>allow permission model with persistable scoped rules and managed-settings lockdown. Sources: <https://developers.openai.com/codex/concepts/sandboxing>, <https://code.claude.com/docs/en/sandboxing>, <https://code.claude.com/docs/en/permissions>.

**Strengths.** The most directly reusable, battle-tested permission engineering in the field. Codex's **orthogonal three-axis model** — command admissibility (`execpolicy` allow/prompt/forbidden) × sandbox confinement mode (read-only/workspace-write/full-access) × approval policy (never/on-request/on-failure/untrusted) — with **strictest-wins** merge (`Ord` = Allow < Prompt < Forbidden, take `.max()`). Ordered `(path, Read|Write|Deny)` FS policy with path-specificity ordering. Pass paths as sandbox *parameters*, not string-interpolated policy. Fail **closed** on network. Escalation-on-failure UX. Claude Code's deny>ask>allow with deny-always-wins, source-attributed rules, and managed/org lockdown.

**Weaknesses.** Both default proxies allowlist by **client-supplied hostname without TLS inspection** → domain-fronting / exfil risk. Claude Code's default `denyRead` still exposes `~/.aws/credentials` and `~/.ssh`. Bash argument-constraining rules are fragile (`Bash(curl http://github.com/ *)` is trivially bypassed). Claude Code computer-use runs on the **real desktop, not sandboxed** — a weaker trust boundary than its Bash path. Codex's `bwrap.rs` is 2700+ lines (FS-policy translation is genuinely hard).

**Lesson.** ADOPT the two-layer UI split (a "what it CAN touch" OS-sandbox layer vs. a "when to ASK" approval-policy layer), the strictest-wins decision merge, ordered path-specificity FS policy, the escalation prompt shape (justification + minimal grant + scope choices), and the out-of-sandbox egress proxy reached over a Unix socket with an OS backstop (D6). ADOPT Codex's HTTP-method allowlist (GET/HEAD/OPTIONS) as a cheap exfil mitigation and offer a TLS-MITM mode for high-risk sessions. AVOID their gaps: deny credential dirs by default, sandbox computer-use, and reject domain-fronting at the proxy (see [08 Threat Model](08-threat-model.md)).

### 2.12 OSS remote-desktop streaming stacks (Selkies-GStreamer, Sunshine, Apache Guacamole, KasmVNC, noVNC)

**What.** Five OSS remote-desktop stacks evaluated as foundations/learning sources for the Linux streaming layer. Survey: <https://github.com/selkies-project/selkies>, <https://docs.lizardbyte.dev/projects/sunshine/master/>, <https://guacamole.apache.org/doc/gug/guacamole-architecture.html>, KasmVNC, noVNC/websockify (<https://github.com/novnc/websockify>).

**Strengths.** Selkies is the closest reference pipeline: `ximagesrc → NVENC (nvcudah264enc) → webrtcbin → browser`, input/clipboard over a WebRTC DataChannel — and validates the GStreamer-NVENC-WebRTC path Shinken wants. Sunshine contributes a capture-backend abstraction + zero-copy GPU pipeline (frames GPU-resident from DXGI/KMS/NvFBC through NVENC). KasmVNC contributes per-rectangle adaptive quality + an EncCache and a "static UI = low quality, motion = high quality" heuristic. Guacamole contributes a stateless-web-tier + per-session-backend horizontal scaling pattern.

**Weaknesses.** Selkies is **Linux/X11 only** today (no Wayland/Windows/macOS) — fails the cross-platform mandate alone. Sunshine is **not browser-deliverable** (needs a native Moonlight client). Guacamole transports images/instructions over TCP → higher latency, WebSocket head-of-line blocking, no GPU encode. KasmVNC's HW encode is **VAAPI-only (no NVENC)** and uses TCP/WebSocket RFB.

**Lesson.** ADOPT the Selkies GStreamer→NVENC→webrtcbin model as the Linux streaming reference and likely starting point; ADAPT Sunshine's zero-copy capture abstraction across guests; ADAPT KasmVNC's per-rectangle adaptive quality + content heuristic but pair it with NVENC; ADAPT Guacamole's stateless-web-tier scaling pattern (and heed its per-session-RAM × cores × connections failure mode). KEEP noVNC/websockify only as a universal low-fidelity fallback viewer. **BUILD** (don't adopt) the differentiators on top: the structured event-replay/fork timeline, the permission panel, and the ACI (D4/D5/D6).

### 2.13 The rest of the field (briefly)

- **OpenAdaptAI/OpenAdapt** — OSS desktop record-and-replay RPA. Validates the replay thesis; hands over a capture format (timestamp-correlated action+screenshot+window+a11y+DOM streams, H.264 sidecar) and a clean Strategy/`get_next_action_event` abstraction (ADOPT, D5). AVOID its host-coupled execution (replay drives the real cursor, no sandbox) and SQLite-blob + global-lock storage.
- **Agent-S/S2/S3, Cradle, Bytebot, Self-Operating-Computer, Open Interpreter, AskUI, Tarsier** — research/OSS operators (<https://github.com/simular-ai/agent-s>, <https://github.com/bytebot-ai/bytebot>). ADOPT Agent-S2's Mixture-of-Grounding split and Behavior Best-of-N (+~10pt OSWorld) and Bytebot's containerized isolation + live streaming + takeover baseline (D3/D7). AVOID Self-Operating-Computer / Open Interpreter driving the host with no sandbox — the explicit anti-pattern.
- **OpenCUA / e2b open-computer-use / HF Open Computer Agent** — open CUA prior art. ADOPT OpenCUA's synchronized screen+input+a11y recording → state-action-CoT pipeline (best-in-class for replay-as-training-data, D5). AVOID their streaming mistakes (full-frame PNG per step, single-client FFmpeg) and the universal gap: no permission model (safety = VM isolation only).
- **Android stack (scrcpy, Android Emulator, AndroidWorld)** — building blocks for the post-v1 mobile leg (D10 roadmap). scrcpy already embodies Shinken's streaming ideals (device-side HW encode, change-driven frames). AVOID emulator gRPC screenshot-polling as the streaming path.

---

## 3. The competitive matrix

Dimension × Shinken target × how others do it × verdict (**MATCH** = reach parity, **BEAT** = exceed a dimension everyone is weak on, **DIFFERENTIATE** = own ground nobody holds).

| Dimension | Shinken target (decision) | How others do it | Verdict |
|---|---|---|---|
| **Cross-platform guests** | Linux + Windows + macOS desktop v1, one control plane + one Guest Runtime + one ACI; Android roadmap (D10) | cua: macOS+Linux+Windows+Android (closest). E2B/Morph: Linux only. Browser cohort: Chromium only. | **MATCH+** (match cua, exceed scope of all others) |
| **Isolation substrate** | Tiered, substrate-pluggable, routed by (OS × needs-GPU × needs-fast-fork): Firecracker (headless Linux) / QEMU-microvm/crosvm (Linux desktop) / Cloud Hypervisor (Windows+GPU) / Apple VZ (macOS) / OSS `agent-sandbox` CRD on gVisor/Kata (D1) | E2B/Morph: Firecracker only. cua: Apple VZ + QEMU + Docker + Windows Sandbox. Browser cohort: container/microVM. | **MATCH** (no single competitor spans the matrix) |
| **Reset / fork speed** | MATCH Morph: target <30 ms VMM restore + sub-ms CoW fork on Linux tier (MAP_PRIVATE + userfaultfd + warm pools); GPU/Win/mac tiers snapshot-light (D1) | Morph: P99 ~1.3 ms fork (best). E2B: ~150 ms restore. cua: cold clone-from-golden only. | **MATCH** (Linux); honest **degrade** on GPU/Win/mac |
| **Streaming & bandwidth** | BEAT all: dual-channel — primary structured a11y/DOM/SoM event stream (~20 kbps) + on-demand NVENC H.264/AV1 video over WebRTC (D3/D4) | OSWorld: full-PNG polling. E2B/cua: VNC/screenshot. Browser cohort: CDP + screencast. Anthropic/OpenAI: base64 PNG per turn. | **BEAT** — ~150× cheaper than H.264 office video (the headline win) |
| **Replay / branching** | DIFFERENTIATE: one event-sourced logical-clock timeline (the live stream IS the replay log) + bisected env/agent snapshots + immutable checkpoint DAG; scrubbable, forkable, greppable (D5) | OSWorld: `traj.jsonl` + `recording.mp4` only. Morph: VM-level branch, no agent-trajectory timeline. cua/E2B: none. | **DIFFERENTIATE** — largely greenfield |
| **Permission model** | DIFFERENTIATE: 3-layer capability-unlock — Cedar decision + ocap caretaker/membrane + OS enforcement; 8 capability classes, 4 risk tiers, taint-aware, live HITL card (D6) | OpenAI: `pending_safety_checks`. Anthropic: classifiers + HITL. Codex/Claude Code: OS sandbox + egress allowlist. cua/E2B/OSWorld: none / TODO. | **DIFFERENTIATE** — closest prior art is per-command coding tools, not CUA runtimes |
| **AI-native ACI** | Native streaming ACI: small typed verb set + a11y/SoM-first layered observation with stable element refs, pixels/video on demand; version-pinned bidirectional adapters (Anthropic/OpenAI/UI-TARS/computer_13) as the only model-facing surface (D2/D3) | Anthropic `computer_2025xxxx` + bash + text_editor. OpenAI `computer_call` + `actions[]`. cua: typed WS dispatch. | **MATCH** the schemas, **DIFFERENTIATE** the structured-first observation + element-ref targeting |
| **Eval support** | MATCH + reproduce: built-in OSWorld-Verified / WindowsAgentArena / AndroidWorld / WebArena conformance, deterministic setup, N≥5 CoW replicas → pass@k/pass^k + CIs, graders as tested artifacts (D7) | OSWorld-Verified: the de-facto bar (369 tasks, AWS ~50×, ~1h). HUD: hosts it as MCP RL envs. cua-bench: Gym harness. | **MATCH** + inversion (thin orchestration on the runtime, typed verifier DAG) |
| **Concurrency / scale** | MATCH Morph/E2B Linux density via CoW-fork warm pools + SFU stream fan-out + Action Gateway scheduler; accept heavier GPU/Win/mac tiers (D9) | Morph: branch-to-hundreds. E2B: warm pools, ~128 MB/sandbox. Browserbase: tiered concurrency. | **MATCH** (Linux); structurally heavier on premium tiers |
| **Open-source vs hosted** | Open/self-hostable core + reusable Operator + open agent loop; hosted control panel/observability/permission-audit/eval as the commercial layer (D12) | Open: E2B, cua, OSWorld, HUD. Hosted: Anthropic, OpenAI, Browserbase. | **MATCH** the open core; the market punishes closed single-modality products |
| **GPU acceleration** | DIFFERENTIATE (optional tier): GPU-accelerated guests (Cloud Hypervisor VFIO / vGPU / MIG) + NVENC streaming (NICE DCV-class); GPU-TEE + attestation for trusted workloads (D11) | E2B/Morph (Firecracker): **no GPU passthrough at all**. cua: GPU via underlying host, not a managed tier. Browser cohort: headless. | **DIFFERENTIATE** — no competitor offers a managed GPU desktop tier |

---

## 4. Per-domain technology options

For each Shinken pillar, the public technology menu and the choice reconciled to D1-D12.

### 4.1 Streaming (D4)

| Option | Pros | Cons | Decision |
|---|---|---|---|
| Stock VNC/RFB over WebSocket (E2B, Anthropic, noVNC) | Universal, trivial | Full-frame re-send, no HW encode, no congestion control | Fallback viewer only |
| WebRTC media track + DataChannel (neko, Selkies, Browserbase) | HW-decoded browser playback, GCC/TWCC congestion control, jitter buffering, unified video+data plane | Signaling/TURN/ICE operational cost | **CHOSEN transport** |
| WebCodecs + WebTransport/MoQ | Per-frame control, AV1 screen-content, CDN-relay scale | Newer; v2 fast path | Prototype *after* v1 |
| Sunshine + Moonlight | Zero-copy GPU pipeline | Not browser-deliverable | Borrow the capture abstraction only |

**Choice (D4):** single-PeerConnection, **dual-transport**: a reliable-ordered DataChannel carrying the structured event stream (*this is the replay log*) + an on-demand NVENC H.264/AV1 media track, screen-content-tuned, fanned out at an **SFU** (encode-once), with WHIP/WHEP signaling. Host↔guest = `virtio-vsock`, never HTTP polling. Same-region glass-to-glass target ~50-120 ms (<https://github.com/pion/webrtc>; <https://github.com/selkies-project/selkies>). NICE DCV (NVENC + QUIC + browser + auto-adaptation, <https://docs.aws.amazon.com/dcv/latest/adminguide/what-is-dcv.html>) is the **build-vs-buy** option for the pixel channel.

**Bandwidth economics (the BEAT axis).** Structured ≈ 20 kbps vs. H.264 office content ~3 Mbps ≈ **~150×** cheaper; AV1 screen-content ≈ ~40% below H.264 (vendor-published, unverified). At N=100k concurrent 24×7, egress runs ≈ $4.86M/mo (H.264 office) vs. ≈ $814k/mo (AV1-SCC busy) vs. *tiny* (structured) — see [09 Economics & Build-vs-Buy](09-economics-and-build-vs-buy.md) and <https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/>. This is why structured-first is the default, not pixels.

### 4.2 Isolation (D1)

| Option | Display/GPU | Fast fork | Best for |
|---|---|---|---|
| **Firecracker** | None (5 virtio devices; no graphics/PCIe/VFIO) | 5-30 ms VMM restore, ~125 ms boot | Headless Linux code/agent tier |
| **QEMU-microvm / crosvm** | virtio-gpu (in-tree / rutabaga_gfx) | Fork-capable | Linux *desktop* tier |
| **Cloud Hypervisor / QEMU+VFIO** | Display + VFIO/vGPU | Snapshot experimental, mutually exclusive with VFIO | Windows + GPU tiers (longer-lived) |
| **Apple Virtualization.framework** | Native (host-level capture) | No fast snapshot today; **2 VMs/host cap** | macOS (Apple HW only) |
| **OSS `agent-sandbox` CRD on gVisor/Kata** | Container | gVisor demand-page restore | K8s fast-path, warm pools |

**Choice (D1):** tiered, substrate-pluggable, routed by `(OS × needs-GPU × needs-fast-fork)`. Linux default = Firecracker (headless) + QEMU-microvm/crosvm (desktop, virtio-gpu); reset = fork-from-snapshot (MAP_PRIVATE CoW + userfaultfd + warm parent pool) with a post-fork uniqueness hook. Windows = Cloud Hypervisor/QEMU + virtio-win (licensing-gated). macOS = Apple VZ on Apple HW (2 VMs/host, TCC pre-grant). GPU tier = Cloud Hypervisor/QEMU + VFIO/vGPU/MIG, *no* fast snapshot. Sources: <https://firecracker-microvm.github.io/>, <https://github.com/cloud-hypervisor/cloud-hypervisor/blob/main/docs/windows.md>, <https://kata-containers.github.io/kata-containers/use-cases/NVIDIA-GPU-passthrough-and-Kata-QEMU/>.

### 4.3 Replay (D5)

| Layer | Public technique | Source |
|---|---|---|
| Envelope | rrweb two-level discriminator (`EventType` + `IncrementalSource`) | rrweb |
| Timing | asciicast interval deltas + monotonic `seq` + wall anchor | asciicast v3 |
| Bundle | Playwright `trace.zip` — manifest + line-by-line JSONL + SHA-1 content-addressed media | Playwright Trace Viewer |
| Branching | LangGraph immutable parent-pointer checkpoint DAG | LangGraph |
| Env fork | CoW VM snapshot (Firecracker MAP_PRIVATE / qcow2 overlay) | §4.2 |
| Determinism | Bisected snapshot + event-log + observation-log; **not** bit-deterministic (rr/Hermit/Antithesis are single-core/simulation-only) | rr, Antithesis |

**Choice (D5):** the event stream + bisected snapshots in a `.skn` ZIP bundle: `manifest.json` + append-only `events.jsonl` (two-level envelope `kind ∈ {action, observation, decision, permission, marker, snapshot_ref, meta}`; logical `seq` + wall anchor; `action_id` pairing action→observation) + immutable checkpoint DAG (branchable, never mutated) + content-addressed fMP4 media. Decision channel = **OTel-GenAI** semconv. **Branch = the same primitive as reset** — CoW-fork the env snapshot + deserialize the agent checkpoint → re-run from step N. Replay doubles as RL/SFT trajectory data — the adoption wedge (D12). The human-facing replay panel adopts Playwright's four-zone layout with permission events as the highest-priority marker class. See [`notes/replay.md`](../notes/replay.md).

### 4.4 Permissions (D6)

| Layer | Option | Choice |
|---|---|---|
| Decision | **Cedar** (Lean-verified, SMT-backed, ~sub-ms, statically analyzable) vs. OPA/Rego (~1-10 ms, error-prone) | **Cedar** — the unlock panel is a privilege-escalation surface; provable grants matter most (<https://docs.cedarpolicy.com/policies/syntax-policy.html>, <https://aws.amazon.com/blogs/opensource/introducing-cedar-analysis-open-source-tools-for-verifying-authorization-policies/>) |
| Handle | ocap caretaker/membrane (O(1) instant revoke) | Live, attenuable, instantly-revocable switch — Cedar is *not* the revoke mechanism |
| OS enforcement | Linux: bubblewrap + seccomp(network-gate) + Landlock + cgroups + out-of-VM egress proxy. macOS: Seatbelt + TCC. Windows: restricted token + per-workspace capability SID | Per-OS, degrade honestly (Codex/Claude Code patterns) |

**Choice (D6):** 3-layer capability-unlock. **8 capability classes** (`net.egress, fs.scope, clipboard, gpu, install.privileged/sudo` [the "unlock"], `persistence, credentials, peripheral`), **4 risk tiers** (Auto/Notify/Ask/Block), taint-aware (untrusted input promotes an action up a tier). Live HITL approval card with escalation-on-failure as the default interaction model. Egress = forced out-of-VM proxy (deny-by-default, scoped-domain, anti-domain-fronting, optional TLS-MITM, fail-closed); secrets brokered via Vault/KMS + proxy header-injection — the model never sees plaintext. Approvals/denials are first-class replay events. See [08 Threat Model](08-threat-model.md) and [`notes/permissions.md`](../notes/permissions.md).

### 4.5 ACI — Agent-Computer Interface (D2/D3)

**Action (D2):** one canonical typed tagged-union discriminated by `verb` (~16 verbs); `target = oneof{point_px | point_norm | element_ref}`; an explicit `CoordinateSpace` carried on every observation; versioned (semver) + capability negotiation at handshake. **Version-pinned bidirectional adapters** are the only model-facing surface (Anthropic `computer_20241022/20250124/20251124` + bash + text_editor; OpenAI `computer_call`; UI-TARS DSL; OSWorld `computer_13`). Code-as-action (exec/bash/edit) is a separate off-by-default capability class behind the `tool_runner` boundary.

**Observation (D3):** structured-first, layered escalation. Rung 0 (default) = normalized cross-OS a11y/DOM tree diff (AT-SPI/UIA/AX/CDP) → one `Element{ref, role, name, value, states, bbox, source}` schema with stable per-session refs (~6× token savings, ~25k vs ~150k tokens/task — vendor-published, unverified). Rung 1 = Set-of-Marks/OmniParser (server-side, on-demand, for Electron/Qt/canvas where a11y goes blind). Rung 2 = region/zoom pixels. Rung 3 = full frame. Act on element refs/marks by default; raw x,y only at the pixel rung. CDP `Accessibility.getFullAXTree` is ~80-90% smaller than raw DOM (<https://github.com/microsoft/OmniParser>; <https://github.com/browserbase/stagehand>). **Open risk:** a11y coverage on Electron/Qt/canvas/games is the load-bearing unverified assumption (canon §8) — it needs a measurement spike; that is exactly why rung 1 vision-grounding is first-class, not optional. See [`notes/ai-native-interface.md`](../notes/ai-native-interface.md).

**Interface surface (D8):** one IDL → generated py/ts SDKs over the bidirectional streaming transport, with an **optional MCP facade** at two altitudes (granular tools; agent-task) for model-agnostic hosts. **Never** route the high-frequency action/observation/video loop through MCP — MCP has no bidirectional/media transport. cua proves exactly this stratification.

### 4.6 Eval (D7)

| Option | Public technique | Choice |
|---|---|---|
| Grading | Execution/state-based (OSWorld 134 fns) · LLM/VLM-as-judge · Agent-as-a-Judge rubrics (Mind2Web-2) | Programmatic-primary (94.1% vs 79.2% human agreement, OpenComputer), constrained model-verifier as fallback |
| Task spec | OSWorld stringly-typed `getattr` (300+ grader bugs) vs. typed verifier DAG | **Typed, schema-validated verifier DAG** — graders are tested artifacts |
| Setup | Fixed sleeps (OSWorld flake) vs. golden snapshot + readiness probes | Immutable golden snapshot per task; readiness probes, not sleeps |
| Reliability | Single-run pass@1 (hides 10-30pt variance) vs. N≥5 forked replicas | **N≥5 CoW-forked replicas → pass@k / pass^k + CIs** |

**Choice (D7):** invert OSWorld — the eval layer is thin orchestration *on top of* the runtime (the HUD pattern, but bound to the native streaming runtime, not MCP). Ship built-in conformance with task+grader+env versioned together: OSWorld-Verified (369; Claude Opus 4.8 ~83.4% on the 2026-05-30 leaderboard, above the ~72.4% human baseline — vendor/leaderboard, unverified), WindowsAgentArena (~154), AndroidWorld (116), WebArena/VisualWebArena/WebVoyager. Sources: <https://xlang.ai/blog/osworld-verified>, <https://github.com/hud-evals/hud-python>. See [03 OSWorld Analysis](03-osworld-analysis.md) and [`notes/eval-benchmarks.md`](../notes/eval-benchmarks.md).

### 4.7 The optional GPU tier (D11) — public NVIDIA product facts

For the *optional* accelerated tier, public NVIDIA product facts shape the design:

- **NVENC engine placement.** A100/H100/H200/B200 have **zero NVENC engines** (the encode tier must NOT run on them) — use Ada **L4** (density, 3× 8th-gen NVENC) / **L40S** (premium 4K/AV1 + render). The consumer 8-session NVENC cap does **not** apply to datacenter/workstation GPUs (T4/A10/L4/L40S/RTX PRO) — they encode until the hardware saturates. AV1 ≈ ~40% bitrate savings vs. H.264, ~500 fps single-stream on Ada (vendor-published, unverified). Sources: <https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-application-note/index.html>, <https://developer.nvidia.com/blog/improving-video-quality-and-performance-with-av1-and-nvidia-ada-lovelace-architecture/>, <https://en.wikipedia.org/wiki/Nvidia_NVENC>.
- **GPU sharing.** Two pools: time-sliced **vGPU** (light desktops, density) and **MIG**-backed/Confidential-Containers (isolation-sensitive/trusted). MIG = up to 7 slices on A100/H100, up to 4 on RTX PRO 6000 Blackwell, each with dedicated media engines; Ada (L4/L40S) has no MIG, so don't MIG the encoders (<https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html>, <https://research.colfax-intl.com/sharing-nvidia-gpus-at-the-system-level-time-sliced-and-mig-backed-vgpus/>).
- **Trusted variant.** GPU-TEE + attestation + Confidential Containers for isolation-sensitive workloads (<https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/25.3.1/gpu-operator-kata.html>).
- **Build-vs-buy.** NICE DCV (NVENC + QUIC + browser + auto-adaptation) is the buy option for the pixel channel vs. a custom GStreamer-NVENC-WebRTC pipeline.

**The wedge:** no competitor offers a *managed* GPU-accelerated desktop tier — Firecracker (E2B/Morph) has no GPU passthrough at all, and the browser cohort is headless. GPU stays **opt-in**; the vast majority of agent/browser tasks ride the CPU-only Linux fork tier (D11).

---

## 5. Net position

```
                  cross-platform desktop breadth ▲
                                                 │      ● Shinken (target)
                              ● cua              │     (4 camps, one platform)
                                                 │
   ───────────────────────────────────────┼──────────────────────────────►
   weak                                         │              strong
   AI-native streaming / replay / permission    │
                                                 │
        ● E2B  ● Morph (Linux fork)              │
        ● Browserbase/Kernel (browser-only)      │
        ● OSWorld (eval, screenshot-poll)        ▼
```

Shinken is **not** trying to out-breadth cua on day-one backend count, out-fork Morph on the raw microsecond, or out-host Browserbase. It wins the *intersection*: the only platform that is cross-platform-desktop **and** AI-native-streaming **and** event-sourced-replay **and** capability-gated **and** eval-on-the-same-runtime — with an optional GPU tier nobody else manages. The three load-bearing risks to retire with first-party data (canon §7-8): the a11y-coverage assumption behind the structured-first bandwidth thesis (D3), the absence of first-party fork/density/latency numbers, and the macOS/Windows fast-reset infeasibility. Those, plus the consolidated threat model and the economics, are carried into [05 Tech Decisions](05-tech-decisions.md), [08 Threat Model](08-threat-model.md), and [09 Economics & Build-vs-Buy](09-economics-and-build-vs-buy.md).
