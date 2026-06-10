# Sandbox / Substrate Workstream

Audience: implementers working on Sandbox lifecycle, provider integration, local performance
measurement, and future substrate spikes.

This page is the engineering charter for the Sandbox/Substrate workstream. It turns the target
design in [`../design/architecture.md`](../design/architecture.md), D1/D9/D10 in
[`../design/tech-decisions.md`](../design/tech-decisions.md), and
[`../../notes/sandbox-infra.md`](../../notes/sandbox-infra.md) into a local-first implementation
sequence. The rule is: **measure the local reference path first; expose substrate guarantees
explicitly; do not imply Docker/qcow2 can deliver Firecracker-class fork semantics.**

## Scope

The immediate scope is a low-concurrency local provider layer around today's Linux/X11 slice:

- `DockerLocalProvider` starts the existing `shinken/sandbox-linux` image, waits for `shinkend`,
  records lifecycle timings, and cleans up containers.
- `ExternalProvider` connects to an already-running provider endpoint, including the current
  OSWorld-image provider path.
- Benchmark scripts measure N=1/2/4 local runs before any cloud/fleet claim.
- Checkpoint/fork APIs exist as capability-gated operations. Providers that cannot prove them
  return `unsupported` rather than pretending reset is a fork.

Out of scope for this first pass: Firecracker or QEMU fleet implementation, Kubernetes control
plane, WebRTC/SFU, cross-OS guests, GPU scheduling, and production multi-tenancy.

## Provider Capability Contract

Each provider reports what it can actually do. The scheduler and benchmark layer should consume
these fields instead of inferring behavior from provider names.

| Capability | Meaning | Docker local | External OSWorld provider | Future Firecracker L0 | Future QEMU desktop |
|---|---|---:|---:|---:|---:|
| `supports_lifecycle` | Provider can create/destroy Sandboxes | yes | no | yes | yes |
| `supports_gui` | Guest exposes a graphical desktop | yes | provider-managed | no | yes |
| `supports_snapshot` | Provider can save a resumable runtime state | no | provider-managed | yes | yes, after PoC |
| `supports_fork` | Provider can fork multiple children from one checkpoint | no | provider-managed | yes | yes, after PoC |
| `supports_gpu` | Provider can attach GPU resources | no | provider-managed | no | tier-specific |
| `supports_vsock` | Host/guest transport can use vsock | no | provider-managed | yes | yes |
| `supports_egress_policy` | Provider can enforce network egress scopes | local only | provider-managed | yes | yes |
| `reset_strategy` | What `reset()` means | recreate | provider-managed | fork-from-snapshot | fork/recreate by tier |

`DockerLocalProvider` is useful for development, compatibility, and low-concurrency baselines. It
is not an isolation boundary for multi-tenant workloads, and it is not a checkpoint/fork tier.

## Local Benchmark Contract

The first useful output is a measurement table for the current provider/image:

- `create_ms`: provider request to ready handle return.
- `ready_ms`: follow-up `shinkend` health check after create.
- `connect_ms`: WebSocket connect + ACI handshake.
- `screenshot_ms`: one screenshot round trip.
- `click_ms`: one pointer action round trip.
- `screencast_first_frame_ms`: start screencast to first frame.
- `screencast_bytes`: bytes received during the sample.
- `replay_bytes`: size of recorded `.skn`, if recording is enabled.
- `rss_bytes`: provider-reported resident memory when available.
- `destroy_ms`: provider cleanup time.

Run N=1/2/4 locally before investing in new substrate work. CI should keep a cheap N=1 smoke only.

## Readiness And Cleanup

Provider readiness is stricter than "the process started":

1. The endpoint accepts a WebSocket.
2. ACI `hello` returns `welcome`.
3. `ping()` and `screenshot()` succeed.
4. The screenshot is a valid PNG and, when a provider can check it cheaply, not an all-black frame.

Every provider-created resource must carry a Shinken label or identifier so a local reaper can clean
up orphans after failed benchmarks.

## Deferred Checkpoint/Fork Work

Checkpoint/fork is deliberately deferred until a Linux substrate spike proves it:

- Linux headless: Firecracker snapshot restore using `MAP_PRIVATE` memory, `userfaultfd`, and a
  post-fork uniqueness hook.
- Linux desktop: QEMU-microvm/crosvm proof of concept with X11 or virtio-gpu/software render.
- gVisor/Kata: checkpoint/restore and background restore for non-GUI/code workloads.
- Windows/macOS/GPU: warm-pool or provider-managed reset; no Firecracker-class fast fork claim.

Until a provider proves snapshot/fork in code and tests, its capability descriptor must set those
fields to false.
