# Shinken Docs

The docs are being split into three audiences so user-facing guidance, design canon, and active
engineering plans do not fight each other.

## Entrypoints

| Section | Audience | Role |
|---|---|---|
| [`user/`](user/README.md) | Users, contributors, agent developers | Runnable behavior, quickstarts, concepts, current usage |
| [`design/`](design/README.md) | Maintainers, architecture reviewers | Full CUA infrastructure scope, decisions, tradeoffs, target architecture |
| [`engineering/`](engineering/README.md) | Implementers | v0.0.1 plan, current status, release gates, active milestone alignment |

The numbered `0X-*.md` files (`00-vision.md` … `11-aci-spec.md` plus `STATUS.md`) are now thin
compatibility stubs that preserve old external links; the canonical copies live in `design/` and
`engineering/` per the table below.

## Canonical Files

| # | Doc | What it covers | Status |
|---|-----|----------------|--------|
| 00 | [Vision & positioning](design/vision.md) | What Shinken is, why it exists, who it's for, north-star, non-goals | ✅ |
| 01 | [Product Requirements (PRD)](design/prd.md) | Personas, journeys, functional + non-functional requirements (IDs), scope, KPIs | ✅ |
| 02 | [Architecture](design/architecture.md) | System architecture: control plane + Sandbox/Guest Runtime + Operator + Control Panel; the 3 planes; data flow; substrate matrix | ✅ |
| 03 | [OSWorld teardown](design/osworld-analysis.md) | Deep analysis of OSWorld: how it works, what to keep, what's too primitive (with file:line), mapped to our decisions | ✅ |
| 04 | [Competitive & tech landscape](design/landscape.md) | The 4-camp survey, per-competitor capsules, competitive matrix, per-domain tech options | ✅ |
| 05 | [Technical decisions (ADRs)](design/tech-decisions.md) | One ADR per decision D1–D12: context, decision, alternatives, consequences, evidence | ✅ |
| 06 | [Roadmap](engineering/roadmap.md) | Phased plan: v0.0.1 semantic-complete reference runtime → performance/scale → eval at concurrency → cross-OS → cloud + GPU | ✅ |
| 07 | [Glossary](design/glossary.md) | Shared vocabulary | ✅ |
| 08 | [Isolation & capability note](design/threat-model.md) | Runtime design note: the isolation boundary (D1) and capability scoping (D6), built vs designed | ✅ |
| 09 | [Economics & build-vs-buy](design/economics-and-build-vs-buy.md) | Concurrency/cost model, build-vs-buy (OSS substrates vs in-house), measurement plan | ✅ |
| 10 | [v0.0.1 implementation plan](engineering/v0.0.1-plan.md) | Feature-complete local/reference runtime: ACI, adapters, act/observe, `.skn`, capabilities, artifacts, tiny eval, a11y coverage harness, code layout | ✅ |
| 11 | [ACI specification](design/aci-spec.md) | The north-star interface: elegant API, typed actions, observation, runtime state (checkpoint/fork/resume), the supporting replay (`.skn`) audit ledger, the async harness core + adapters | ✅ |

Legend: ⏳ pending · 🚧 drafting · ✅ drafted

## Source-of-Truth Rules

- **Implementation reality:** [`STATUS.md`](engineering/status.md).
- **Design decisions:** [`05-tech-decisions.md`](design/tech-decisions.md).
- **Current milestone plan:** [`10-phase0-plan.md`](engineering/v0.0.1-plan.md) and GitHub milestone
  `v0.0.1 — feature-complete local/reference runtime`.
- **User-facing runnable behavior:** [`user/`](user/README.md) and root [`README.md`](../README.md).
- **Raw research:** [`../notes/`](../notes/README.md) or ignored local files until distilled.

## Implementation Notes

Operational docs that track the v0.0.1 build:

- [Status](engineering/status.md) — what is implemented vs designed, per subsystem.
- [Benchmark report](benchmarks/README.md) — every headline number with provenance and
  evidence-class labels; methodology in [engineering/benchmarks.md](engineering/benchmarks.md).
- [Release checklist](engineering/release-checklist.md) — the v0.0.1 contract checks that must pass to ship.
- [Testing](engineering/testing.md) — local and CI test surfaces.
- [Milestone triage](engineering/milestone-triage.md) — how to classify v0.0.1 vs later work.

Working research and raw teardowns live in [`../notes/`](../notes/README.md).
