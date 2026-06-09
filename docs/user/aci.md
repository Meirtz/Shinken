# ACI User Guide

Audience: agent developers and adapter authors.

The canonical design spec is [`../design/aci-spec.md`](../design/aci-spec.md). This page explains the user
shape.

## What ACI Is

ACI is the typed contract between an agent/operator and a Shinken Sandbox. It separates:

- Agent-facing dialects and provider grammars.
- Canonical Shinken actions.
- Runtime execution backends.
- Capability checks and runtime-state operations.

Agents should not send arbitrary Python or `pyautogui` strings as the primary control path. Adapter
code parses model output, validates it, normalizes coordinates, checks capabilities, and emits ACI
actions.

## Current Low-Level SDK

The Python SDK exposes direct methods for debugging and tests:

```python
env.click(x=640, y=420)
env.type_text("hello")
env.key("enter")
env.scroll(x=900, y=500, dy=-300)
shot = env.screenshot()
```

These methods are useful, and Shinken also ships agent-native dialects and provider adapters so
off-the-shelf agents can drive Shinken unchanged.

## Canonical Target Kinds

ACI actions target one of:

- `point_px`: raw display pixels.
- `point_norm`: normalized coordinates.
- `element_ref`: a structured observation reference.

Every observation carries coordinate-space metadata so the runtime knows what coordinate frame the
agent saw and acted on.

## Provider Adapters

Shinken ships fixture-tested adapters (no live API yet) for:

- Anthropic Computer Use.
- OpenAI Computer Use.
- Kimi-VL.
- A Shinken-native action dialect.

Adapters expose provider/tool version, coordinate transforms, and image resize assumptions so
callers can preserve that context where needed.

## Batch Actions

Some providers emit ordered action batches. Shinken should model ordered batches even if v0.0.1
executes them serially. Each action still gets an explicit result in the batch response.

## Code Execution

CLI/code execution is a separate capability surface. It belongs behind capability policy, filesystem
scope, egress policy, timeout controls, and future capture redaction. It is not a GUI action backend.
