# Code-agent RL readiness — reserved seams for SWE/terminal-agent training workloads

> Status: design-only (nothing here is being built now — §7 lists the explicit triggers) ·
> Audience: maintainers + the first train-Workload implementer.
> Reconciles to **D2** (exec is not an ACI wire verb in v0.0.1), **D5** (runtime state),
> **D6** (capability classes), **D7** (eval layer), and the 2026-06 recalibration's interop
> targets (A‑1/A‑2 in [`../engineering/recalibration-2026-06.md`](../engineering/recalibration-2026-06.md)).
> Exec **semantics** (argv-not-shell, bounded, structured errors) are owned by
> [`code-execution.md`](code-execution.md); this doc owns the *RL-workload composition* on top.
> Built-vs-designed truth: [`../engineering/status.md`](../engineering/status.md).

## 1. Why this doc exists (and why it is a new doc)

Shinken's wedge for RL trainers is **runtime state**: a golden checkpoint forked N ways replaces
N cold boots per prompt. The trainer-side stacks that need this today are **terminal/SWE-agent**
loops (verl/uni-agent-class GRPO training, swerex-deployed bash agents), not GUI loops — they need
**no display**, one exec-ish action channel, token-exact trajectories, and an in-sandbox verifier.
None of that is on the v0.0.1 critical path (the alpha gate is a GUI loop), but several v0.0.1
surfaces are the *seams* these workloads will plug into. This doc names those seams precisely so
near-term work keeps them clean — and so nobody "helpfully" implements them early.

It is a separate doc (not an extension of [`code-execution.md`](code-execution.md)) because that
doc is a deliberately narrow **capability-boundary spec** — what exec *is* and why it is never a
GUI verb. This doc is at a different altitude: how a *consumer class* (code-agent RL) composes
exec, headless sandboxes, fork, trajectories, and verifiers. Mixing the two would blur the
boundary spec, which exists precisely to prevent blur.

The narrow-waist litmus from [`agent-runtime.md`](agent-runtime.md) governs everything below:
**code-agent RL must land as Workload/Provider/adapter compositions, with zero changes to the
runtime core or the `Workload` protocol.** Every seam in this doc passes that test.

## 2. (a) The exec primitive — typed verb family, reserved not built

### What v0.0.1 ships (the interim path — substrate side channels)

Per the settled D2 sub-decision ([`tech-decisions.md`](tech-decisions.md) D2, 2026-06): **exec and
file transfer are NOT ACI wire verbs in v0.0.1.** A code workload today reaches a shell through
the substrate's own out-of-band channel, and that path is already built and proven:

