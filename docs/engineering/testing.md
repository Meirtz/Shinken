# Testing

Audience: implementers working on v0.0.1.

This page summarizes the current test surface and the v0.0.1 contract tests still needed.

## Current Local Commands

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

The GitHub workflow currently runs:

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
- Ordered action batch replay events.
- Screencast/window observation schema validation.
- Bounded stream queues and slow-consumer behavior.
- AT-SPI/CDP observation normalization.
- `element_ref` resolution and stale-ref errors.
- `.skn` action-observation pairing.
- Atomic `.skn` writes and bundle validation.
- Capability envelope and permission events.
- File/artifact transfer with checksum mismatch.
- Deterministic GUI task fixtures.
- Tiny eval verifier receipts and N-run summary.

## Test Principle

Every feature that becomes part of ACI, `.skn`, capability events, or eval evidence needs a fixture
that prevents drift across schema, Rust, Python, and docs. Performance tests can come later; contract
tests are v0.0.1-critical.
