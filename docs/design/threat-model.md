# Isolation & capability boundary — runtime design note

> Status: design note · Reconciles to canon **D1** (isolation substrate), **D6** (capability
> scoping), **D9** (control plane). Sibling docs: [architecture](architecture.md) ·
> [tech-decisions](tech-decisions.md) · [glossary](glossary.md).

This is a **runtime design note**, not a security analysis. Shinken is a sandbox runtime: it
boots isolated guest computers, drives them through the ACI, and manages their runtime state.
This note describes the two runtime boundaries that follow from that — the **isolation
boundary** (where the guest ends) and the **capability boundary** (what a sandbox is granted) —
and how they are implemented today vs designed.

## Isolation boundary

A Sandbox is a guest computer behind a substrate boundary (D1). The agent does whatever it
likes *inside* the guest — that is the point of a sandbox. What leaves the guest is the ACI
event/observation stream and any explicitly transferred artifact; nothing else crosses by
default.

- **Today (reference):** a Docker container per Sandbox; `shinkend` binds the ACI WebSocket,
  requires a token on every TCP listener (including loopback), compares it in constant time,
  rejects browser origins by default, and keeps arbitrary in-guest process spawning (`exec`
  and executable-path `launch_app`) default-off. This is the
  local/reference boundary, not the production isolation tier.
- **Designed (D1):** microVM substrates (Firecracker headless-fork tier, QEMU/crosvm desktop,
  CLH/QEMU+VFIO for GPU/Windows, Apple VZ for macOS), selected by a substrate router. The
  product preference is a small device-model footprint (fewer virtual devices to maintain),
  which is also why the headless fork tier targets Firecracker.

## Capability boundary

A Sandbox is **granted** the runtime resources a task needs and nothing more — network egress,
filesystem scope, clipboard, GPU, credentials, persistence, privileged install, peripheral/OS
automation (D6). This is **resource scoping / entitlement management**, a runtime feature: it
keeps a Sandbox to the resources its task declares, the way a process is given a working
directory and an environment. It is a minor, supporting part of the runtime — not the product's
purpose.

- **Today (reference):** a local capability-gateway shim records the session's granted envelope
  and routes each action allow / ask / deny against it (`sdk/python/src/shinken/gateway.py`).
  This is a client-side reference boundary for the eval/audit surface.
- **Designed (D6):** the granted envelope is resolved server-side in the control plane (D9), so
  an action a Sandbox was not granted is simply not dispatched, and each grant/denial is a
  first-class event in the run record. Credentials, when granted, are brokered rather than
  handed to the guest. The enforcement engine is a control-plane concern, deferred with D9.

## What this note is not

It is not a threat model, adversary analysis, or security certification. Capability scoping and
isolation are runtime features that make the platform predictable and auditable; they are not
the product's purpose. Production deployment hardening (multi-tenant isolation, credential
brokering, egress policy) is designed-only and tracked with D9.
