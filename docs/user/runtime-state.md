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

## How `.skn` Fits

`.skn` stores the timeline:

- actions,
- observations,
- permission decisions,
- media refs,
- artifact refs,
- verifier receipts,
- future `checkpoint_ref` / `snapshot_ref` events.

When runtime state is implemented, `.skn` should reference checkpoint ids at meaningful boundaries:
task start, step boundaries, capability grants, side-effecting operations, verifier milestones, and
manual markers.

## Provider Honesty

Every provider should advertise what state operations it truly supports:

- `supports_snapshot`
- `supports_fork`
- `reset_strategy`
- `snapshot_kind`
- `transport`
- `isolation`

If a provider cannot restore or fork, it must say so. v0.0.1 Docker-local reset is recreate, not
fork-from-snapshot.

## Current Status

Today Shinken has `.skn` recording and local provider capability descriptors. Runtime
checkpoint/restore/fork/resume are design targets and provider contracts, not complete production
features. See [`../engineering/status.md`](../engineering/status.md).
