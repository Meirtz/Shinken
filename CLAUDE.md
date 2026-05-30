# CLAUDE.md — guide for AI coding sessions in this repo

Shinken is an **AI-native, cross-platform sandbox runtime + control plane + control panel for
computer-use agents** (a streaming-first successor to OSWorld). See [README.md](README.md) and
[`docs/`](docs/README.md). Currently **design phase — no runtime code yet.**

## ⛔ The one hard rule: this is a PUBLIC open-source project

This is a **public, vendor-neutral OSS project** (despite the `~/dev/` path). Anything
committed is world-readable.

- **NEVER** put confidential or company-internal material in tracked files (`docs/`, `notes/`,
  `README`, code). No internal platform names, no internal links, nothing marked confidential.
- Internal/private design references live ONLY in `internal/` (git-ignored). Do not reference
  `internal/` or `scratch/` from any tracked file, and do not link to them.
- **Public** vendor product facts (e.g. NVENC, NICE DCV, vGPU/MIG, GPU-TEE) ARE fine in docs when
  cited from public sources — the project stays vendor-neutral and runs on any cloud.
- Do not run internal-only tooling (e.g. company intranet search) for this project.

## Layout

| Path | Tracked? | What |
|------|----------|------|
| `docs/` | ✅ | 10 authoritative docs: vision, PRD, architecture, OSWorld teardown, landscape, ADRs (D1–D12), roadmap, glossary, threat model, economics |
| `notes/` | ✅ | 9 working notes: per-domain deep dives, open questions, sources |
| `README.md`, `LICENSE` (Apache-2.0) | ✅ | front matter |
| `references/` | 🚫 git-ignored | 9 cloned prior-art repos studied for design (OSWorld, cua, codex, anthropic-quickstarts, neko, OpenAdapt, e2b-desktop, UI-TARS-desktop, OmniParser); provenance + re-clone in `references/README.md` (tracked) |
| `scratch/` | 🚫 git-ignored | working research: `_canon.md` (the design single-source-of-truth) + 62 structured research findings + digests |
| `internal/` | 🚫 git-ignored | non-confidential private design references; never publish |

## Conventions

- **The design canon is `scratch/_canon.md`** — decisions are numbered **D1–D12**. When changing a
  design decision, update the relevant ADR in `docs/05-tech-decisions.md` AND the canon, and keep
  the two consistent. Other docs reconcile to D-numbers.
- Naming (use consistently): **Shinken** (platform), **Sandbox** / **Session**, **Guest Runtime**
  (`shinkend`), **ACI** (Agent-Computer Interface), **Operator**, **Control Plane** / **Control
  Panel**, **Substrate/Provider**, **Capability** / **Permission Panel**, **`.skn`** (replay
  bundle), the control/event/media **planes**. See [docs/07-glossary.md](docs/07-glossary.md).
- Docs are **self-contained**: cite external sources by URL and sibling docs by relative path; do
  not cite working filenames or link to `scratch/`.
- Mark unverified vendor numbers `(vendor-published, unverified)`.

## Status & next steps

Design corpus complete; pre-implementation. Per [docs/06-roadmap.md](docs/06-roadmap.md), the
immediate work is **de-risking spikes** before Phase-0 code:
1. **a11y-coverage spike** — measure what fraction of real apps (browser, Electron, Qt, canvas,
   games) expose usable accessibility trees + the bandwidth of a tree diff. This is the
   load-bearing assumption behind the structured-first thesis (D3).
2. **CoW-fork density spike** — real concurrent-guests-per-host via snapshot fork (D1).
3. **Dual-channel WebRTC latency PoC** — structured data channel + on-demand NVENC video (D4).

Open questions and risks: [notes/open-questions.md](notes/open-questions.md).
