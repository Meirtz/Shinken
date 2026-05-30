# v0.0.1 Release Checklist

Audience: maintainers preparing the first feature-complete local/reference release.

v0.0.1 is complete when Shinken's core semantics work locally and are covered by tests. It does not
need production-scale performance, multi-tenant control plane, fork density, or WebRTC/SFU/NVENC.

## Required Capabilities

- [ ] ACI v0 schema is strict enough for all implemented verbs and observations.
- [ ] Rust and Python protocol fixtures agree with the JSON Schema.
- [ ] Agent-native action dialect parser exists and has conformance fixtures.
- [ ] At least one provider adapter path is implemented and fixture-tested.
- [ ] Pointer, keyboard, screenshot, focused/region capture, and screencast work locally.
- [ ] AT-SPI/CDP structured observation reference paths exist.
- [ ] `element_ref` resolution exists with stale/missing-ref errors.
- [ ] `.skn` records paired actions and observations.
- [ ] `.skn` writes are atomic and validator-tested.
- [ ] Capability envelope is recorded in every run.
- [ ] Permission events can be recorded and replayed.
- [ ] File/artifact transfer exists with checksums and replay refs.
- [ ] Deterministic GUI task fixtures exist.
- [ ] Tiny eval harness emits verifier receipts and N-run summaries.
- [ ] Replay privacy/redaction controls exist at least at configuration/metadata level.

## Required Docs

- [ ] Root README describes current status honestly.
- [ ] `docs/user/` describes runnable behavior.
- [ ] `docs/design/` points to the design canon.
- [ ] `docs/engineering/` points to current implementation plan and testing.
- [ ] `docs/engineering/status.md` matches code reality.
- [ ] `docs/engineering/v0.0.1-plan.md` matches GitHub milestone.
- [ ] No tracked doc links to `internal/`, `scratch/`, or private sources.

## Required CI / Testing

- [ ] Guard job passes.
- [ ] Schema sanity and contract tests pass.
- [ ] Rust format, clippy, and tests pass.
- [ ] Python lint and tests pass.
- [ ] Wheel install smoke passes.
- [ ] Live Xvfb integration passes.
- [ ] Docker sandbox image smoke passes.
- [ ] v0.0.1 contract test matrix passes.

## Not Required For v0.0.1

- Production Control Plane.
- Warm pools.
- CoW fork density.
- Runtime checkpoint/restore/fork.
- WebRTC/SFU/NVENC production media path.
- Full Cedar + ocap + OS enforcement.
- Windows/macOS guests.
- Android.
- GPU tier.
- Multi-tenant deployment.

These are later performance, scale, substrate, and production-hardening milestones.
