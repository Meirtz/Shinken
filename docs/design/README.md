# Shinken Design Canon

Audience: maintainers, contributors, and reviewers who need the full target architecture and the
reasons behind it.

This section is the **design canon**. It is allowed to describe the full Shinken scope: the complete
CUA infrastructure stack across runtime, control plane, control panel, replay/data, capabilities,
eval, substrates, streaming, and future cross-OS/GPU tiers.

## Canonical Design Docs

- Vision and positioning: [`vision.md`](vision.md)
- Product requirements: [`prd.md`](prd.md)
- System architecture: [`architecture.md`](architecture.md)
- Agent runtime (narrow waist + Workload/Provider): [`agent-runtime.md`](agent-runtime.md)
- OSWorld teardown: [`osworld-analysis.md`](osworld-analysis.md)
- Competitive landscape: [`landscape.md`](landscape.md)
- ADRs / technical decisions: [`tech-decisions.md`](tech-decisions.md)
- CLI / code execution boundary: [`code-execution.md`](code-execution.md)
- Threat model: [`threat-model.md`](threat-model.md)
- Economics and build-vs-buy: [`economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md)
- Glossary: [`glossary.md`](glossary.md)

## Rules

- ADRs in [`tech-decisions.md`](tech-decisions.md) are the source of truth for design
  decisions D1-D12.
- Design docs may describe target architecture, but must distinguish target, v0.0.1, and later
  optimized/production tiers when that matters.
- Current implementation claims should link to [`../engineering/status.md`](../engineering/status.md), not be duplicated.
- Raw research belongs in [`../../notes/`](../../notes/README.md) or ignored local files until it is
  distilled into public-safe design text.
