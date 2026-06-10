# CLAUDE.md — guide for AI coding sessions in this repo

Shinken is an **AI-native, cross-platform sandbox runtime + control plane + control panel for
computer-use agents** (a streaming-first successor to OSWorld). See [README.md](README.md) and
[`docs/`](docs/README.md). The design corpus is complete; the **implementation is a proven
Linux/X11 vertical slice** — handshake/auth, pointer+keyboard actions, pixel observation
(screenshot + real-time screencast + bandwidth levers + focused-window capture),
Docker disk-tier checkpoint/fork/resume + `run_eval_forked`, a local capability-gateway shim,
and a Python SDK, all under live CI. The **structured/a11y thesis (D3), production permission
enforcement, `.skn` recording/playback, the control plane, and cross-platform are designed-only
and not yet built**, and the load-bearing **a11y-coverage spike (#2) is still ungated**.
**[`docs/engineering/status.md`](docs/engineering/status.md) is the authoritative built-vs-designed map — read it before
trusting present-tense claims in the vision docs. This file's status summary must track
status.md; reconcile both when either changes.**

## ⛔ The one hard rule: this is a PUBLIC open-source project

This is a **public, vendor-neutral OSS project** (despite the `~/dev/` path). Anything
committed is world-readable.

- **NEVER** put confidential or company-internal material in tracked files (`docs/`, `notes/`,
  `README`, code). No internal platform names, no internal links, nothing marked confidential.
- Internal/private design references must stay out of tracked files. Do not link to private working
  areas or use them as public documentation sources.
- **Public** vendor product facts (e.g. NVENC, NICE DCV, vGPU/MIG, GPU-TEE) ARE fine in docs when
  cited from public sources — the project stays vendor-neutral and runs on any cloud.
- Do not run internal-only tooling (e.g. company intranet search) for this project.

## Layout

| Path | Tracked? | What |
|------|----------|------|
| `docs/` | ✅ | Authoritative docs: vision, PRD, architecture, OSWorld teardown, landscape, ADRs (D1–D12), roadmap, glossary, isolation & capability note, economics, Phase-0 plan, ACI spec |
| `notes/` | ✅ | 9 working notes: per-domain deep dives, open questions, sources |
| `README.md`, `LICENSE` (Apache-2.0) | ✅ | front matter |
| `schema/` | ✅ | ACI JSON Schema (`aci.schema.json`) |
| `shinkend/` | ✅ | Rust Guest Runtime (`shinkend`) |
| `sdk/python/` | ✅ | Python SDK and CLI |
| `images/linux/` | ✅ | Local Linux Sandbox image |
| `benchmarks/` | ✅ | Rerunnable local benchmark suites + raw result JSONs; figures land in `docs/engineering/assets/benchmarks/`, narrative in `docs/engineering/benchmarks.md` |
| `references/` | 🚫 git-ignored | 12 cloned prior-art repos studied for design (OSWorld, cua, codex, anthropic-quickstarts, neko, OpenAdapt, e2b-desktop, UI-TARS-desktop, OmniParser; + 2026-06: uni-agent, CUA-Gym, Agentix); provenance + re-clone in `references/README.md` (tracked) |

## Conventions

- **The public design canon is `docs/design/tech-decisions.md`** — decisions are numbered **D1–D12**.
  When changing a design decision, update the relevant ADR and reconcile sibling docs to the same
  D-number.
- Naming (use consistently): **Shinken** (platform), **Sandbox** / **Session**, **Guest Runtime**
  (`shinkend`), **ACI** (Agent-Computer Interface), **Operator**, **Control Plane** / **Control
  Panel**, **Substrate/Provider**, **Capability** / **Capability Manager**, **`.skn`** (replay
  bundle), the control/event/media **planes**. See [docs/design/glossary.md](docs/design/glossary.md).
- Docs are **self-contained**: cite external sources by URL and sibling docs by relative path; do
  not cite private working filenames.
- Mark unverified vendor numbers `(vendor-published, unverified)`.

## Status & next steps

**Built & proven (Linux/X11):** M0 transport/auth + M1 act-and-observe are done — pointer+keyboard
actions, screenshot, real-time screencast (server-push) with idle-suppression + downscale, and
focused-window/`window:<id>` capture, all with live Xvfb/Docker CI smokes. **Docker disk-tier
checkpoint/fork/resume** (`docker commit`, #209) behind the provider interface, plus
`eval.run_eval_forked` (golden→fork-N→score, #231), are built; a **local capability-gateway shim**
(`sdk/python/src/shinken/gateway.py` + tests) is built. The Python SDK (sync facade + reader/demux)
ships too. `.skn` recording is **not** built (removed/deferred, #216/#217). Full built-vs-designed
map: **[docs/engineering/status.md](docs/engineering/status.md)**.

The immediate work (per the recalibrated priorities):
1. **#56 hardening is DONE** — the schema alignment (screencast verbs, `scope`/`fps`/`max_long_edge`,
   `stream`/`seq`, `hello.token`), the frame-queue bounds, the vacuous-test fixes, the **error
   taxonomy** (`sandbox_died` with exit/signal detail, typed per-action status), the
   **screencast reconnect semantics** (`resume_stream`: stream identity + seq continuity), the
   **trajectory-level `exit_reason`** (documented precedence, `shinken/runtime/trajectory.py`),
   and **subprocess scorer isolation** (T-5, `shinken/scorer_proc.py`) are all built — see
   [docs/engineering/v0.0.1-plan.md](docs/engineering/v0.0.1-plan.md) §6.
2. **a11y-coverage spike — STILL UNGATED (#2)** — measure what fraction of real apps (browser,
   Electron, Qt, canvas, games) expose usable accessibility trees + the bandwidth of a tree diff.
   This gates the structured-default fast path and the bandwidth/cost claims (D3); the screenshot
   baseline still carries v0.0.1 usability.
3. **Designed-only, not started:** the Capability Manager (production enforcement beyond the local
   gateway shim), `.skn` recording/playback, the memory (CRIU) + sub-ms CoW fork fast tiers,
   control plane + concurrency, dual-channel WebRTC/NVENC, cross-platform (mac/Win) + **Wayland**.
4. **CoW-fork density** and **dual-channel WebRTC latency** remain Phase-1 boundary spikes (D1/D4).

The **2026-06 recalibration change inventory** (positioning / architecture / functionality / contract /
testing / docs — what changed, why, status, and the still-open list) is
[docs/engineering/recalibration-2026-06.md](docs/engineering/recalibration-2026-06.md).

Open questions and risks: [notes/open-questions.md](notes/open-questions.md).
