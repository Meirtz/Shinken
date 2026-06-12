# Contributing to Shinken

Thanks for helping build Shinken — the open infrastructure stack for computer-use agents: an
AI-native, cross-platform sandbox runtime + control plane + control panel. This guide is short on
purpose.

## Project shape

- **`docs/`** — user docs, design canon, and engineering plans (read
  [`docs/README.md`](docs/README.md); decisions are numbered **D1–D15** in
  [`docs/design/tech-decisions.md`](docs/design/tech-decisions.md)).
- **`notes/`** — working research & deep dives.
- Code: `schema/` (the ACI JSON Schema, `aci.schema.json` — the wire source of truth),
  `shinkend/` (Rust Guest Runtime), `sdk/python/` + `sdk/typescript/` (SDKs + Operator +
  adapters; the eval harness ships in-SDK as `shinken.eval` / `run_eval_forked`),
  `images/linux/` (Sandbox image), `examples/` (runnable interop examples, see
  [`examples/README.md`](examples/README.md)), `benchmarks/` (rerunnable local suites),
  `spikes/` (de-risking experiments).
- Release history: [`CHANGELOG.md`](CHANGELOG.md).
- See [`CLAUDE.md`](CLAUDE.md) for the conventions and naming an AI pair-programmer should follow.

## The one hard rule: this is a public, vendor-neutral project

Never commit confidential or company-internal material. Public vendor *product* facts (e.g.
NVENC, NICE DCV, vGPU/MIG, GPU-TEE) are fine when cited from public docs; internal platform
names, internal URLs, and confidential markers are not. CI enforces this
(`scripts/check-no-internal.sh`). Private design references belong only in the git-ignored
`internal/` directory.

## Workflow

1. **Open or claim an issue** describing the change (use the templates; current work is tracked
   under the **v0.0.1 — feature-complete local/reference runtime** milestone).
2. **Branch** off `main`: `feat/…`, `fix/…`, `chore/…`, `docs/…`, `spike/…`.
3. **Commit** with [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
   `docs:`, `chore:`, `test:`, `refactor:`). Keep commits focused.
4. **Open a PR** (fill the template; link the issue with `Closes #N`). Keep PRs small and reviewable.
5. **CI must be green** and the PR reviewed before merge. Squash-merge preferred.
6. When a design decision changes, update the relevant **ADR** in `docs/design/tech-decisions.md` in
   the same PR.

## Local dev (as code lands)

- Rust: `cd shinkend && cargo fmt && cargo clippy --all-targets -- -D warnings && cargo test`
- Python: `cd sdk/python && pip install -e . && ruff check . && pytest -q`
- Schemas: `python3 -c "import json,glob;[json.load(open(f)) for f in glob.glob('schema/**/*.json',recursive=True)]"`
- Guard: `bash scripts/check-no-internal.sh`

## Reviews

Aim for: correctness, a clean and *elegant* interface (the API is the product), tests for new
behavior, and docs/ADRs kept in sync. Be kind and specific.
