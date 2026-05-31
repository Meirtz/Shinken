# Shinken User Docs

Audience: users, contributors, and agent developers who want to understand what Shinken is and how
to run what exists today.

This section is intentionally conservative: user docs should describe **released or runnable**
behavior first, then link to design docs for target architecture.

## Start Here

- Current implementation truth: [`../engineering/status.md`](../engineering/status.md)
- Project overview and quickstart: [`../../README.md`](../../README.md)
- Quickstart: [`quickstart.md`](quickstart.md)
- Concepts: [`concepts.md`](concepts.md)
- ACI surface and SDK target: [`../design/aci-spec.md`](../design/aci-spec.md)
- ACI user guide: [`aci.md`](aci.md)
- Runtime state (checkpoint/fork/resume): [`runtime-state.md`](runtime-state.md)
- Replay and `.skn` (audit/data ledger): [`replay.md`](replay.md)
- Capabilities: [`capabilities.md`](capabilities.md)
- Eval: [`eval.md`](eval.md)
- v0.0.1 implementation plan: [`../engineering/v0.0.1-plan.md`](../engineering/v0.0.1-plan.md)

## What Belongs Here

Additional user docs can be extracted here over time:

- Installation and packaging docs.
- SDK API reference.
- Adapter examples.
- Troubleshooting.

Design ambitions belong in [`../design/`](../design/). Engineering plans and implementation status
belong in [`../engineering/`](../engineering/).
