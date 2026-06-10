# Recalibration — 2026-06

> Audience: maintainers and implementers. This is the **change inventory** produced by the June-2026
> recalibration: a full-codebase review (108 verified findings) plus a competitive/landscape resurvey.
> It states, per area, **what changed, why, where, and its status** — so the canon stays internally
> consistent and the next implementer knows what is settled vs proposed vs open.
>
> Sibling sources: built-vs-designed truth in [`status.md`](status.md); milestone plan in
> [`v0.0.1-plan.md`](v0.0.1-plan.md); sequencing in [`roadmap.md`](roadmap.md); decisions in
> [`../design/tech-decisions.md`](../design/tech-decisions.md); competitive landscape in
> [`../design/landscape.md`](../design/landscape.md).

## 0. How to read this

Every item carries a **status**:

- **✅ done** — applied in the working tree (the `review-fixes-2026-06-10` branch), not necessarily
  merged. Verify against the cited file before trusting present-tense claims elsewhere.
- **🟡 proposed** — agreed direction, edit drafted into the relevant doc, implementation not started.
- **🔵 open** — a decision or task still to be made/scheduled; named here so it is not lost.

Priorities: **P0** (the canon is factually wrong / a built feature is broken without it), **P1** (clear
correctness or strategy improvement), **P2** (judgment call / later).

Two hard constraints govern every change below and did **not** move:

1. **Positioning lock.** Runtime state (checkpoint / fork / resume) is *the* differentiator and leads
   everywhere; replay/trajectory export is a supporting ledger and must never co-headline or precede it.
2. **v0.0.1 scope discipline.** The alpha gate is OSWorld + Kimi K2.6 actuation through `shinkend`.
   Recalibration may **re-sequence or sharpen** v0.0.1 and the phases right after it — it may not
   balloon v0.0.1.

---

## 1. Positioning

The field moved between the 2026-05-30 survey and June 2026. The differentiator did not disappear, but
its precise form **narrowed** and is now **time-boxed** — the docs had to stop overclaiming.

| # | Change | Why | Where | Status / Pri |
|---|--------|-----|-------|--------------|
| P‑1 | "No competitor has snapshot/fork" is **retired**; replaced with the precise surviving claim: **harness-integrated, local-first, vendor-neutral fork (`run_eval_forked`)** that nobody ships. | A public CUA platform now ships cloud-only CoW fork (vendor-published, unverified) but does not wire it into its own benchmark; another roadmaps fork with no substrate. The blunt claim was checkably false. | [`../design/landscape.md`](../design/landscape.md) §2.1, §3 matrix | ✅ P0 |
| P‑2 | New **fifth camp** documented: trainer-side agent/rollout orchestrators (RL training stacks, gym/rollout harnesses, task factories). They are sandbox **consumers**, not rivals — each pays cold-boot / fresh-sandbox costs per rollout and lacks GUI + runtime state. | The four-camp framing could not place where computer-use *infrastructure demand* now originates. | [`../design/landscape.md`](../design/landscape.md) §1 amendment + capsules | ✅ P0 |
| P‑3 | D12 first-user wedge reworded from **"replay-as-training-data"** to **"runtime-state wedge"**; trajectory/`.skn` export demoted to a supporting byproduct. | Trajectory traces are now commodity (multiple stacks export training-ready traces); replay can no longer be the wedge even on its own terms, and replay must never lead (positioning lock). | [`../design/tech-decisions.md`](../design/tech-decisions.md) D12 + decision map | ✅ P0 |
| P‑4 | Runtime-state lead annotated as **time-boxed** in the reality-check doc: publish first-party fork-vs-cold-boot numbers while the lead exists. | A named competitor roadmaps fork; the honesty doc must not let a reader infer the lead is permanent or uncontested. | [`status.md`](status.md) runtime-state row | ✅ P1 |
| P‑5 | Interop framed as the landing strategy ("Shinken as the stateful GUI substrate beneath existing trainers"), explicitly over feature-racing eval/RL orchestration. | Rollout orchestration is commoditized; building our own loses, plugging under theirs wins. | [`../design/landscape.md`](../design/landscape.md), [`roadmap.md`](roadmap.md) Phase 2 | ✅ P1 |

