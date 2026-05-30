# v0.0.1 release gate

The contract that must hold before a `v0.0.1` (and `v0.0.1-alpha`) release is cut.
**Scope is correctness and complete product semantics at local/reference scale.**
Performance, fork density, WebRTC/SFU/NVENC, multi-tenant scale, and cross-substrate
breadth are explicitly **out of scope for v0.0.1** — they are later milestones.

> Authoritative built-vs-designed status: [`STATUS.md`](STATUS.md). This page is the
> *gate*; `STATUS.md` is the *map*.

## Automated gate (CI — every PR)

All seven jobs must be green:

| Job | Enforces |
|-----|----------|
| **v0.0.1 contract gate** (`contract`) | `tests/test_contract.py` — ACI wire vocab, `.skn` manifest+events (capabilities/permission/redaction), verifier receipts, and packaged-vs-repo schema parity all validate; **fails on schema/runtime drift** |
| No internal/confidential content (`guard`) | no private identifiers/links/secrets in tracked files |
| Schema sanity (`schema`) | all JSON Schemas parse |
| shinkend Rust (`shinkend`) | `cargo fmt --check` + `clippy -D warnings` + `cargo test` |
| SDK Python (`sdk-python`) | `ruff` + full `pytest` |
| SDK wheel install (`sdk-wheel`) | wheel builds, installs in a clean venv, validates schema **outside the repo**; packaged schemas in sync |
| Linux integration (`integration-linux`) | live Xvfb: real X11 action, screencast frames, downscale, focused-window capture |
| Docker sandbox image (`docker`) | image builds; **act → observe → replay** over the wire (handshake + screenshot off the in-container desktop) |

## Manual / reference checklist

Core semantics that must be demonstrable (most are covered by the jobs above):

- [ ] **ACI v0**: handshake + honest capability negotiation; typed actions; unknown verbs/fields rejected.
- [ ] **Act + observe (Linux/X11)**: pointer/keyboard; screenshot; real-time screencast (idle-suppression + downscale); focused-window/`window:<id>` capture.
- [ ] **`.skn` replay**: atomic + schema-validated writes; action↔observation pairing; `replay --step`/`--validate`; capability envelope + permission events; privacy redaction.
- [ ] **Policy seam**: local Action-Gateway capability check denies ungranted actions before dispatch and records the decision.
- [ ] **Eval**: tiny harness with verifier receipts + N-run summary; OSWorld-compatible interface + first-party smoke.
- [ ] **Adapters/agents**: at least one provider-neutral smoke-agent path (skips cleanly without credentials).
- [ ] **Docs honest**: README/roadmap reconciled with `STATUS.md`; designed-only features not claimed as built.

## Explicitly NOT required for v0.0.1

Performance/latency targets, CoW-fork density, dual-channel WebRTC/SFU/NVENC, GPU-TEE,
multi-tenant control plane, and cross-OS (Windows/macOS) + Wayland. These are gated on
their own spikes and later milestones; the a11y-coverage spike (#2) likewise gates how
fast structured observation becomes the default fast path.