- **The injector/controller pattern** (`sdk/python/src/shinken/inject.py`, #230/#233): pluggable,
  explicitly-selected injectors — `docker` (`docker cp` + `docker exec`), `ssh`, `osworld-exec`
  (an OSWorld-style controller's `/execute`) — place a binary in a Sandbox and run commands in it,
  with no silent fallback. Today they inject `shinkend`; the same pattern *is* the interim exec
  channel for setup, reward scripts, and bash-agent actuation.
- **Substrate-side file transfer**: SDK `put_file`/`get_file` ride the substrate
  (`DockerGuestTransport` / `LocalArtifactStore`), checksummed end-to-end (`artifacts.py`, #85).

A SWE/terminal-agent workload can therefore run *today* on any provider that has a side channel —
which is every current provider. That is the design intent, not a gap.

### What is reserved (post-v0.0.1): the typed exec verb family

When a Workload must do setup/scoring/actuation **purely through the ACI** (a substrate with no
side channel — D2's stated revisit trigger), the wire grows a typed exec family. Requirements and
invariants are fixed now; **verb names and exact wire shapes are deliberately NOT specified here**
— D2 says "spec them then", and this doc honors that. What is fixed:

1. **One-shot exec** — the Phase-0 shape in [`code-execution.md`](code-execution.md) verbatim:
   argv vector (never an implicit shell), `cwd`/`env` validated against the granted `fs.scope`,
   required-with-default `timeout_ms` (process group killed on expiry), size-capped
   `stdout`/`stderr` with a `truncated` flag, structured `exit_code`/`timed_out`/`error` — spawn
   failure, policy denial, timeout, and non-zero exit are distinct machine-readable outcomes.
2. **PTY session** — terminal agents (REPLs, TUIs, interactive installers, long-running test
   watchers) need a session, not one-shots: open (rows/cols + command), write, resize, close,
   with output as **server-pushed, seq-numbered stream events** — the same single-writer
   transport, bounded-queue, and `stream`/`seq` reconnect discipline the screencast already
   ships (#48, `resume_stream`). Streamed exec output is "screencast for text": idle-suppressed,
   bounded, resumable. No second streaming mechanism gets invented.
3. **Exit accounting** — a session end always yields a typed terminal record (exit code or
   signal), feeding the same failure taxonomy (`sandbox_died` + per-action status) RL consumers
   already get from `act_batch` and eval (#56/C‑4).
4. **Capability gating (D6)** — the exec family is advertised at handshake **only when the
   Sandbox is provisioned with the code-as-action capability class**, and every invocation routes
   through the `tool_runner` policy boundary (gateway decision: allow/ask/deny). Default-off.
   Never the OSWorld unauthenticated-RCE shape — that is the named anti-pattern this project
   exists to replace ([`osworld-analysis.md`](osworld-analysis.md)).
5. **Sandbox-internal power stays cheap** (D6 boundary rule): running `pytest` inside a
   provisioned code Sandbox is an ordinary in-sandbox power, not a human approval; egress,
   credentials, and host mounts remain boundary capabilities.

The seam to keep clean meanwhile: nothing in the SDK or `shinkend` may assume "action ⇒ GUI
backend exists." The action dispatch path stays verb-routed so an exec verb family can register
beside the pointer/keyboard executors without touching them.

## 3. (b) The headless profile — code agents need no display

A SWE/terminal agent never looks at pixels. The reserved seams:

- **`SandboxSpec.needs_gui=False` already exists** (`sdk/python/src/shinken/providers/base.py`)
  and `ProviderCapabilities.display` already admits `"none"`. These are the routing seam: a
  provider given `needs_gui=False` may serve a display-less Sandbox, and the Control Plane (D9,
  future) routes on exactly these fields. **No new fields are needed — do not add any.**
- **`shinkend` is optional in this profile.** A pure code workload can run with *no ACI session
  at all* — the substrate exec channel (§2 interim path) plus checkpoint/fork is a complete
  loop. When `shinkend` *is* injected headless (e.g. for the typed exec family later, or for
  health/handshake), capability negotiation already handles a backend honestly advertising zero
  GUI verbs; a "virtual" no-op display backend is an alternative if a guest stack insists on
  `DISPLAY`. Decide when built — both fit the existing negotiation contract.
- **Image profile**: `images/linux` exists to prove the GUI slice (Xvfb + Openbox + x11 tools).
  A code-image variant drops the entire X stack and GUI apps — smaller pull, faster boot,
  smaller `docker commit` checkpoints, no X server resident in RAM. It is **not built now**;
  when the first code workload lands it should be a sibling image, not a mode of the GUI image.
- **Warm-pool economics**: headless images shrink exactly the numbers that size warm pools
  ([`economics-and-build-vs-buy.md`](economics-and-build-vs-buy.md)) — per-sandbox RSS (no
  X/compositor), golden-checkpoint size, and fork materialization cost — so code-agent pools run
  denser than GUI pools on the same host. This is also why the CoW-fork density spike was
  extended (T‑6) to measure a **SWE-bench-class image** alongside the OSWorld-class desktop
  image: the code profile is where fork-vs-cold-boot amortization shows up first, at the highest
  N.

## 4. (c) GRPO / N-rollout on runtime state — the flow that justifies the wedge

**The shipped primitive is `eval.run_eval_forked` (#206/#231):** create base → run `task.setup`
once to the golden state → `checkpoint` → materialize each replica from the *single* golden
checkpoint → score → destroy. Its docstring already names the seam: *"one golden state, many
cheap forks — the seam training reuses (best-of-N / tree-search)."*

The train-side flow is the same loop with a trainer on top:

```
golden setup (clone repo, install deps, warm test cache)   — paid ONCE
        │ checkpoint
        ▼
   fork ×N  (one per GRPO rollout, n_resp_per_prompt-style)
        │ rollout (bash/exec loop, token-exact trajectory)
        ▼
   score each (in-sandbox verifier, §5) → rewards → trainer
```

Why this beats per-rollout cold boot: GRPO-style training needs N rollouts **from the identical
state** per prompt. The incumbent terminal-agent RL stack (verl/uni-agent,
[`landscape.md`](landscape.md) §2.14) has zero runtime state, so `n_resp_per_prompt=8` pays 8
cold boots of the same image — repo clone, dependency install, environment warm-up — per prompt,
mitigated only by a creation semaphore; their own docs concede rollout capacity is often the
first bottleneck. Fork-from-golden pays setup once and amortizes it across all N, and the
identical-start guarantee is *stronger* than N independent setups (no setup nondeterminism across
rollouts). First-party fork-vs-cold-boot numbers are the proof obligation (T‑6, time-boxed lead —
see [`../engineering/status.md`](../engineering/status.md)).

**Deployment shape — the swerex-protocol shim (reference, not a redesign):** the Phase-2 interop
deliverable ([`../engineering/roadmap.md`](../engineering/roadmap.md), recalibration A‑1) is
either running `swerex.server` inside the Linux image so a SWE-ReX/uni-agent attach deployment
drives a Shinken Sandbox unmodified, or a **~300-line deployment backend whose `start()` forks
from a golden checkpoint** instead of cold-booting. Plus the HTTP gym facade
(`/reset`, `/step`, `/evaluate`) over the train Workload — the shape verl/TRL-class trainers
consume. Those deliverables are defined there; this doc only fixes what they may assume: the
provider checkpoint/fork contract (D5/A‑6) and the headless profile (§3) are their only
dependencies. They must not require ACI exec verbs (§2) — the side channel suffices.

## 5. (d) The token-fidelity trajectory contract (A‑2)

`Trajectory` stays consumer-neutral (no reward, no verdict — [`agent-runtime.md`](agent-runtime.md)
§4). But RL is lossy without token fidelity: messages-only records retokenize differently than
the policy sampled, corrupting the loss mask. Recalibration A‑2 therefore makes token passthrough
an **adapter requirement** when collecting against a token-level inference server. For lossless
conversion to a verl-style `AgentLoopOutput`, a trainer needs, per trajectory:

| Field (semantic) | Meaning |
|---|---|
| `prompt_token_ids` | the exact prompt tokens the policy was conditioned on |
| `response_token_ids` | the full interleaved rollout tokens (model + tool spans) |
| `response_mask` | per-token: **1 = model-generated, 0 = tool/injected** — the loss mask |
| `response_logprobs` | policy logprobs where available (off-policy correction) |
| `finish_reason` / exit reason | why the rollout ended (length/stop/tool-error/sandbox-died) |
| `num_turns` | turn count for turn-level baselines |

Reward/score stays out-of-band, keyed by episode (the Trajectory rule). **These fields land with
the train Workload (#223)**, and the trajectory-level **exit-reason** field — the last #56
residue — is being reserved on the `feat-exit-reason` branch; that branch owns the concrete field
naming and precedence rules, and this doc deliberately does not duplicate them (mention, don't
conflict). Until #223, token data rides the existing passthrough slots (`Step.info`,
`Trajectory.metadata`) without schema commitment — which is exactly why this doc adds **zero
dataclass fields now**.

The seam to keep clean meanwhile: adapters must not discard provider token-level fields when the
provider returns them; the failure-taxonomy work already gives the `traj_masked`-style signal RL
needs (a sandbox-died rollout is masked, not scored — C‑4/T‑5 split).

## 6. (e) Reward / verifier seams for code tasks

Code-task rewards are programs, and graders are **tested artifacts** (D7's core lesson). Three
seams, all compositions over existing primitives:

1. **In-sandbox `RewardSpec` execution.** A reward is a command run *inside* the Sandbox
   (a pytest subset, a build, a linter, a diff check) over the §2 exec channel — side channel
   today, typed exec verbs later. Validation follows the gold-patch **CI dry-run** pattern the
   incumbent stacks proved (uni-agent's in-sandbox `RewardSpec`s; CUA-Gym's double-test):
   `reward(golden) == 1.0` AND `reward(initial) == 0.0`, machine-checked before a task enters a
   training set. Runtime state makes the double-test cheap: golden checkpoint + one fork replaces
   the dual-VM tax ([`landscape.md`](landscape.md) §2.15 — already ADOPTed as
   `run_eval_forked`'s future verifier-validation mode). Capability posture: running the reward
   is ordinary in-sandbox power (D6); dependency downloads are *not* — bake them into the golden
   image at setup (the golden-image preparation pattern,
   [`../user/runtime-state.md`](../user/runtime-state.md)), so rollouts run with `net.egress`
   empty.
2. **Scorer subprocess isolation (T‑5).** A noisy third-party evaluator must not corrupt a score
   or crash the fork loop: external scorers run host-side in a **subprocess** with an atomic
   result file that is authoritative over the exit code. Scoped in the recalibration to the
   external-evaluator lane (not the in-process reference verifiers); the train Workload inherits
   the same rule for any host-side reward post-processing.
3. **Checksummed task bundles.** A task = (instruction, setup, reward program, fixtures). Every
   byte that crosses into the Sandbox moves via the content-addressed transfer contract
   (`artifacts.py`: `ArtifactRef` path + sha256 + size + scope), so a golden checkpoint's
   provenance is auditable — *which* reward program, byte-exact, produced this score. CUA-Gym
   bundles (OSWorld-shape `config.json` + in-guest evaluator printing `REWARD: X.X`) arrive
   through the same seam as a second eval `TaskSource` + scorer pair; reward scripts are
   checksummed on the way in like any artifact.

All three live in consumer libs (`TaskSource`/`Scorer`/`RewardFn` never enter the waist), and a
heavy evaluator's dependencies go in the **host** bucket of the three-tier dependency split
(A‑4) — never into the guest image.

## 7. (f) Explicitly NOT being built now — and what triggers each piece

| Piece | Status now | Build trigger |
|---|---|---|
| Typed `exec`/PTY/file wire verbs (§2) | side channels only (D2 settled) | a Workload must do setup/scoring/actuation purely through the ACI — a substrate with **no side channel** (D2's revisit clause) |
| Headless code image + headless/virtual `shinkend` mode (§3) | GUI image only | the swerex shim or first SWE workload lands (Phase 2), or the T‑6 SWE-bench-class fork-density measurement needs it |
| swerex-protocol shim / ~300-line fork-from-golden deployment backend + HTTP gym facade (§4) | designed (A‑1) | Phase 2, sequenced **after the alpha gate** (recalibration open list #6) |
| Token-fidelity fields on `Trajectory` + trajectory exit reason (§5) | passthrough slots only | the train Workload (#223), coordinated with the `feat-exit-reason` reservation |
| In-tree `RewardSpec` runner + golden/initial verifier-validation mode (§6) | pattern documented | the CUA-Gym `TaskSource` (second eval task source) or the first train Workload — whichever lands first |
| Subprocess scorer isolation (§6, T‑5) | specified | lands with the external OSWorld evaluator lane |
| A Shinken trainer, task-synthesis pipeline, or agent-loop framework | — | **never** — explicit non-goals ([`roadmap.md`](../engineering/roadmap.md) Phase 2); Shinken plugs *under* trainers, it does not race them |

Nothing in this doc moves v0.0.1 scope; everything composes as Workloads, Providers, adapters,
and image variants on the existing waist. If a future implementation of any row above requires
changing the runtime core or the `Workload` protocol, the design is wrong — re-read §1.
