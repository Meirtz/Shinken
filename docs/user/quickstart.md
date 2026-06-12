# Quickstart

Audience: users and contributors who want to run the current local Shinken slice.

This page describes what works **today**. For the full v0.0.1 target, see
[`../engineering/v0.0.1-plan.md`](../engineering/v0.0.1-plan.md) once it is split out, or the
current canonical plan at [`../engineering/v0.0.1-plan.md`](../engineering/v0.0.1-plan.md).

## What You Can Run Today

Current implementation: Linux/X11 reference slice (plus the local-only macOS capture+input
engine, `--backend macos`). Authoritative built-vs-designed map:
[`../engineering/status.md`](../engineering/status.md).

- `shinkend` WebSocket Guest Runtime — the **22-verb ACI**: pointer/keyboard (incl. `drag`,
  `mouse_down`/`mouse_up`), screenshot + real-time screencast + focused-window capture,
  typed in-guest `exec` (argv/shell, buffered + streamed), `clipboard_get`/`clipboard_set`,
  `launch_app`, `activate_window`, `list_windows`.
- **Structured observation** (Linux/AT-SPI guest engine v1): `observe` with stable element
  ids + tree diffs + settle; `element_ref` targets, `invoke_action`/`set_value`.
- Python SDK and CLI (sync + async, pipelined `step()`), TypeScript control-surface SDK,
  model adapters (Anthropic/OpenAI/Kimi-VL) and XML/dialect action parsing.
- Checkpoint / fork / resume: Docker disk tier, warm-pool graft, and the opt-in
  **CRIU memory tier** (privileged-only) — plus the fork-native gym (`reset()` = fork)
  and `run_eval_forked`.
- Operation-layer **backends** (D15): drive the same ACI over trycua/cua, a codex-style MCP
  desktop server, a CDP browser, or an E2B desktop (`shinken.backends`).
- File transfer (`put_file`/`get_file`) through the provider; local capability-gateway shim.

Not implemented yet: production capability enforcement (the control-plane layer),
`.skn` recording/replay, the cloud control plane, the sub-ms CoW fork fast tier,
Windows/Wayland engines, and the macOS AX observation tier.

## Run The Guest Runtime

From the repository root:

```bash
cargo run --manifest-path shinkend/Cargo.toml
```

By default `shinkend` binds `127.0.0.1:8765`. Binding a non-loopback address requires
`SHINKEND_TOKEN`.

## Install The Python SDK

```bash
cd sdk/python
pip install -e ".[dev]"
```

## Connect

In another shell:

```bash
shinken connect
```

Or from Python:

```python
import shinken

env = shinken.connect()
print(env.platform)
print(env.screen_size())
print(env.capabilities)
env.close()
```

## Drive A Local Desktop

Pointer and keyboard actions work when `shinkend` can reach an X11 display.

```python
import shinken

with shinken.connect() as env:
    env.move(x=300, y=200)
    env.click(x=300, y=200)
    env.type_text("hello from Shinken")
    shot = env.screenshot()
```

## Use The Docker Sandbox Image

Build:

```bash
make sandbox-image
```

The CI workflow demonstrates the current container smoke path: build image, run container with a dev
token, connect, and capture a screenshot.

## Run Tests

```bash
make lint
make test
```

The full CI also runs live Xvfb integration, wheel install, and Docker image smoke jobs.

## Where To Go Next

- Concepts: [`concepts.md`](concepts.md)
- ACI: [`aci.md`](aci.md)
- Runtime state (checkpoint/fork/resume): [`runtime-state.md`](runtime-state.md)
- Replay (future design): [`replay.md`](replay.md)
- Capabilities: [`capabilities.md`](capabilities.md)
- Eval: [`eval.md`](eval.md)
- Implementation status: [`../engineering/status.md`](../engineering/status.md)
