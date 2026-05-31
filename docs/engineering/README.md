# Shinken Engineering Docs

Audience: implementers working on the current milestone.

This section tracks **what is built, what v0.0.1 must still implement, and how release correctness is
verified**. Until Shinken goes public, correctness is verified by local CI-equivalent commands rather
than GitHub Actions; remote CI is a future/public mirror of the same gate. This section should stay
aligned with the GitHub milestone and issues.

## Current Engineering Sources

- Implementation reality check: [`status.md`](status.md)
- v0.0.1 implementation plan: [`v0.0.1-plan.md`](v0.0.1-plan.md)
- Roadmap and milestone sequencing: [`roadmap.md`](roadmap.md)
- ACI specification used by implementation: [`../design/aci-spec.md`](../design/aci-spec.md)
- Sandbox/substrate workstream: [`sandbox-workstream.md`](sandbox-workstream.md)
- OSWorld evaluation bring-up: [`osworld-eval.md`](osworld-eval.md)
- Testing guide: [`testing.md`](testing.md)
- Release checklist: [`release-checklist.md`](release-checklist.md)
- Milestone triage rules: [`milestone-triage.md`](milestone-triage.md)
- Spike A — a11y/structured-observation coverage report: [`spike-a11y-coverage.md`](spike-a11y-coverage.md)
- Spike — macOS sandbox automation readiness (entitlements + TCC): [`spike-macos-readiness.md`](spike-macos-readiness.md)

## v0.0.1 Meaning

v0.0.1 is **feature-complete at local/reference scale**. It should implement and test all core
Shinken semantics:

- ACI v0 and agent-native dialect/adapters.
- Real GUI act/observe.
- Screenshot, focused/region capture, screencast, AT-SPI/CDP, and `element_ref` reference paths.
- Runtime-state checkpoint/fork/resume semantics at the local/provider tier.
- Capability envelope and local gateway shim.
- File/artifact transfer.
- Deterministic GUI task fixtures and tiny eval harness.
- Contract tests across schema, Rust, Python SDK, adapters, runtime state, capabilities, and eval.

Later releases optimize performance, fork density, WebRTC/SFU/NVENC, multi-tenant control-plane
operation, and cross-substrate/cross-OS production fidelity.

## Compatibility Stubs

The old root-level paths (`../STATUS.md`, `../10-phase0-plan.md`, `../06-roadmap.md`) remain as
compatibility stubs for external links.