**What did NOT change (and why):** the typed JSON ACI + Rust guest runtime remains ahead of every
surveyed stack (the rest are pickle/bash/string waists); server-push screencast with idle suppression
remains unique; the capability/permission design remains unclaimed ground; the `.skn` deferral (#216)
remains correct and is *reinforced* by trajectory export being commodity.

---

## 2. Architecture

Settled architecture (the narrow-waist agent-runtime — semantics-free core + Workload × Runtime ×
Provider registries) was **validated** by every project surveyed and is not up for revision. The changes
are evolutions and named interop seams.

| # | Change | Why | Where | Status / Pri |
|---|--------|-----|-------|--------------|
| A‑1 | **Named reference consumers / interop targets** added to the agent-runtime doc, in priority order: (1) RL trainers via a swerex-protocol shim or a ~300-line deployment backend whose `start()` forks from a golden checkpoint; (2) public verifiable-task bundles as a second eval `TaskSource`; (3) keep the SDK session surface **provider-shaped** so out-of-tree orchestrators integrate trivially. | #223/#224 had no defined integration shape; the cheapest landing path is plugging under stacks that already exist. | [`../design/agent-runtime.md`](../design/agent-runtime.md) §5.1 | ✅ P1 |
| A‑2 | **Token fidelity is an adapter requirement** recorded: when collecting against a token-level inference server, adapters must pass through token ids/masks/logprobs — messages-only records are lossy for RL (retokenization mismatch). `Step` now **reserves** the optional fields (`prompt_token_ids`, `response_token_ids`, `response_mask` 1=model/0=tool, `finish_reason`; all default `None`, populated by no current code path) and `Trajectory.exit_reason` covers `traj_exit_reason`, so the record converts losslessly to verl's `AgentLoopOutput` once the train Workload (#223) lands. | The train lane (#223) is unusable for RL without this; two reference stacks make it explicit. | [`../design/agent-runtime.md`](../design/agent-runtime.md) §5.1, `shinken/runtime/trajectory.py` | ✅ P1 |
| A‑3 | **Registry evolution** recorded: graduate out-of-tree plugins from env-var module lists to `importlib.metadata` entry-point groups (`shinken.providers`, `shinken.workloads`) for *installed public* plugins, keeping env vars as the private/non-installed fallback; adopt conflict-on-duplicate, per-entry import-error isolation, provenance reporting, and in-process `register()` override for tests. | The env-var-only registry has silent last-wins and no load-error isolation — the weakest part of an otherwise settled architecture; entry points are how the ecosystem discovers third-party plugins. | [`../design/agent-runtime.md`](../design/agent-runtime.md) §6 | ✅ P1 |
| A‑4 | **Three-tier dependency split** (host / base / runtime) made a packaging requirement for Workload/Provider plugins: the host orchestrator and the sandbox guest image install disjoint sets. | A Workload carrying a heavy evaluator (an OSWorld evaluator pulls ~50 packages) must not bloat the guest image, and the guest must not pull host-only tooling. | [`../design/agent-runtime.md`](../design/agent-runtime.md) | 🟡 P1 |
| A‑5 | **`interactive` Workload (#224) design note**: adopt chat-mode as the *same* loop with "no tool call ⇒ turn done" (not a parallel loop); for multi-party, study caller-declared session ids + idle-TTL eviction + per-session visual cursors, and co-design with the D6 permission panel (which needs session identity anyway). | #224 is unstarted; recording the pattern now prevents it being built as a divergent second loop. | [`../design/agent-runtime.md`](../design/agent-runtime.md) §8 | ✅ P2 |
| A‑6 | **D5 checkpoint/fork API shape** pinned: a checkpoint ref is a first-class creation input (`fork` = "create from checkpoint ref", one primitive, not parallel checkpoint/restore APIs); lifecycle guards (warn on destroying a sandbox with live checkpoints; refuse/stage deleting a checkpoint with live forks) are part of the contract. | The built Docker disk tier and the future CoW tier had no stated unifying API; the cleanest shipped semantics in the field is snapshot-returns-image. | [`../design/tech-decisions.md`](../design/tech-decisions.md) D5 | ✅ P1 |
| A‑7 | **D2 open sub-decision recorded**: when the code-exec capability is granted, do `exec`/file-transfer use typed wire verbs (`exec`, `put_file`, `get_file`, app-launch) or a sidecar protocol? To be settled in the #56 reconciliation; until then every Workload tunnels them untyped. | No eval/train Workload can run setup/scoring on pointer+keyboard alone; the decision should be explicit, not accidental. The alpha gate is unaffected (OSWorld's own server covers it today). | [`../design/tech-decisions.md`](../design/tech-decisions.md) D2 | 🔵 P1 |

---

## 3. Functionality / implementation

The review fixed real bugs across the Rust runtime, the Python SDK, and the adapters. These are
**applied** on the working branch (49 Rust tests + 365 Python tests green, clippy/ruff clean). Grouped;
see the per-file diffs for specifics.

| # | Area | Change | Status / Pri |
|---|------|--------|--------------|
| F‑1 | Rust `executor.rs` | `key_combo` now honors the keysym shift level (was synthesizing `a`/`1` for `A`/`!`); `point_px` validated for bounds + NaN (was silently saturating to a screen edge); F1–F12 + insert/capslock/numlock/printscreen/menu added to the keysym table (the OSWorld PRESS path needed them); `type_text` made atomic (resolve all keysyms up front); horizontal scroll `dx` implemented; `point_norm` 1.0 maps to the last pixel, not one past. | ✅ P0 |
| F‑2 | Rust `pyautogui.rs` | scroll direction un-inverted and magnitude reconciled to the X11 backend's pixel convention (was opposite direction and ~100× off); horizontal scroll added. | ✅ P0 |
| F‑3 | Rust `main.rs` / `connection.rs` | WebSocket-upgrade deadline added (closes a 64-slot pre-auth squat DoS); per-message write timeout + bounded writer drain (a stalled peer can no longer make `MAX_CONNECTION_LIFETIME` unenforceable); accept loop survives transient errors instead of killing the daemon; dropped-frame no longer updates `last_hash` (fixed permanently-stale screencast); screencast default frame size bounded; one-shot capture/execute moved off the runtime threads via `spawn_blocking`; client-supplied `call_id` no longer interpolated unescaped into JSON; `valid_scope` parses real `u32`. | ✅ P0 |
| F‑4 | Rust `protocol.rs` | `respond()` no longer auto-welcomes a `hello` (removed a latent tokenless-auth footgun) and no longer fabricates a 1280×800 `screen_size`. | ✅ P1 |
| F‑5 | Python `client.py` | a `screenshot` action via `act()`/`act_batch()` no longer spuriously fails (an `observation` reply has no `ok` field — this broke the main Operator path); removed the hidden 30 s ceiling that severed long ops (multi-`wait` batches, docker-commit checkpoints) while the coroutine ran on detached; `ping()` bounded by the RPC timeout + waiter cleanup; a mid-stream disconnect now raises `ConnectionError` (distinct from a clean stream end); handshake auth errors surfaced with the server's reason + a recv timeout; wire-path logging added; dead `_batch_id` removed; `checkpoint(name=…)` threaded through. | ✅ P0 |
| F‑6 | Python `cli.py` | `connect` accepts `--token` (defaults to `$SHK_TOKEN`) and `$SHK_ADDR` — it could not previously reach a token-protected (non-loopback) runtime. | ✅ P1 |
| F‑7 | Python adapters/dialect | scroll units normalized to **pixels** at the adapter boundary (Anthropic wheel-clicks and Kimi clicks were passed as pixel `dy`, collapsing every scroll to one step); Kimi/dialect/OSWorld scrolls now carry a default centre target (the schema + executor require one); Anthropic modifier-click raises instead of silently degrading to a plain click; dialect rejects malformed tags instead of silently dropping them, requires coordinates on pointing verbs, and maps `button` to the right verb; OSWorld quote/semicolon parsing fixed (`write("don't")` was truncated to `don`). | ✅ P0 |
| F‑8 | Python runtime-state | `run_eval_forked` now forks **all replicas from the single golden checkpoint** (was forking the live, drifting base); snapshot images are reclaimed (`delete_snapshot`/`cleanup_snapshots`); `restore()`/`fork()` preserve screen geometry + resource limits (forks were silently booting at default resolution). | ✅ P0 |
| F‑9 | Python `inject.py` | injector `args` shlex-quoted; readiness poll after detached start (injection used to "succeed" even if the binary never started); ssh argument-injection guard. | ✅ P1 |
| F‑10 | Python `artifacts.py` / `cdp.py` / plugin loaders | atomic put/get (copy-to-temp + verify + rename, so the returned hash matches the delivered bytes); `cdp.parse_ax_tree` guards a missing `nodeId`; plugin-load flag set only after all imports succeed (a failed import is retryable). | ✅ P1 |
| F‑11 | **PyAutoGUI as a first-class typed backend (#213)** | The typed backend exists; remaining work is the contract test enumerating the OSWorld key names the shim can emit, and aligning unsupported-character behavior between the X11 and PyAutoGUI backends (X11 errors, PyAutoGUI silently skips). | 🔵 P2 |
| F‑12 | **Native automation + a11y backend ladder (#96)** | Designed-only; gated behind the a11y coverage spike (#2) and D3. No change to scope — listed so it is not mistaken for built. | 🔵 P2 |

---

## 4. Contract & schema

| # | Change | Why | Where | Status / Pri |
|---|--------|-----|-------|--------------|
| C‑1 | Schema `Hello` gained an optional `token` field. | The schema forbade the `token` the Rust runtime requires and the client sends — the authenticated handshake failed validation. Both schema copies stay byte-identical (parity test). A `hello`-with-token case was added to the contract test. | `schema/aci.schema.json` (+ packaged copy), `sdk/python/tests/test_contract.py` | ✅ P0 |
| C‑2 | `ActionBatch` `$def` relabeled as an **SDK-side batching convention** (not a wire message — it is deliberately absent from the top-level `oneOf`). | It looked like a normative wire shape nothing implements; a test validates the SDK batch shape against it, so it is kept but clarified rather than deleted. | `schema/aci.schema.json` | ✅ P2 |
| C‑3 | `aci-spec.md` canonical v0 verb list gained `start_screencast`/`stop_screencast` (was 9, schema has 11); the broken `schema/skn.schema.json` link replaced with prose. | The canonical enumeration drifted from the schema + advertised capabilities; the `.skn` schema does not exist (removed with #216). | [`../design/aci-spec.md`](../design/aci-spec.md) | ✅ P1 |
| C‑4 | **#56 contract reconciliation + hardening** — the screencast wire vocabulary is schema-validated and contract-tested, and the **error taxonomy is now implemented**: `SandboxDied` (exit/signal detail), typed per-action `act_batch` status (`ok\|error\|timeout\|skipped\|sandbox_died`) + `failure_kind`, eval `RunResult.kind` + `infra_failure`, and `provider.check_alive()` upgrading a drop to confirmed sandbox death. **Screencast reconnect is now implemented too**: `start_screencast` + `resume_stream` continues a live logical stream (same `stream` id, `seq` carrying on — the frame gap readable off the first frame; a runtime-side bounded/TTL'd resume registry) or restarts fresh at seq 0 when the state is gone, with `resume_screencast` in the SDK. **The trajectory-level exit reason has now landed too**: `Trajectory.exit_reason` with the documented precedence `sandbox_died > setup_error > agent_error > scorer_error > max_steps > task_complete` (`shinken/runtime/trajectory.py`), set by `rollout` (an exception is recorded as `terminal="aborted"` + classified, never a crashed batch), the OSWorld episode, the `osworld_single` receipt, and eval `RunResult.exit_reason` (the finer projection of `kind`, mapping documented in `eval.py`); `Step` reserves the A‑2 token-fidelity fields (`prompt_token_ids`/`response_token_ids`/`response_mask`/`finish_reason`, default `None`) for #223. | RL/eval consumers must branch infra-death vs task-failure and need a specified reconnect contract for the stream fields the runtime already ships. | `shinken/errors.py`, `eval.py`, `client.py`, `providers/*`, `shinken/runtime/trajectory.py`, `shinkend/src/main.rs`; [`v0.0.1-plan.md`](v0.0.1-plan.md) §6 | ✅ P1 |
| C‑5 | Scroll contract settled: `dx`/`dy` are **pixels**; `dx` (horizontal) is honored by the executor; all producers emit a `target`. | Three producers disagreed on units and one emitted targetless scrolls the executor rejects; the field now means one thing everywhere. | schema + executor + adapters + dialect | ✅ P0 |

---

## 5. Testing / CI / ops / security

| # | Change | Why | Where | Status / Pri |
|---|--------|-----|-------|--------------|
| T‑1 | The in-process mock runtime now **schema-validates inbound traffic and rejects unknown verbs** (was acking any verb `ok=true`); the suite no longer sends a schema-invalid `fps=100`. | SDK-emitted wire drift could not previously fail any test. | `sdk/python/tests/conftest.py` | ✅ P0 |
| T‑2 | Vacuous tests strengthened (async screencast now asserts a frame arrives; `ws_max_size` asserts the cap reaches the websocket; the operator composition verifier reads observed state instead of a hard-coded `True`); the flaky wall-clock pause test replaced with a `sleep` recorder; new CLI + hand-rolled-PNG-decoder coverage (both were at 0%). | The historical "vacuous tests" complaint (#56) was only partly fixed. | `sdk/python/tests/` (365 pass, +20) | ✅ P1 |
| T‑3 | CI fail-open guards inverted to **fail-closed** (a renamed gate file now fails the job instead of skipping green); `cancel-in-progress` disabled on `main`; `cargo --locked`; `ruff format --check` + a Python 3.10/3.13 matrix; schema job upgraded to JSON-Schema metaschema validation; a **live checkpoint→fork→screenshot smoke** added (the headline differentiator had zero live CI). | Every gate could silently self-disable on a rename; the differentiator's only validation was a commit message. | `.github/workflows/ci.yml` | ✅ P1 |
| T‑4 | Docker image pinned to a single Debian release (bookworm) across both build stages and run as a **non-root** user; hardening run-flags documented (`--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`); dependabot gained a `docker` ecosystem. | Floating `rust:1-slim`/`debian:stable-slim` could drift glibc across stages; root maximizes escape blast radius. | `images/linux/Dockerfile`, `.github/dependabot.yml` | ✅ P1 |
| T‑5 | **Infra-vs-task failure split — implemented**: `run_eval`/`run_eval_forked` classify each run (`pass\|fail\|setup\|sandbox_died\|error`), count `infra_errors` separately, and confirm sandbox death via `provider.check_alive()`. **Subprocess scorer isolation — now implemented** (`shinken/scorer_proc.py`): the external evaluator runs in a fresh subprocess (task JSON on stdin via `run_scorer`, or a forked live object via `run_scorer_callable` — the form `DesktopEnv.evaluate` takes), writes its verdict to an atomic result file that is authoritative even over a non-zero/timed-out exit, under a bounded timeout, with a typed `ScorerError` (`crash\|timeout\|garbage`) feeding `exit_reason="scorer_error"`; wired into `osworld-eval` by default, while the in-process reference verifier stays in-process. | A noisy third-party evaluator must not corrupt a score, and an eval flake must not consume infra retries. | `eval.py`, `scorer_proc.py`, `providers/*`; [`v0.0.1-plan.md`](v0.0.1-plan.md) §6 | ✅ P1 |
| T‑6 | **CoW-fork density spike** extended to measure **fork-N vs cold-boot-N amortization** on an OSWorld-class desktop image and a SWE-bench-class image — the wedge's economics number against named public baselines. | "Prove the fork economics with numbers, not architecture." | [`roadmap.md`](roadmap.md) Phase 1 spike | ✅ P1 |
| T‑7 | Security: timing-safe token compare, pre-auth deadline, 16 MiB inbound cap, non-loopback-without-token refusal — all already built and re-verified; remaining is the threat-model boundary② callback-path STRIDE (added) and the production Capability Manager (designed-only). | The agent-to-host boundary is the load-bearing one for a sandbox running untrusted actions. | `shinkend/`, [`../design/threat-model.md`](../design/threat-model.md) | ✅ P1 |
| T‑8 | **Main-branch protection (#52)** still open (unavailable without GitHub Pro / public repo); mitigated by `cancel-in-progress != main`. | `main` landed red once (#227→#228); nothing forces CI green before merge. | repo settings | 🔵 P1 |

---

## 6. Documentation & process / hygiene

| # | Change | Why | Where | Status / Pri |
|---|--------|-----|-------|--------------|
| D‑1 | The **#216 (replay removal) + #206 (checkpoint/fork built)** events swept through every doc that still said the opposite — `CLAUDE.md`, `v0.0.1-plan.md`, `roadmap.md`, `vision.md`, `replay.md`, `status.md`, `aci-spec.md`, user docs. `CLAUDE.md` had it backwards in both directions (`.skn` "ships", checkpoint/fork "not started"). | The canon contradicted its own authoritative status map. | many docs | ✅ P0 |
| D‑2 | **Spike-A report overclaim** corrected: a single GTK dialog measurement is no longer called "the evidence D3 needed"; `pct_addressable` and the multi-app sweep are named as still-unmeasured; D3's structured-default upgrade stays Provisional and spike #2 stays ungated. | The one dishonest spot in an otherwise self-aware design corpus. | [`spike-a11y-coverage.md`](spike-a11y-coverage.md) | ✅ P0 |
| D‑3 | **D3 status split** to "Accepted (screenshot baseline) / Provisional (structured-default upgrade, gated on spike #2)"; D4 risk list gained the D3-coverage dependency. | The most load-bearing spike-gated decision was mislabeled "Accepted" while a lesser one was correctly "Provisional". | [`../design/tech-decisions.md`](../design/tech-decisions.md) D3/D4 | ✅ P1 |
| D‑4 | Economics doc fixed: §5/§6 pointer rot, the numpy 27 MB↔1.75 MB self-contradiction, the structured-"default" framing softened to "target default, gated", and a per-step-screenshot-polling **incumbent** baseline added (the headline only compared against a 24×7-video strawman). | The doc's credibility device is "no number leaves unverified"; these were checkable errors. | [`../design/economics-and-build-vs-buy.md`](../design/economics-and-build-vs-buy.md) | ✅ P1 |
| D‑5 | **Milestone-triage** `.skn`-in-v0.0.1 criterion removed (it is deferred); runtime-state added as the in-scope criterion; `.skn` moved to the after-v0.0.1 list. | The triage rule still gated on a removed feature. | [`milestone-triage.md`](milestone-triage.md) | ✅ P1 |
| D‑6 | **Hygiene / hard-rule**: four tracked files' Owner-line email headers changed to role-based owners; machine-local file-URI links rewritten to relative/GitHub URLs; the no-internal guard extended (file-URI links, macOS home-directory paths, Owner-line emails, the anchored private-canon pattern) and its self-test strengthened; `references/README.md` provenance completed; the TypeScript SDK marked `private`; a stray `.coverage` artifact un-tracked + gitignored. | This is a public vendor-neutral repo; these were leaks of personal/corp metadata and tracked junk. | several | ✅ P0 |
| D‑7 | **Golden-image preparation** documented as the concrete mechanism behind fork economics: bake per-episode hygiene (shm remount, update-popup policy, fixtures, settle) into the checkpoint **once**, fork per episode. | Turns the fork differentiator from architecture-talk into a usage pattern. | [`../user/runtime-state.md`](../user/runtime-state.md) | 🟡 P2 |
| D‑8 | **Parity discipline** added to the OSWorld eval bring-up: emit a parity warning on any deviation from upstream OSWorld defaults; keep a behavior-alignment ledger; vendor the official evaluator and run its getters unmodified. | Eval scores are only defensible if comparable to the official harness. | [`osworld-eval.md`](osworld-eval.md) | 🟡 P1 |
| D‑9 | `CLAUDE.md` naming fixed ("Permission Panel" → "Capability / Capability Manager", the glossary term) + a maintenance rule that its status must track `status.md`; `docs/README.md` stub note corrected; `quickstart.md` gained the runtime-state link; `notes/` headers reframed to runtime-state-first. | Naming drift + stale navigation. | several | ✅ P2 |

---

## 7. The open list (decisions / tasks still to make)

The 🔵/🟡 items above, plus the implementation follow-ups of ✅-recorded design notes (A‑1, A‑3 — the
doc edits are done, the code is not) and two standing items with no inventory row of their own
(spike #2; the D8 MCP facade) — collected so none is lost:

1. **#56 — CLOSED** (C‑4) — the `SandboxDied` class, per-action `act_batch` status, eval
   `kind`/`infra_failure`, the `resume_stream` reconnect contract (stream identity + seq
   continuity, gap detectability), and the trajectory-level `exit_reason` (documented precedence
   `sandbox_died > setup_error > agent_error > scorer_error > max_steps > task_complete`,
   `shinken/runtime/trajectory.py`) have all shipped. *Done.*
2. **D2 exec/file wire shape — SETTLED** (A‑7) — recorded in [tech-decisions](../design/tech-decisions.md)
   D2: `exec`/file-transfer are NOT ACI wire verbs in v0.0.1 (they flow over the substrate's own
   channel; SDK `put_file`/`get_file` stay substrate-side); typed wire verbs deferred post-v0.0.1.
   *Done.*
3. **Three-tier dependency split** (A‑4) — implement the host/base/runtime buckets in the Workload/Provider
   packaging before a heavy-evaluator Workload lands. *P1.*
4. **Registry → entry points** (A‑3) — graduate out-of-tree discovery post-v0.0.1. *P1.*
5. **Infra/eval split + subprocess scorer isolation DONE** (T‑5) — the failure split +
   `provider.check_alive()` shipped earlier; `shinken/scorer_proc.py` (fresh subprocess, atomic
   result file authoritative over exit code/timeout, typed `ScorerError`) now isolates the external
   `osworld-eval` evaluator by default; the in-process reference verifier stays in-process. *Done.*
6. **Interop deliverables** (A‑1, Phase 2) — the swerex shim, the HTTP gym facade, and CUA-Gym bundle
   support are the lowest-risk way to land first users; sequence them after the alpha gate. *P1.*
7. **Main-branch protection** (#52, T‑8) — enable required status checks the moment the repo goes public. *P1.*
8. **a11y coverage spike #2** — still the single load-bearing ungated assumption; run the multi-app sweep
   with the sharpened hypothesis (hybrid-per-window for acting + guest-state probe for verifying) and
   record `pct_addressable`. *P0 to unblock D3/D4.* (Separately **settled 2026-06**: `element_ref`
   resolution is SDK-side for v0.0.1; guest-side resolution is post-v0.0.1/#96 — see D3.)
9. **#213 / #96** — PyAutoGUI contract test + native-automation/a11y backend ladder. *P2.*
10. **MCP facade (D8)** — a thin `shinken mcp` wrapper is a re-sequencing candidate to pull to the
    Phase-1 boundary (never the hot loop); not a commitment. *P2.*

> Anything not listed here is either built and verified (§3–§5) or unchanged settled design. When in
> doubt about what is real, [`status.md`](status.md) is authoritative.
