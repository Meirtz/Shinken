# Changelog

Notable changes to Shinken. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
entries are coarse-grained on purpose — the authoritative built-vs-designed map is
[docs/engineering/status.md](docs/engineering/status.md), and every measured number lives in
[docs/benchmarks/README.md](docs/benchmarks/README.md).

## [Unreleased]

### Added

- **22-verb ACI surface**: gesture verbs (`drag`, `mouse_down`/`mouse_up`), the typed in-guest
  `exec` channel (argv default + `shell` opt-in, buffered or streamed, gateway-audited), the
  desktop verbs (`clipboard_get`/`clipboard_set`, `launch_app`, `activate_window`),
  `list_windows`, and act-returns-observation (per-action `observe`).
- **Guest structured-observation engine v1 (Linux/AT-SPI)**: `observe` with stable element ids,
  tree-text diff, settle; guest-side `element_ref` actions + `invoke_action`/`set_value`.
- **Runtime-state tiers beyond the disk tier**: the **CRIU memory tier** (`CriuDockerProvider`,
  privileged-only — atomic stopped-window checkpoint, with post-hardening live revalidation
  pending), plus push-based boot readiness (`provider.create()` ~0.2 s). The historical live
  warm-pool graft is disabled because restore equivalence was not proven.
- **Agent-runtime narrow waist** (`shinken.runtime`) + Workload/Provider registries, the
  pluggable `shinkend` injector, OSWorld as a Workload (single-task official-evaluator score
  1.0 — a functional gate, not a conformance sweep), and `eval.run_eval_forked`
  (golden → fork-N → score).
- **Fork-native gym adapter** (`shinken.gym`: `reset()` = fork) and trainer interop adapters —
  swerex/uni-agent, CUA-Gym, Agentix, ProRL-Agent-Server — plus the NeMo Gym resources server
  example with a local group-relative optimizer-step smoke (`examples/nemo_gym/`).
- **Operation-layer backends (D15, `shinken.backends`)**: `cua`, `mcp-computer`,
  `browser-runtime`, `e2b` adapters under the typed ACI with honest capability negotiation, and
  `RoutedSession` CU↔BU composition; fixture-tested + env-gated live smokes (the browser backend
  proven against real headless Chrome; the e2b/cua/mcp live gates are written but unrun).
- **Observation pointer metadata**: pointer position (`pointer: [x, y]`) on one-shot screenshots.
- **Bandwidth + scale levers, all first-party-measured**: JPEG observation codec, dirty-tile
  delta screencast, binary WS media frames, XDamage event-driven capture, fleet-level frame
  dedup, pipelined `step()`, and `SharedLoop` — client plane measured to **3,096 live sessions
  on one event-loop thread** ([docs/benchmarks/README.md](docs/benchmarks/README.md)).
- **14 rerunnable local benchmark suites** with tracked raw results and the headline report.
- **macOS engine v1**: native CoreGraphics/CGEvent capture+input backend in `shinkend`
  (`--backend macos`), local-only proof — no mac CI, AX observation designed-only.
- **a11y-coverage spike measured (E5)**: verdict = hybrid per-window structured + pixel
  fallback; D3 stays Provisional.
- **#56 hardening complete**: typed failure taxonomy, screencast reconnect (`resume_stream`),
  trajectory-level `exit_reason`, subprocess scorer isolation.

### Changed

- **Session-ownership API (v2, #265)**: the public session lifecycle moved to the
  provider-owned `provider.session()` shape; consumers no longer manage raw connect/close
  pairs.
- Narrative and docs re-weighted: runtime state (checkpoint/fork/resume) is the headline
  differentiator; the capability/permission model is framed as a runtime entitlement note.

### Removed

- The runtime replay (`.skn`) recording surface — deferred to later design work (#216/#217);
  replay remains designed-only.

## [v0.0.1-alpha.20260531] - 2026-05-31

### Added

- Provider **runtime-state interface** (checkpoint/restore/resume/fork descriptors, #207) and
  the **Docker reference implementation on the disk tier** (`docker commit`-based
  snapshot/restore/resume/fork/checkpoint, #209); `sandbox.checkpoint()` exposed in the SDK.

### Changed

- Docs re-weighted: runtime state promoted to the differentiator, replay demoted to a
  supporting evidence ledger (#208/#210).

## [v0.0.1-alpha] - 2026-05-31

First tagged alpha: the proven **Linux/X11 vertical slice** plus the design corpus.

### Added

- ACI v0 JSON Schema, `shinkend` (Rust Guest Runtime) handshake-first transport with dev-token
  auth, and honest capability negotiation.
- Pointer + keyboard actions (X11/XTEST), screenshot capture, real-time screencast with
  idle-suppression and downscale, focused-window/region capture.
- Python SDK (sync facade, reader/demux, wheel packaging) and the TypeScript control-surface
  workspace (ACI client, CDP semantic adapter, state model, replay viewer, TUI console).
- OSWorld `DesktopEnv` compat shim; Anthropic/OpenAI Computer Use adapters; the action dialect
  parser.
- SDK-local structured observation (AT-SPI + CDP backends, `element_ref` resolution, tree-diff)
  and the a11y coverage harness (Spike A, #2).
- Tiny eval harness with deterministic task fixtures; the local Action Gateway capability shim
  with an ask/approval tier; file/artifact transfer.
- `.skn` replay recording, scrubber, and redaction controls (since removed — see Unreleased).
- Docker Linux sandbox image and live CI (Xvfb + Docker smokes); a live Operator smoke driving
  the stack end to end with an off-the-shelf vision model.

[Unreleased]: https://github.com/Meirtz/Shinken/compare/v0.0.1-alpha.20260531...HEAD
[v0.0.1-alpha.20260531]: https://github.com/Meirtz/Shinken/compare/v0.0.1-alpha...v0.0.1-alpha.20260531
[v0.0.1-alpha]: https://github.com/Meirtz/Shinken/releases/tag/v0.0.1-alpha
