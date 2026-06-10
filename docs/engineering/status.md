# Shinken — Implementation Status (reality check)

> Date: 2026-06-11 · Scope: what is **actually built and proven** vs **designed-only** vs
> **unvalidated**. The design corpus (vision/PRD/architecture/ADRs/roadmap) intentionally describes
> the full CUA infrastructure stack; the *implementation* is the first well-tested local slice of
> that stack. This page is the honest map between target scope and current code. When in doubt, this
> file describes what exists today; the roadmap describes what v0.0.1 and later releases must add.
>
> Audience: users and implementers · Role: implementation reality check. This is the source of truth
> for what exists today.

The one-line summary: **a proven Linux/X11 pixel-observation + real-time-streaming slice exists
and is covered by live CI; v0.0.1 must still add the rest of the core semantics — adapters,
structured observation reference paths, capability/resource-scoping plumbing, artifacts,
runtime-state surfacing, and tiny eval — before Shinken is feature-complete at local/reference
scale.**

## ✅ Implemented & proven (Linux / X11)

Each row is exercised by unit tests **and** a live end-to-end smoke (Xvfb in CI and/or the Docker
sandbox image), not just by design.

| Capability | State | Proof |
|---|---|---|
| ACI v0 handshake + capability negotiation | done | `shinkend` + SDK; unit + live |
| Secure transport (handshake-first state machine, dev-token auth, non-loopback requires token) | done | #38 |
| Honest capability negotiation + ACI version/unknown-field enforcement | done | #39 |
| Pointer actions (move/click/double/right/scroll) via X11 XTEST | done | live Xvfb smoke (cursor verified with `xdotool`) |
| Keyboard (`type_text`, `key`, keysym + modifier combos) | done | #30 |
| Screenshot capture (X11 GetImage → PNG) | done | live Xvfb + Docker smoke |
| **Real-time screencast** (server-pushed frames, single-writer transport) | done | #48 — tokio WS integration test + live |
| **Bandwidth levers**: idle-frame suppression + resolution downscale (`max_long_edge`) | done | #48/#54 — live: 1280×800→640×400, ~3.4× smaller |
| **JPEG observation codec** (`format`/`quality` per action, capability-negotiated) | done | #243 — measured ~1–21× vs PNG, content-dependent ([benchmarks](benchmarks.md) §2) |
| **Lossless dirty-tile delta screencast** (changed 64-px tiles + periodic keyframes) | done | B2 — measured ~11× vs full-PNG while typing ([benchmarks](benchmarks.md) §3) |
| **`SharedLoop`** (N sync sessions on one event-loop thread) + `ping_jitter` fleet decorrelation | done | #244 — measured: 64 real sandboxes / 1,024 mock sessions on one loop thread ([benchmarks](benchmarks.md) §5–§6) |
| **Rerunnable local benchmark suites** (6 suites, raw JSON + figures tracked) | done | [`benchmarks/`](../../benchmarks) → [benchmarks.md](benchmarks.md) |
| **Focused-window / region capture** (`scope`: `screen` / `active_window` / `window:<id>`) | done | #55 — live `xclock` `window:<id>` → 200×200 |
| Python SDK: sync facade + async core, reader/demux (RPC vs server-push) | done | #51 |
| TypeScript control-surface SDK (`sdk/typescript/`) | done | tracked + CI-tested (dedicated `SDK (TypeScript)` job) |
| OSWorld `DesktopEnv` compatibility shim | done | #32 |
| CU provider adapters (Anthropic, OpenAI, **Kimi-VL**) → canonical ACI | done | fixture-tested, no live API; #75/#76 + Kimi-VL |
| **Agent-runtime narrow waist** (`shinken.runtime`: Session/rollout/Trajectory, zero scorer/reward) + **Workload registry** | done | #220/#221/#227 · [agent-runtime.md](../design/agent-runtime.md) |
| **Provider registry** + `DockerLocalProvider` + out-of-tree plugin loaders | done | #219/#226 |
| **Pluggable `shinkend` injector** (`shinken.inject`: `docker`/`ssh`/`osworld-exec`) | done | #230/#233 — chunked upload, shell-wrapped start, configurable remote path, surfaced errors |
| **OSWorld as a Workload** (`osworld-eval`) + `scripts/osworld_single.py` runner | done | #222/#229 — Kimi K2.6 in OSWorld pixel-pyautogui form, scored by the official OSWorld evaluator |
| Tiny eval harness + **`run_eval_forked`** (golden-checkpoint → fork-N → score) | done | #86/#87/#206/#231 — non-vacuous verifiers, unit-tested |
| Wheel packaging (schemas bundled), idempotent close | done | #40 |
| Docker Linux sandbox image | done | #45 — build + token handshake + screenshot in CI |
| CI: 9 jobs (guard, schema sanity, `shinkend` Rust, v0.0.1 contract gate, Python SDK, TypeScript SDK, wheel install, live Xvfb integration, Docker sandbox image) | done | every PR |

