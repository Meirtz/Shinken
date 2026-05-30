# Replay And `.skn`

Audience: users, eval authors, and training-data users.

The canonical design decision is D5 in [`../design/tech-decisions.md`](../design/tech-decisions.md). Current
implementation status is in [`../engineering/status.md`](../engineering/status.md).

## What `.skn` Is

`.skn` is the Shinken replay and trajectory bundle. It is intended to carry:

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

## What Works Today

Current code can record a minimal ZIP bundle with:

- `manifest.json`
- `events.jsonl`
- content-addressed screenshot media

The CLI can print a timeline summary:

```bash
shinken replay demo.skn
```

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

## Replay Is Not Checkpoint

Saving a `.skn` replay does **not** restore the runtime state. Future APIs such as `checkpoint`,
`restore`, and `fork` refer to environment/agent state and substrate snapshots. `.skn` may reference
checkpoint ids later, but it is not itself a VM snapshot.

## Privacy

Replay bundles are sensitive. Screenshots, DOM/a11y values, artifacts, file paths, and permission
events can contain private data. v0.0.1 should support redaction metadata and configuration to
disable or redact media capture for sensitive runs.
