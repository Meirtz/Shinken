# Shinken docs

Authoritative, relatively-stable design corpus. Read in order; each builds on the last.

| # | Doc | What it covers | Status |
|---|-----|----------------|--------|
| 00 | [Vision & positioning](00-vision.md) | What Shinken is, why it exists, who it's for, north-star, non-goals | ✅ |
| 01 | [Product Requirements (PRD)](01-prd.md) | Personas, journeys, functional + non-functional requirements (IDs), scope, KPIs | ✅ |
| 02 | [Architecture](02-architecture.md) | System architecture: control plane + Sandbox/Guest Runtime + Operator + Control Panel; the 3 planes; data flow; substrate matrix | ✅ |
| 03 | [OSWorld teardown](03-osworld-analysis.md) | Deep analysis of OSWorld: how it works, what to keep, what's too primitive (with file:line), mapped to our decisions | ✅ |
| 04 | [Competitive & tech landscape](04-landscape.md) | The 4-camp survey, per-competitor capsules, competitive matrix, per-domain tech options | ✅ |
| 05 | [Technical decisions (ADRs)](05-tech-decisions.md) | One ADR per decision D1–D12: context, decision, alternatives, consequences, evidence | ✅ |
| 06 | [Roadmap](06-roadmap.md) | Phased plan: local PoC → Linux fork tier → eval layer → cross-OS → cloud scale + GPU | ✅ |
| 07 | [Glossary](07-glossary.md) | Shared vocabulary | ✅ |
| 08 | [Threat model](08-threat-model.md) | Trust boundaries, STRIDE table, 5 kill chains, mitigations mapped to D6 | ✅ |
| 09 | [Economics & build-vs-buy](09-economics-and-build-vs-buy.md) | Concurrency/cost model, build-vs-buy (OSS substrates vs in-house), measurement plan | ✅ |

Legend: ⏳ pending · 🚧 drafting · ✅ drafted

Working research and raw teardowns live in [`../notes/`](../notes/README.md).
