# Concepts

Audience: users and contributors who need the product vocabulary without reading the full design
canon.

For the canonical glossary, see [`../design/glossary.md`](../design/glossary.md).

## Shinken

Shinken is the open infrastructure stack for computer-use agents. It aims to provide one substrate
for production agent deployment, evaluation, and trajectory-data capture.

v0.0.1 implements the complete semantics at local/reference scale. The D15 backend waist already
connects Linux, macOS, Windows, and browser surfaces through one ACI, with backend-specific proof
depth. Later versions optimize the same semantics for performance, fork density, WebRTC streaming,
the multi-tenant control plane, and managed first-party native cross-OS production tiers.

## Sandbox

A Sandbox is one capability-described computer surface behind the ACI. Today that can be a
Shinken-managed Linux/X11 desktop, the native macOS v1, or a Linux/macOS/Windows/browser surface
reached through a D15 backend. A Session is a live attach/run against a Sandbox. Native Windows,
Wayland, and full macOS AX engines remain separate first-party implementation gaps.

## Guest Runtime

`shinkend` is the daemon inside the Sandbox. It executes validated ACI actions, captures
observations, and emits events. It should not make tenant policy decisions; the Action Gateway or
local v0 shim owns capability decisions.

## ACI

The Agent-Computer Interface is the typed protocol every agent uses to drive a Sandbox.

Actions are canonical Shinken actions, not provider-specific strings. Adapters translate Anthropic,
OpenAI, UI-TARS, OSWorld, and Shinken-native dialects into ACI.

## Observation

Observation is layered:

- Screenshot baseline: works everywhere.
- Structured fast paths: AT-SPI, CDP, UIA, AX, and `element_ref` where available.
- Generated structure: Set-of-Marks / OmniParser for low-a11y surfaces.
- Region/zoom and video: pixel escalation for humans or hard visual surfaces.

v0.0.1 must implement reference screenshot and structured paths. Later versions optimize defaults
based on measured coverage.

## Runtime State

Runtime state is the live, branchable computer state behind a Sandbox — Shinken's headline
primitive. The key terms are:

- **Snapshot**: substrate-captured state such as disk, memory, or device state.
- **Checkpoint**: Shinken restore point that links substrate snapshot(s), event offset, and optional
  agent state.
- **Fork**: create a new Sandbox/run branch from a checkpoint — one golden state into N live
  replicas.
- **Resume**: continue a paused/suspended Sandbox or Session.

These are the primitives behind instant reset, N-run eval replicas, counterfactual / best-of-N
branches, and long-running or idle-suspended tasks. See [`runtime-state.md`](runtime-state.md).
Docker disk-tier checkpoint/fork/resume is a reference-tier implementation; the memory/fast tiers
and the control plane are designed.

## `.skn`

`.skn` is a replay/data-bundle concept that was **intentionally deferred** from v0.0.1 (the runtime
replay surface was removed in #216); it is not implemented in the current runtime or SDK. When it
returns, it should remain separate from runtime state: a timeline artifact can describe what
happened, but it is not itself a VM snapshot and cannot make an old desktop live again.

## Capabilities

Capabilities are sandbox powers such as network egress, filesystem scope, credentials, clipboard,
GPU, persistence, privileged install, and OS automation. v0.0.1 has a local gateway shim and
capability envelope; later versions add full Cedar + ocap + OS enforcement.

## Eval

Eval is not a separate world. Shinken evals run on the same runtime. v0.0.1 includes a tiny local
verifier harness; later versions add conformance suites, forked replicas, pass@k/pass^k, and hosted
eval services.
