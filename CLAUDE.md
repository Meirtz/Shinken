# CLAUDE.md — guide for AI coding sessions in this repo

Shinken is an **AI-native, cross-platform sandbox runtime + control plane + control panel for
computer-use agents** (a streaming-first successor to OSWorld). See [README.md](README.md) and
[`docs/`](docs/README.md). The design corpus is complete and M0 implementation has started:
`shinkend` and the Python SDK currently prove the ACI v0 handshake; action execution, observation,
replay, permissions, and eval are Phase-0 follow-up work.

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

Design corpus complete; M0 scaffold in progress. Per [docs/10-phase0-plan.md](docs/10-phase0-plan.md),
the immediate work is:
1. **Harden M0** — align advertised capabilities with implemented behavior, secure the local
   `shinkend` transport defaults, and keep README/docs consistent with actual status.
2. **a11y-coverage spike** — measure what fraction of real apps (browser, Electron, Qt, canvas,
   games) expose usable accessibility trees + the bandwidth of a tree diff. This is the
   load-bearing assumption behind the structured-first thesis (D3).
3. **M1 act + observe** — implement the first real GUI action and observation loop inside the
   local Linux Sandbox.
4. **CoW-fork density** and **dual-channel WebRTC latency** remain Phase-1 boundary spikes (D1/D4).

Open questions and risks: [notes/open-questions.md](notes/open-questions.md).
