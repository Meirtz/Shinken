# Changelog

All notable changes to Shinken are documented here. The project is in early
development; **[`docs/STATUS.md`](docs/STATUS.md) is the authoritative map of what is
actually built vs designed-only.**

## [0.0.1-alpha] — 2026-05-31

First tagged pre-release. This is an **alpha**: a small, tested **Linux/X11** slice of
the much larger `v0.0.1` "feature-complete reference runtime" design. It is **not**
feature-complete — the structured-first thesis and most of the product semantics are
still designed-only (see `docs/STATUS.md`).

### Added — implemented & proven (Linux/X11; unit-tested + live Xvfb/Docker smokes)
- **ACI v0** handshake + capability negotiation; secure transport (handshake-first
  state machine, dev-token auth; a non-loopback bind requires `SHINKEND_TOKEN`).
- **Actions:** pointer (move / click / double_click / right_click / scroll) and
  keyboard (`type_text` / `key`, keysym + modifier combos) via X11 XTEST.
- **Observation:** screenshot (X11 GetImage → PNG); **real-time screencast**
  (server-pushed frames over the ACI WebSocket) with **bandwidth levers** —
  idle-frame suppression + resolution downscale (`max_long_edge`); and
  **focused-window / region capture** (`scope`: `screen` / `active_window` /
  `window:<id>`).
- **`.skn` replay recording** (append-only `events.jsonl` + content-addressed media).
- **Python SDK:** synchronous facade over an async core, with a reader/demux that
  multiplexes RPC replies and unsolicited server-pushed frames; `env.screencast(...)`.
- **OSWorld `DesktopEnv` compatibility shim**; **Docker** Linux sandbox image.
- **Backend selection** via `SHINKEND_EXECUTOR` (auto / x11_xtest / virtual).
- **CI:** no-internal-content guard, schema sanity, Rust (fmt/clippy/test), SDK
  (ruff/pytest), wheel install, live Xvfb integration, Docker image — green per PR.

### Fixed / hardened
- Aligned `schema/aci.schema.json` with the implemented wire vocabulary (screencast
  verbs, `scope` / `fps` / `max_long_edge`, `stream` / `seq`) and added contract
  tests so the schema and implementation can't drift silently again. (#56, #89)
- Bounded the screencast writer channel (drop frames when the client lags) to remove
  an unbounded-memory / OOM vector under slow or stalled clients. (#56)

### Not in this release — designed-only (see `docs/STATUS.md`)
- Structured / accessibility observation (a11y tree, `element_ref` resolution) — the
  load-bearing differentiator; its coverage spike (#2) has **not** been run yet.
- Permission / capability gate + panel, `.skn` playback, checkpoint / fork, control
  plane + concurrency, dual-channel WebRTC / NVENC streaming, high-throughput file
  transfer, eval/verifier harness, model adapters, and cross-platform (macOS /
  Windows) + Wayland.

[0.0.1-alpha]: https://github.com/Meirtz/Shinken/releases/tag/v0.0.1-alpha
