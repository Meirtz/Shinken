# Eval

Audience: users who want to run tasks and understand Shinken's eval direction.

The full design is D7 in [`../design/tech-decisions.md`](../design/tech-decisions.md). v0.0.1 starts with a
tiny local harness.

## Eval Philosophy

Eval is thin orchestration on the same runtime. A task run should produce:

- A setup state.
- A goal/instruction.
- A run through ACI.
- A `.skn` replay bundle.
- A programmatic verifier result.
- A summary with pass/fail, steps, wall-clock, and replay path.

The same replay used for debugging should be usable as eval evidence and training trajectory data.

## v0.0.1 Scope

v0.0.1 should include:

- 3-5 deterministic local GUI task fixtures.
- Programmatic verifiers.
- N-run sequential execution.
- Verifier receipts linked to `.skn`.
- A summary report.

This is enough to prove semantics without a cloud eval service.

## Later Scope

Later versions add:

- OSWorld-Verified and other conformance suites.
- Golden snapshots per task.
- CoW-forked replicas.
- `pass@k`, `pass^k`, confidence intervals.
- Hosted eval service and dashboards.

## Metrics To Prefer

Do not report only success rate. Include:

- Pass/fail and verifier evidence.
- Step count.
- Wall-clock time.
- Cost or sandbox time.
- Replay size.
- Observation mode used.
- Capability events.

These metrics make scores auditable and comparable.
