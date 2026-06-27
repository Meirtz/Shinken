# Runtime State: Snapshot, Checkpoint, Fork, Resume

Audience: users and contributors who need to distinguish replay data from runnable state.

Shinken has two related but different artifact families:

- **Replay / `.skn`**: the evidence ledger for what happened.
- **Runtime state**: the thing that lets a Sandbox stop, resume, restore, or fork.

They are designed to link to each other, but they are not the same object.

## Four Concepts

### Snapshot

A snapshot is substrate state captured at a point in time. Depending on the provider, it may include
disk, memory, device state, or only a subset of those. A Docker recreate is not a snapshot. A
Firecracker memory file plus block snapshot is a snapshot. A macOS APFS clone is a disk-style
snapshot-like primitive, not a Firecracker-class memory fork.

### Checkpoint

A checkpoint is a Shinken-level restore point. It points at one or more substrate snapshots and the
logical event position they correspond to. Later, it may also include the agent-side state needed to
continue a run without replaying the entire history.

Checkpoint = environment state + optional agent state + event offset + metadata.

### Fork

A fork creates a new Sandbox or run branch from a checkpoint. It is the primitive behind:

- instant reset from a golden state,
- N-run eval replicas,
- counterfactual replay branches,
- best-of-N exploration,
- training-data branch generation.

On the Linux fast-fork tier this should use copy-on-write memory/disk. On GPU, Windows, macOS, or
container-only providers it may degrade to slower snapshot restore, clone, or recreate. A provider
must reject a requested fidelity/latency it cannot satisfy rather than silently degrade it.

### Resume

Resume continues a paused or suspended Sandbox/Session. It is not the same as replay. A resumed
Sandbox has live OS state. A replayed `.skn` shows what happened and may feed a fork/restore
operation, but by itself it does not make the old desktop live again.

### Prepare expensive setup once, in the golden checkpoint

Per-episode environment hygiene that an unsnapshotted runner repeats every reset — remounting shared memory, applying policy, masking updates, and installing fixtures — is the cost runtime state removes. Bake persistent setup into the sandbox once, take a checkpoint, then restore per episode. Filesystem tiers restart processes; an already-running GUI/window requires an explicitly requested process-memory tier. This is the concrete mechanism behind the restore-vs-cold-provision economics (see [tech-decisions.md](../design/tech-decisions.md) D1/D7).

## Future Timeline Link

A future timeline/audit format may reference checkpoint ids at meaningful boundaries: task start,
step boundaries, capability grants, side-effecting operations, verifier milestones, and manual
markers. That format is deliberately deferred; the runtime-state API should stand on its own first.

## Provider Honesty

Every provider advertises what state operations it truly supports, in its `ProviderCapabilities`:

- `supports_snapshot`, `supports_fork`, `supports_checkpoint`, `supports_resume`
- `reset_strategy`, `snapshot_kind`, `transport`, `isolation`, `display`, `tier`

If a provider cannot restore or fork, it must say so (the contract tests enforce that a claimed
operation is actually wired, not a raising stub). The **Docker** provider now advertises
`supports_snapshot/fork/checkpoint/resume = True` with `snapshot_kind="disk"`; note its `reset()`
is still `recreate` (a fresh container), distinct from `fork()` (a new branch off a snapshot).
A provider whose substrate must run privileged says so too (`requires_privileged=True` — the CRIU
memory tier below), so routing can treat it as a latency tier rather than an isolation boundary.

## Current Status

The **Docker disk tier is implemented** (#209): `provider.snapshot()` (`docker commit`) /
`restore()` / `resume()` / `fork()` (snapshot + restore, disk copy-on-write) / `checkpoint()`.
The SDK exposes provider-managed `sandbox.checkpoint()`, `sandbox.fork()`, and `sandbox.resume()`.

The **CRIU memory tier is implemented too** (opt-in `shinken.CriuDockerProvider`,
`snapshot_kind="process"`, Linux/Docker only): checkpoint = `criu dump --leave-stopped`,
`docker commit` in the same stopped consistency window, then donor resume; restore = a fresh
**privileged** container + `criu restore`. Replicas carry **live process+memory+filesystem
state** — open apps, mid-task processes, X11 clients, in-heap program state (verified per fork
by an in-memory marker) — with one designed exception: established TCP connections are closed
at dump (`--tcp-close`), so agent WebSocket sessions reconnect via the documented
`resume_stream` semantics. ⚠ Every container on this tier runs `--privileged` (in-container
CRIU needs CAP_SYS_ADMIN): it is a **latency/state-fidelity feature, not an isolation
posture** — the production isolation answer remains the microVM tier (D1/D5). Measured
numbers: [`../engineering/benchmarks.md`](../engineering/benchmarks.md) §1b.

**Not yet built:** the **sub-second CoW fork-from-snapshot fast tier** (Firecracker/QEMU,
Phase-1, gated on a first-party latency spike). See
[`../engineering/status.md`](../engineering/status.md).
