# CLAUDE.md — guide for AI coding sessions in this repo

Shinken is an **AI-native, cross-platform sandbox runtime + control plane + control panel for
computer-use agents** (a streaming-first successor to OSWorld). See [README.md](README.md) and
[`docs/`](docs/README.md). The design corpus is complete; the **implementation is a proven
Linux/X11 vertical slice** — handshake/auth, pointer+keyboard actions, pixel observation
(screenshot + real-time screencast + bandwidth levers + focused-window capture), `.skn` recording,
and a Python SDK, all under live CI. The **structured/a11y thesis (D3), permissions, replay
playback, checkpoint/fork, the control plane, and cross-platform are designed-only and not yet
built**, and the load-bearing **a11y-coverage spike (#2) is still ungated**.
**[`docs/STATUS.md`](docs/STATUS.md) is the authoritative built-vs-designed map — read it before
trusting present-tense claims in the vision docs.**

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
| `docs/` | ✅ | Authoritative docs: vision, PRD, architecture, OSWorld teardown, landscape, ADRs (D1–D12), roadmap, glossary, threat model, economics, Phase-0 plan, ACI spec |
| `notes/` | ✅ | 9 working notes: per-domain deep dives, open questions, sources |
| `README.md`, `LICENSE` (Apache-2.0) | ✅ | front matter |
| `schema/` | ✅ | ACI and `.skn` JSON Schemas |
| `shinkend/` | ✅ | Rust Guest Runtime (`shinkend`) |
| `sdk/python/` | ✅ | Python SDK and CLI |
| `images/linux/` | ✅ | Local Linux Sandbox image |
| `references/` | 🚫 git-ignored | 9 cloned prior-art repos studied for design (OSWorld, cua, codex, anthropic-quickstarts, neko, OpenAdapt, e2b-desktop, UI-TARS-desktop, OmniParser); provenance + re-clone in `references/README.md` (tracked) |

## Conventions

- **The public design canon is `docs/05-tech-decisions.md`** — decisions are numbered **D1–D12**.
  When changing a design decision, update the relevant ADR and reconcile sibling docs to the same
  D-number.
- Naming (use consistently): **Shinken** (platform), **Sandbox** / **Session**, **Guest Runtime**
  (`shinkend`), **ACI** (Agent-Computer Interface), **Operator**, **Control Plane** / **Control
  Panel**, **Substrate/Provider**, **Capability** / **Permission Panel**, **`.skn`** (replay
  bundle), the control/event/media **planes**. See [docs/07-glossary.md](docs/07-glossary.md).
- Docs are **self-contained**: cite external sources by URL and sibling docs by relative path; do
  not cite private working filenames.
- Mark unverified vendor numbers `(vendor-published, unverified)`.

## Status & next steps

**Built & proven (Linux/X11):** M0 transport/auth + M1 act-and-observe are done — pointer+keyboard
actions, screenshot, real-time screencast (server-push) with idle-suppression + downscale, and
focused-window/`window:<id>` capture, all with live Xvfb/Docker CI smokes. `.skn` recording and the
Python SDK (sync facade + reader/demux) ship too. Full built-vs-designed map: **[docs/STATUS.md](docs/STATUS.md)**.

The immediate work (per the recalibrated priorities):
1. **Reconcile contract + harden (#56)** — align `schema/aci.schema.json` with the implemented wire
   vocabulary (screencast verbs, `scope`/`fps`/`max_long_edge`, `stream`/`seq`), bound the screencast
   frame queues (a real OOM vector), and fix the vacuous tests. 22 verified review findings.
2. **a11y-coverage spike — STILL UNGATED (#2)** — measure what fraction of real apps (browser,
   Electron, Qt, canvas, games) expose usable accessibility trees + the bandwidth of a tree diff.
   This is *the gate* for the structured-first thesis (D3); the differentiator vs OSWorld rests on it.
3. **Designed-only, not started:** permissions/capability gate, `.skn` playback, checkpoint/fork,
   control plane + concurrency, dual-channel WebRTC/NVENC, cross-platform (mac/Win) + **Wayland**.
4. **CoW-fork density** and **dual-channel WebRTC latency** remain Phase-1 boundary spikes (D1/D4).

Open questions and risks: [notes/open-questions.md](notes/open-questions.md).
