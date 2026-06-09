# Eval

Audience: users who want to run tasks and understand Shinken's eval direction.

The full design is D7 in [`../design/tech-decisions.md`](../design/tech-decisions.md). Shinken ships a
tiny local harness, including `run_eval_forked` (golden-checkpoint → fork-N → score over the Docker
disk tier).

## Eval Philosophy

Eval is thin orchestration on the same runtime. A task run should produce:

- A setup state.
- A goal/instruction.
- A run through ACI.
- A programmatic verifier result.
- A summary with pass/fail, steps, and wall-clock time.

Future capture/export features can add richer evidence and training-data artifacts after the runtime
semantics are solid.

## What The Harness Includes

The tiny harness provides:

- Deterministic local GUI task fixtures.
- Programmatic (non-vacuous) verifiers.
- N-run execution, including golden→fork-N→score (`run_eval_forked`) over the Docker disk tier.
- A summary report.

This is enough to prove semantics without a cloud eval service.

## Later Scope

Later versions add:

- OSWorld-Verified and other conformance suites at scale.
- The CRIU memory + sub-ms CoW fork fast tiers under forked replicas.
- `pass@k`, `pass^k`, confidence intervals.
- Hosted eval service and dashboards.

For the OSWorld bring-up path, see the engineering plan:
[`../engineering/osworld-eval.md`](../engineering/osworld-eval.md). The first milestone is one real
OSWorld task in the official image with Shinken injected as the action/observation layer; the second
is the upstream small manifest.

## Metrics To Prefer

Do not report only success rate. Include:

- Pass/fail and verifier evidence.
- Step count.
- Wall-clock time.
- Cost or sandbox time.
- Observation mode used.
- Capability events.

These metrics make scores auditable and comparable.
