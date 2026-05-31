# Quickstart

Audience: users and contributors who want to run the current local Shinken slice.

This page describes what works **today**. For the full v0.0.1 target, see
[`../engineering/v0.0.1-plan.md`](../engineering/v0.0.1-plan.md) once it is split out, or the
current canonical plan at [`../engineering/v0.0.1-plan.md`](../engineering/v0.0.1-plan.md).

## What You Can Run Today

Current implementation: Linux/X11 reference slice.

- `shinkend` WebSocket Guest Runtime.
- Python SDK and CLI.
- ACI v0 handshake and capability negotiation.
- Pointer actions, keyboard actions, screenshots.
- Screencast and focused-window/region capture.
- Docker disk-tier checkpoint/fork/resume through the provider API.
- Docker Linux sandbox image smoke test.

Not implemented yet: provider adapters, a11y trees, `element_ref`, file/artifact transfer,
capability enforcement, tiny eval, checkpoint/fork, cloud control plane.

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
- Replay: [`replay.md`](replay.md)
- Capabilities: [`capabilities.md`](capabilities.md)
- Eval: [`eval.md`](eval.md)
- Implementation status: [`../engineering/status.md`](../engineering/status.md)
