# Milestone Triage

Audience: maintainers managing GitHub issues and PRs.

Current milestone: `v0.0.1 — feature-complete local/reference runtime`.

## Triage Rule

An issue belongs in v0.0.1 if it is required for **core CUA semantics at local/reference scale**:

- ACI contract.
- Agent-native dialect or provider adapter.
- GUI act/observe.
- Screenshot/focused/region/screencast/a11y/CDP/element-ref reference paths.
- `.skn` replay/data.
- Capability envelope or permission events.
- File/artifact transfer semantics.
- Tiny eval/verifier evidence.
- Contract tests and release gates.

An issue belongs after v0.0.1 if it mainly optimizes:

- Fork density.
- WebRTC/SFU/NVENC production streaming.
- Multi-tenant Control Plane.
- Full Cedar + ocap + OS enforcement.
- Windows/macOS/Android production tiers.
- GPU/TEE.
- Large-scale eval service.

## Priority Order Inside v0.0.1

1. **Contract safety:** schema/runtime/SDK drift, action schemas, replay validation.
2. **Act/observe completeness:** backends, screenshot, screencast, a11y/CDP, `element_ref`.
3. **Agent interoperability:** dialect parser, Anthropic/OpenAI adapters.
4. **Replay/data completeness:** action-observation pairing, capability events, artifact refs.
5. **Capability semantics:** envelope, local gateway shim, permission events, privacy controls.
6. **Eval proof:** deterministic tasks, verifier receipts, N-run summaries.
7. **Docs/release gates:** user docs, engineering docs, checklist, CI contract matrix.

## PR Review Checklist

For every v0.0.1 PR, ask:

- Does this change add or alter ACI or `.skn` contract? If yes, are fixtures updated?
- Does Rust, Python, and JSON Schema agree?
- Does a user-facing behavior need docs in `docs/user/`?
- Does a design decision need an ADR update?
- Does this accidentally implement a later performance/scale system before the reference semantics
  are stable?
- Does it introduce any tracked internal/private reference? CI should catch this, but reviewers
  should check too.

## Issue Hygiene

- Broad epics should link concrete implementation issues.
- Design issues should say whether implementation is part of v0.0.1 or later.
- Bugs that affect contract correctness should be prioritized above new features.
- If a PR closes part of an issue, update the issue body or leave a follow-up issue rather than
  leaving stale acceptance criteria.
