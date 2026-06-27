# v0.0.1 release gate

The contract that must hold before a `v0.0.1` (and `v0.0.1-alpha`) release is cut.
**Scope is correctness and complete product semantics at local/reference scale.**
Performance, fork density, WebRTC/SFU/NVENC, multi-tenant scale, and cross-substrate
breadth are explicitly **out of scope for v0.0.1** — they are later milestones.

> Authoritative built-vs-designed status: [`status.md`](status.md). This page is the
> *gate*; `status.md` is the *map*.

## Merge gate

GitHub Actions is the authoritative merge/release gate. Required jobs must be green for the exact
commit being merged; a local pass does not override a red or missing remote check. Repository branch
protection should enforce those required jobs rather than relying on maintainer convention.

The local core preflight uses the same locked Rust, formatting, lint, and test flags as CI. A PR that
touches code should run the affected subset plus:

```bash
make guard
make lint
make test
```

For runtime/provider changes, also run the relevant live smokes locally:

```bash
make sandbox-image
make sandbox-bench
```

## Remote gate

All contract jobs must be green on GitHub Actions:

| Job | Enforces |
|-----|----------|
| **v0.0.1 contract gate** (`contract`) | `tests/test_contract.py` — ACI wire vocab, verifier receipts, and packaged-vs-repo schema parity all validate; **fails on schema/runtime drift** |
| No internal/confidential content (`guard`) | no private identifiers/links/secrets in tracked files |
| Schema sanity (`schema`) | all JSON Schemas parse |
| shinkend Rust (`shinkend`) | `cargo fmt --check` + `clippy -D warnings` + `cargo test` |
| SDK Python (`sdk-python`) | `ruff check` + `ruff format --check` + full `pytest` |
| SDK wheel install (`sdk-wheel`) | wheel builds, installs in a clean venv, validates schema **outside the repo**; packaged schemas in sync |
| Linux integration (`integration-linux`) | live Xvfb: real X11 action, screencast frames, downscale, focused-window capture |
| Docker sandbox image (`docker`) | image builds; **act → observe** over the wire (handshake + screenshot off the in-container desktop) |

## Manual / reference checklist

Core semantics that must be demonstrable (most are covered by the jobs above):

- [ ] **ACI v0**: handshake + honest capability negotiation; typed actions; unknown verbs/fields rejected.
- [ ] **Act + observe (Linux/X11)**: pointer/keyboard; screenshot; real-time screencast (idle-suppression + downscale); focused-window/`window:<id>` capture.
- [ ] **Runtime state**: Docker disk-tier checkpoint/fork/resume is exposed and documented; unsupported providers report unsupported operations honestly.
- [ ] **Runtime boundary**: every TCP listener requires token authentication; browser `Origin` is deny-by-default with an exact allowlist; privileged in-guest `exec` is default-off and omitted from advertised capabilities unless explicitly enabled.
- [ ] **Policy seam**: local Action-Gateway capability checks deny ungranted actions before dispatch when enabled. This broader SDK capability gateway remains a **client-side reference shim** (#84/#161); server-side auth/origin/exec gates are enforced, while full per-action Cedar policy (D6) remains post-v0.0.1.
- [ ] **Eval**: tiny harness with verifier receipts + N-run summary; OSWorld-compatible interface + first-party smoke.
- [ ] **Adapters/agents**: at least one provider-neutral smoke-agent path (skips cleanly without credentials).
- [ ] **Docs honest**: README/roadmap reconciled with `status.md`; designed-only features not claimed as built.

## Explicitly NOT required for v0.0.1

Performance/latency targets, CoW-fork density, dual-channel WebRTC/SFU/NVENC, GPU-TEE,
multi-tenant control plane, and Shinken-owned native Windows/Wayland plus managed macOS fleet
tiers. Cross-platform D15 backends are already built; these native/fleet tiers are gated on
their own spikes and later milestones; the a11y-coverage spike (#2) likewise gates how
fast structured observation becomes the default fast path.
