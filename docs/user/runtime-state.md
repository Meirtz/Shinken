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
container-only providers it may degrade to slower snapshot restore, warm-pool swap, clone, or
recreate.

### Resume

Resume continues a paused or suspended Sandbox/Session. It is not the same as replay. A resumed
Sandbox has live OS state. A replayed `.skn` shows what happened and may feed a fork/restore
operation, but by itself it does not make the old desktop live again.

### Prepare expensive setup once, in the golden checkpoint

Per-episode environment hygiene that an unsnapshotted runner is forced to repeat every reset — remounting shared memory for heavy browsers, applying enterprise/update-popup policy, masking auto-update timers, settling the desktop, installing fixtures — is exactly the cost runtime state removes. Bake that setup into the sandbox **once**, take a golden checkpoint, then `fork` per episode: every replica inherits the prepared state instantly instead of re-running minutes of setup-and-settle on a cold boot. This is the concrete mechanism behind the fork-vs-cold-boot economics (see [tech-decisions.md](../design/tech-decisions.md) D1/D7).

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

## Current Status

The **Docker disk tier is implemented** (#209): `provider.snapshot()` (`docker commit`) /
`restore()` / `resume()` / `fork()` (snapshot + restore, disk copy-on-write) / `checkpoint()`.
The SDK exposes provider-managed `sandbox.checkpoint()`, `sandbox.fork()`, and `sandbox.resume()`.

**Not yet built:** the **CRIU memory** checkpoint tier (`snapshot_kind="process"`) and the
**sub-second CoW fork-from-snapshot fast tier** (Firecracker/QEMU, Phase-1, gated on a first-party
latency spike). See
[`../engineering/status.md`](../engineering/status.md).
