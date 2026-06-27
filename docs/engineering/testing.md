# Testing

Audience: implementers working on v0.0.1.

This page summarizes the current test surface and the v0.0.1 contract tests still needed.

## Merge Test Policy

**GitHub Actions is the authoritative gate for the exact commit being merged.** Local commands are
the contributor fast path and should use the same locked/formatting flags, but do not override a red
or missing remote job. Required checks should be enforced by branch protection.

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
cargo fmt --manifest-path shinkend/Cargo.toml --all -- --check
cargo clippy --locked --manifest-path shinkend/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path shinkend/Cargo.toml --all
```

Python only:

```bash
cd sdk/python
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest -q
```

## Coverage

Line-coverage snapshots live in [`benchmarks/results/coverage.json`](../../benchmarks/results/coverage.json)
(totals + per-module, with commit and date); the headline table and the honest-caveats note are in
[docs/benchmarks/README.md §4b](../benchmarks/README.md). Regenerate with:

```bash
# Rust (shinkend/) — cargo-llvm-cov, LLVM source-based coverage
cargo install cargo-llvm-cov --locked   # one-time; also: rustup component add llvm-tools-preview
cargo llvm-cov --manifest-path shinkend/Cargo.toml --all-targets --summary-only --json \
  --output-path /tmp/shinken-rustcov.json
cargo llvm-cov report --manifest-path shinkend/Cargo.toml   # human-readable per-file table

# Python (sdk/python/) — pytest-cov over the same suite `pytest -q` runs
cd sdk/python
pip install pytest-cov
python -m pytest -q --cov=shinken --cov-report=term --cov-report=json:/tmp/shinken-pycov.json
```

Keep the repo lean: commit only the small `coverage.json` summary, never HTML reports or the
instrumented `target/` output (both are git-ignored via `target/` and `.coverage`). Remember the
caveat when reading the numbers: the live X11/Docker smoke paths run uninstrumented, so backend
modules (`executor.rs`, `pyautogui.rs`, `providers/docker.py`, `smoke.py`, `a11y.py`,
`scorer_proc.py`) under-report.

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

The structured-observation smoke (`scripts/observe_smoke.py`, part of the Docker image job) drives
the guest a11y engine against a real AT-SPI app: it launches `zenity --entry` inside the container,
asserts element ids are stable across two `observe`s, clicks the entry **by element id**
(guest-side `element_ref` resolution), types, asserts the `observe` diff carries the change
(`~ … Value:"…"`), and closes the dialog via `invoke_action` (the AX path).

The CRIU memory-tier smoke (`scripts/criu_smoke.py`, **local-only — needs Docker + privileged
containers + the `shinken/sandbox-linux-criu` image**, not wired into CI) proves the
process-memory claim end-to-end: it plants an in-heap counter inside the checkpointed tree,
checkpoints (`criu dump --leave-stopped` + commit in one consistency window + donor resume), asserts the donor keeps serving AND its
counter keeps beating, restores a replica, and asserts the replica answers with the **same pid,
same nonce, and a counter ≥ the pre-dump reading** — the check no files-only tier can pass —
plus the golden-file inheritance check. Offline unit coverage for the tier is
`sdk/python/tests/test_criu_provider.py`.

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