## 🟡 Partial / wired-but-stubbed

| Item | Reality |
|---|---|
| `element_ref` action targets | Present in the wire contract, but resolution **bails** — needs the a11y/observation engine (below) |
| Wire schema vs implementation | The screencast wire vocabulary (`start_screencast`/`stop_screencast`, `screencast`, `scope`, `fps`, `max_long_edge`, `stream`/`seq`, `resume_stream`) is **validated by `schema/aci.schema.json` and exercised by the contract gate** (#187); the authenticated handshake (`hello.token`) is schema-valid and contract-tested. The **error taxonomy is now implemented** (`SandboxDied` with exit/signal detail, typed per-action `act_batch` status + `failure_kind`, eval `RunResult.kind`/`infra_failure`, `provider.check_alive()`), and so is **screencast reconnect** (`resume_stream` on `start_screencast`: a live logical stream keeps its `stream` id with `seq` continuing — the frame gap readable off the first frame — while an expired one restarts fresh at seq 0). The **trajectory-level `exit_reason`** is implemented too: documented precedence `sandbox_died > setup_error > agent_error > scorer_error > max_steps > task_complete` (`shinken/runtime/trajectory.py`), set by `rollout`, the OSWorld episode/receipt, and eval `RunResult.exit_reason` (the finer projection of `kind`) — closing the #56 contract residue ([v0.0.1-plan §6](v0.0.1-plan.md)) |
| Hardening | The 2026-06 review sweep landed the queue bounds (writer channel bounded + default frame-size cap), the pre-auth upgrade/write deadlines, the vacuous-test fixes, and the typed failure taxonomy ([recalibration inventory](recalibration-2026-06.md) §3–§5, C‑4/T‑5); the screencast reconnect contract, the trajectory-level exit reason, and **subprocess scorer isolation** (T‑5: `shinken/scorer_proc.py` — fresh subprocess, atomic result file authoritative over exit code/timeout, typed `ScorerError` → `exit_reason="scorer_error"`, default-on for the external `osworld-eval` evaluator) have since landed too, closing the #56 contract residue |
| **Runtime state** (snapshot/restore/resume/fork/checkpoint) | **Implemented on the Docker disk tier** (#209, via `docker commit` + container launch) behind the provider interface (#207); Docker advertises `supports_snapshot/fork/checkpoint/resume`, `snapshot_kind="disk"`. SDK `sandbox.checkpoint()`, `sandbox.fork()`, and `sandbox.resume()` expose the provider operations, and `eval.run_eval_forked` runs the golden→fork-N→score loop over them (#231). Still missing: the CRIU memory tier and the sub-ms CoW fast tier (#206). **Competitive time-box (2026-06):** trycua/cua now ships cloud-only snapshot + CoW fork ("instant on btrfs", 1–5 s typical — vendor-published, unverified) and Agentix roadmaps "checkpoint/partial rollout … then fork" — but **no one ships a harness-integrated golden-checkpoint → fork-N → score loop** (cua-bench recreates the sandbox per reset; uni-agent/CUA-Gym/Agentix cold-boot per rollout). `run_eval_forked` is the unshipped wedge; first-party fork-vs-cold-boot numbers should be published while the lead exists. See [landscape](../design/landscape.md) |
| **Local capability gateway** (Action Gateway shim + capability envelope + ask-tier) | **Built** as an SDK-process shim (`sdk/python/src/shinken/gateway.py` + tests): records the session capability envelope (the declared resource scopes) and routes boundary requests through an allow/ask/deny decision. This is the v0.0.1 audit/policy seam for eval and runtime resource-scoping, **not** the production control-plane resource-scoping layer (no Cedar/ocap/OS layer) — see Designed-only below |

## 🔵 Designed-only — documented, **not implemented**

These appear in the vision/PRD/architecture/README in present-ish tense, but **no working code exists yet**:

- **Cross-platform**: macOS and Windows tiers (today: Linux only). Even on Linux, capture/input is **X11 only** — **Wayland** (the modern Linux default) is unaddressed and would break XTEST/GetImage.
- **Structured / accessibility observation (ADR D3)** — a **guest-runtime** a11y/CDP observation engine + element-ref resolution **does not exist** in `shinkend` (element_ref resolution still bails). SDK-local AT-SPI/CDP helpers (`a11y.py`, `cdp.py`) ship as the #2 coverage-spike harness, but they run in the SDK process — not the runtime — so this remains the core *un-shipped* differentiator.
- **Capability Manager panel + production resource-scoping layer** — the designed control-plane 3-layer model (Cedar decision + object-capability caretaker + OS-level scoping + egress proxy + secret broker) that scopes which resources a session can reach. Only the **local Action Gateway shim** exists today (see Partial, above); the production control-plane layer is **not implemented**.
- **Runtime-state memory + fast tier** — the Docker **disk** tier is implemented (see Partial, above); the **CRIU memory checkpoint** (`snapshot_kind="process"`) and the **sub-second CoW fork-from-snapshot fast tier** (Firecracker/QEMU) are **not built** and stay Phase-1, gated on a first-party latency spike. Runtime-state time-travel is the **headline differentiator** (D1/D5, #206).
- **Replay / `.skn` recording and playback** — deferred to later design work; no runtime or SDK implementation is shipped now.
- **Control plane + ultra-high-concurrency / multi-tenant orchestration** — a single local `shinkend` is all that exists.
- **Dual-channel WebRTC media plane + GPU/NVENC encode** — today streaming is **base64 PNG over the control WebSocket**, fine for an MVP but not the low-latency/bandwidth story at scale.
- **High-throughput file-transfer path** (#49/#50) — design only.
- **Code-agent RL readiness** — the typed exec/PTY verb family, headless (`needs_gui=False`) code-image profile, swerex-shim deployment backend, and token-fidelity trajectory fields are **reserved seams, design-only** ([code-agent-rl.md](../design/code-agent-rl.md)); what exists today is the substrate-side exec channel (`shinken.inject`, `put_file`/`get_file`) and `run_eval_forked` as the fork-N primitive.
- **Full OSWorld-Verified conformance at scale** — the building blocks all **ship** (see the ✅ table: OSWorld-as-a-Workload, the `scripts/osworld_single.py` runner driving **Kimi K2.6** in OSWorld's native **pixel-pyautogui code-block** form — `parse_model_actions` mirrors OSWorld's `parse_code_from_string` — scored by the **official OSWorld evaluator**, the `shinkend` injector for `--backend shinken` actuation, and `run_eval_forked` for the golden→fork-N→score loop). The open-weight Kimi-VL/Aguvis path is a separate adapter (`adapters/kimi.py`). What is **not** done here: a full OSWorld-Verified conformance sweep (Small/full set) with published numbers, and large-N forked scoring at scale.

## 🔬 Load-bearing assumptions — what has and hasn't been measured

The roadmap names these as the de-risking spikes:

- **Spike A — a11y coverage (#2): MEASURED (E5), verdict in.** Coverage of real app surfaces:
  strong for Qt via AT-SPI (0.87 addressable) and browser-via-CDP (every labeled control
  resolved; 0.23 of all nodes), weak for GTK (0.09–0.10), absent for terminals; tree-diff ~2 KiB
  vs ~77 KiB screenshot while typing. **Verdict: supports a *hybrid* per-window structured +
  pixel fallback, not structured-by-default — D3 stays Provisional** (evidence:
  [`spikes/a11y-coverage/`](../../spikes/a11y-coverage), summary in
  [docs/benchmarks/](../benchmarks/README.md)). Canvas/games and Electron remain unmeasured.
- **Disk-tier fork economics: MEASURED** — checkpoint ~0.6 s live, fan-out wall-clock flat in N
  ([benchmarks](benchmarks.md) §1). **CoW-fork density** (the designed sub-second fast tier,
  Phase-1 boundary) remains unmeasured.
- **Dual-channel WebRTC latency** (Phase-1 boundary) — latency target unmeasured.
- **NVENC density / GPU-TEE attestation** (Phase-4) — unmeasured.

## How to read the milestones honestly

- "M0/M1" in commit/PR labels delivered the **Linux/X11 pixel + streaming slice above** — real and
  tested. That is genuine progress and should not be undersold.
- The milestone is now **v0.0.1 — feature-complete local/reference runtime**: all core semantics
  should exist and be tested locally, even if performance, fork density, WebRTC/SFU/NVENC,
  multi-tenant control-plane operation, and cross-substrate scale come later.
- Milestone *labels* previously outran both the **contract** (schema drift, #56 — since closed)
  and the structured-observation evidence (Spike A — since measured, verdict: hybrid). Treat the
  🔵/🔬 sections as the honest v0.0.1 and post-v0.0.1 backlog, not as a reduced product scope.

_See also: [roadmap](roadmap.md), [tech decisions D1–D12](../design/tech-decisions.md),
hardening backlog (#56), a11y-coverage gate (#2)._
