# Shinken — Implementation Status (reality check)

> Date: 2026-05-31 · Scope: what is **actually built and proven** vs **designed-only** vs
> **unvalidated**. The design corpus (vision/PRD/architecture/ADRs/roadmap) intentionally describes
> the full CUA infrastructure stack; the *implementation* is the first well-tested local slice of
> that stack. This page is the honest map between target scope and current code. When in doubt, this
> file describes what exists today; the roadmap describes what v0.0.1 and later releases must add.
>
> Audience: users and implementers · Role: implementation reality check. This is the source of truth
> for what exists today.

The one-line summary: **a proven Linux/X11 pixel-observation + real-time-streaming slice exists
and is covered by live CI; v0.0.1 must still add the rest of the core semantics — adapters,
structured observation reference paths, capability events, artifacts, replay scrub/validation, and
tiny eval — before Shinken is feature-complete at local/reference scale.**

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
| **Focused-window / region capture** (`scope`: `screen` / `active_window` / `window:<id>`) | done | #55 — live `xclock` `window:<id>` → 200×200 |
| Python SDK: sync facade + async core, reader/demux (RPC vs server-push) | done | #51 |
| `.skn` replay **recording** (events.jsonl + content-addressed media, ZIP bundle) | done | #31 |
| OSWorld `DesktopEnv` compatibility shim | done | #32 |
| Wheel packaging (schemas bundled), idempotent close | done | #40 |
| Docker Linux sandbox image | done | #45 — build + token handshake + screenshot in CI |
| CI: 7 jobs (guard, schema, Rust, SDK, wheel, live Xvfb integration, Docker) | done | every PR |

## 🟡 Partial / wired-but-stubbed

| Item | Reality |
|---|---|
| `element_ref` action targets | Present in the wire contract, but resolution **bails** — needs the a11y/observation engine (below) |
| `.skn` **playback** | Only *recording* exists; there is no replay execution/scrubbing engine yet |
| Wire schema vs implementation | The runtime/SDK emit verbs/fields (`start_screencast`, `screencast`, `scope`, `fps`, `max_long_edge`, `stream`/`seq`) that **`schema/aci.schema.json` does not yet validate** — tracked in #56 |
| Hardening | A 22-finding adversarial review (incl. an unbounded-frame-queue OOM vector) is open as #56 |

## 🔵 Designed-only — documented, **not implemented**

These appear in the vision/PRD/architecture/README in present-ish tense, but **no working code exists yet**:

- **Cross-platform**: macOS and Windows tiers (today: Linux only). Even on Linux, capture/input is **X11 only** — **Wayland** (the modern Linux default) is unaddressed and would break XTEST/GetImage.
- **Structured / accessibility observation (ADR D3)** — a11y tree + element-ref resolution + diffing. This is the core differentiator and **does not exist**.
- **Permission / capability panel + enforcement gate** — described as a headline feature; currently docs only.
- **Runtime checkpoint / restore / fork** (and CoW-fork density) — heavily referenced as snapshots; **not implemented**. (See #42 for separating replay from checkpoint semantics.)
- **Control plane + ultra-high-concurrency / multi-tenant orchestration** — a single local `shinkend` is all that exists.
- **Dual-channel WebRTC media plane + GPU/NVENC encode** — today streaming is **base64 PNG over the control WebSocket**, fine for an MVP but not the low-latency/bandwidth story at scale.
- **High-throughput file-transfer path** (#49/#50) — design only.
- **Eval layer + OSWorld-Verified conformance** — the shim exists; the eval loop does not.

## 🔬 Unvalidated load-bearing assumptions (spikes NOT run)

The roadmap names these as the de-risking spikes. **None have been measured.** Until they are, the
architecture's core bets are unproven:

- **Spike A — a11y coverage (#2):** what fraction of real apps expose usable accessibility trees, and
  how big is a tree diff? This does **not** block the screenshot-based v0.0.1 loop, but it gates
  structured-observation defaults and every bandwidth/token-cost claim derived from them.
- **CoW-fork density** (Phase-1 boundary) — fork economics unmeasured.
- **Dual-channel WebRTC latency** (Phase-1 boundary) — latency target unmeasured.
- **NVENC density / GPU-TEE attestation** (Phase-4) — unmeasured.

## How to read the milestones honestly

- "M0/M1" in commit/PR labels delivered the **Linux/X11 pixel + streaming slice above** — real and
  tested. That is genuine progress and should not be undersold.
- The milestone is now **v0.0.1 — feature-complete local/reference runtime**: all core semantics
  should exist and be tested locally, even if performance, fork density, WebRTC/SFU/NVENC,
  multi-tenant control-plane operation, and cross-substrate scale come later.
- Milestone *labels* previously outran both the **contract** (schema drift, #56) and the
  structured-observation evidence (Spike A not run). Treat the 🔵/🔬 sections as the honest v0.0.1
  and post-v0.0.1 backlog, not as a reduced product scope.

_See also: [roadmap](roadmap.md), [tech decisions D1–D12](../design/tech-decisions.md),
hardening backlog (#56), a11y-coverage gate (#2)._
