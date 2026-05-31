# Testing

Audience: implementers working on v0.0.1.

This page summarizes the current test surface and the v0.0.1 contract tests still needed.

## Pre-Public Test Policy

Until Shinken is ready to go public, **the required gate is local CI-equivalent testing**.
GitHub Actions may still exist in the repository as a future/public mirror, but it is not the
authoritative merge gate while the project is pre-public. A PR is considered test-ready when the
maintainer ran the relevant local commands below and included the command list/results in the PR
description.

After public launch, the same checks should run remotely again on every PR. The local commands stay
as the contributor fast path and as the fallback when remote CI is unavailable.

## Local Gate Commands

From the repository root:

```bash
make lint
make test
make guard
```

Local sandbox-provider baseline:

```bash
make sandbox-image
make sandbox-bench
```

Rust only:

```bash
cargo fmt --manifest-path shinkend/Cargo.toml -- --check
cargo clippy --manifest-path shinkend/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path shinkend/Cargo.toml --all
```

Python only:

```bash
cd sdk/python
pip install -e ".[dev]"
ruff check .
pytest -q
```

## Current CI Jobs

These jobs describe the **remote equivalent** of the local gate. They are not the pre-public source
of truth; run the matching local commands instead.

- Internal/confidential-content guard.
- Schema JSON parse sanity.
- Rust format, clippy, and tests.
- Python SDK lint and tests.
- Linux Xvfb integration smoke.
- SDK wheel install smoke.
- Docker sandbox image smoke.
- Docker provider lifecycle smoke.

## Current Live Smokes

The live Linux integration smoke starts Xvfb, runs `shinkend`, drives pointer actions, verifies cursor
position with `xdotool`, and optionally runs screencast/window capture smokes.

The provider lifecycle smoke runs the same Docker image through `DockerLocalProvider` and records a
single-sandbox baseline with create/readiness/connect/action/screencast timings. Larger `N=2/4`
provider benchmarks are local/manual until the fleet layer exists.

## v0.0.1 Contract Tests Needed

The v0.0.1 release gate should add tests for:

- ACI verb-specific positive and negative fixtures.
- Rust/Python/schema agreement on every wire shape.
- Agent-native dialect parser fixtures.
- Anthropic/OpenAI adapter fixtures.
- Ordered action batch results.
- Screencast/window observation schema validation.
- Bounded stream queues and slow-consumer behavior.
- AT-SPI/CDP observation normalization.
- `element_ref` resolution and stale-ref errors.
- Capability envelope and permission events.
- File/artifact transfer with checksum mismatch.
- Deterministic GUI task fixtures.
- Tiny eval verifier receipts and N-run summary.

## Test Principle

Every feature that becomes part of ACI, capability checks, runtime state, or eval evidence needs a fixture
that prevents drift across schema, Rust, Python, and docs. Performance tests can come later; contract
tests are v0.0.1-critical.
