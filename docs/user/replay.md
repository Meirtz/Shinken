# Replay And `.skn`

Audience: users, eval authors, and training-data users.

The canonical design decision is D5 in [`../design/tech-decisions.md`](../design/tech-decisions.md).
Current implementation status is in [`../engineering/status.md`](../engineering/status.md). For
snapshot/checkpoint/fork/resume, see [`runtime-state.md`](runtime-state.md).

## What `.skn` Is

`.skn` is the Shinken replay and trajectory bundle. It answers: **what happened?** It is intended to
carry:

- Manifest and version metadata.
- Append-only `events.jsonl`.
- Action events.
- Observation events.
- Media references.
- Artifact references.
- Capability and permission events.
- Verifier receipts.
- Future snapshot/checkpoint references.

The event stream is the replay ledger.

## Current Status

Replay is a **future feature**. The runtime and SDK do not currently ship `.skn` recording,
playback, or a `shinken replay` CLI. This page is retained as design context so the future
implementation can stay aligned with the runtime-state model.

## v0.0.1 Requirements

v0.0.1 should add:

- Action-observation pairing.
- Atomic bundle writes.
- Bundle validation.
- CLI step/scrub mode.
- Capability envelope and permission events.
- Artifact refs.
- Verifier receipts.
- Replay privacy/redaction metadata.

## Replay Is Not Runtime State

Saving a `.skn` replay does **not** restore the runtime state. Runtime state has its own concepts:

- **Snapshot**: substrate state such as disk, memory, or device state.
- **Checkpoint**: a Shinken restore point linking snapshot(s), event offset, and optional agent state.
- **Fork**: create a new Sandbox/run branch from a checkpoint.
- **Resume**: continue a paused or suspended Sandbox/Session.

`.skn` may reference checkpoint ids later, but it is not itself a VM snapshot and does not make an old
desktop live again.

## Relationship To Checkpoints

When runtime state is implemented, `.skn` should include `checkpoint_ref` or `snapshot_ref` events at
meaningful boundaries: task start, step boundaries, capability changes, verifier milestones, and
manual markers. The replay lets a user find the point; checkpoint/fork/resume make that point live.

## Privacy

Replay bundles are sensitive. Screenshots, DOM/a11y values, artifacts, file paths, and permission
events can contain private data. **No redaction API exists today** — the recording surface itself is
deferred (see Current Status). These are design-future requirements for when recording returns; do
not call any redaction parameter yet.

When the recording surface returns, it should support a redaction posture chosen at connect time,
covering at least:

- A **no-redaction** mode keeping full text + media bytes.
- A **text-redaction** mode that strips typed text, key chords, element name/value, permission
  reasons, and verifier evidence while keeping media bytes.
- A **media-redaction** mode that keeps each frame's content identity (dimensions / scope / hash)
  but not the raw pixel bytes.
- A **fully-redacted** mode combining both.

The chosen posture should be recorded in the `.skn` manifest. Whether full redaction becomes the
default when recording is an open question — it needs eval/verifier fidelity handling, since a
verifier inspects recorded content.
