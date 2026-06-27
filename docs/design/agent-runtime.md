# Agent Runtime — a narrow-waist runtime with pluggable Workloads, Providers, and Trajectories

> Status: proposed · Role: architecture for the runtime that eval/training/interactive agents all sit on ·
> Audience: maintainers + implementers. Reconciles to **D1** (substrate), **D2** (ACI), **D3** (observation),
> **D5** (runtime state), **D7** (eval). Current built-vs-designed status: [`../engineering/status.md`](../engineering/status.md).

## 1. Why this exists (and the trap it avoids)

Shinken needs **one runtime** that an agent can be driven on for *any* purpose — evaluation, RL/SFT data
collection, an interactive/production agent harness, robustness testing, regression, dataset replay, and
**uses we have not thought of yet**. The earlier framing — "an OSWorld benchmark adapter" — is too narrow:
it bakes *evaluation* assumptions (a task list, a `Scorer`, a pass/fail terminal) into the core, so any
consumer without a "score" (training rollouts, a production agent, robustness testing) fights the abstraction.

**The trap:** if you model the runtime around *consumers* (`eval`, `train`), those concepts leak into the
core and the scope is permanently narrowed. The fix is to model **mechanism, not policy** — a *narrow
waist*. The core knows only sessions, steps, runtime-state, and a trajectory sink. It has **zero**
knowledge of tasks, scores, or rewards. Eval/train/interactive are just *compositions* of those
primitives, layered on top.

**Litmus invariant (hold the design to this):**

> *Can a new, unforeseen consumer be built without changing the core or the `Workload` protocol?*
> If yes, the scope is not narrowed. If no, semantics have leaked into the waist — push them back out.

## 2. Architecture — three orthogonal, pluggable axes

```
   Workload            ×            Runtime (narrow waist)        ×          Provider
   (what to do)                     (how to drive a sandbox)                 (what runs the sandbox)
 ┌───────────────┐         ┌──────────────────────────────────┐       ┌────────────────────────┐
 │ eval          │         │ Session: observe / act (ACI)      │       │ docker   (official)    │
 │ train         │ ──run──▶│ runtime state: checkpoint/fork/   │──────▶│ external (official)    │
 │ interactive   │   rt    │   resume                          │       │ <private, out-of-tree> │
 │ <private…>    │         │ Trajectory (event/step record)    │       │   loaded via env var   │
 └───────────────┘         │ rollout(session, agent, stop)     │       └────────────────────────┘
       ▲                   └──────────────────────────────────┘                ▲
 WorkloadRegistry            (no Scorer / Reward / Task here)            ProviderRegistry
 official in-tree +                                                     official in-tree +
 out-of-tree plugins                                                    out-of-tree plugins
```

All three axes use the **same** discipline: official/public implementations ship in-tree; private ones
(an internal cloud-sandbox provider, a private training pipeline) are loaded from **out-of-tree** plugins
and never appear in a tracked file (§6).

## 3. Layer 0 — Provider (substrate)

Already exists as `providers/SandboxProvider` (lifecycle + `snapshot/restore/fork/checkpoint/resume`).
We add only a **registry** so providers are selected by name and private ones load out-of-tree:

```python
# shinken/providers/__init__.py
def register(name: str, factory) -> None: ...          # name -> zero/kw-arg factory
def get(name: str, **kw) -> SandboxProvider: ...        # load_plugins() then resolve
def available() -> list[str]: ...
def load_plugins(env="SHINKEN_PROVIDER_PLUGINS") -> None # import each ':'-listed module (it self-registers)

register("docker", DockerLocalProvider)                 # OFFICIAL — the only names in a tracked file
register("external", ExternalProvider)
```

`SHINKEN_PROVIDER_PLUGINS` defaults empty, so a fresh public clone behaves identically whether or not a
private plugin is present. The only provider names ever committed are official ones.

## 4. Layer 1 — Runtime core (the narrow waist) — **zero semantics**

```python
# shinken/runtime — knows nothing about tasks, scores, or rewards.

class Session(Protocol):                  # a connected sandbox, provider-backed
    def observe(self, **opts) -> dict: ...                 # ACI observation (screenshot / structured)
    def act(self, action: dict) -> dict: ...               # one canonical ACI action
    def checkpoint(self, name=None) -> str: ...            # runtime-state time-travel (D5)
    def fork(self) -> "Session": ...
    def resume(self, ref) -> "Session": ...
    def close(self) -> None: ...

@dataclass
class Step:
    index: int
    observation: dict           # what the agent saw (coordinate-space tagged)
    action: dict | None         # canonical ACI action taken (None on a terminal/observe-only step)
    event: dict | None          # raw provider/agent event (model output, permission decision, …)
    info: dict = field(default_factory=dict)

@dataclass
class Trajectory:               # the universal artifact — training data, eval evidence, audit ledger
    steps: list[Step]
    terminal: str | None        # consumer-defined sentinel ("done"/"fail"/None); NOT a verdict
    metadata: dict              # provider/model/seed/timestamps/checkpoints — never secrets

def rollout(session, agent, *, max_steps, stop=None, on_step=None) -> Trajectory: ...
    # observe -> agent.decide -> act -> record Step ; repeat until stop()/max_steps/agent-done.
    # NO scorer, NO reward, NO task. A thin, optional helper; callers may drive observe/act directly.
```

`agent` is the existing `operator.Agent` Protocol (`decide(obs) -> Decision`); model adapters
(Anthropic/OpenAI/Kimi-VL) plug in at that boundary unchanged. `rollout` is convenience over
`observe/act` — nothing in the waist forces its use.

### Trajectory schema notes (training + eval + audit, one record)
- `observation` carries the coordinate space and may include pixels and/or a structured a11y snapshot
  (D3); a capture policy decides what is retained (full pixels for training vs. hashes/refs for audit).
- `action` is the **canonical ACI** form (post-adapter), so trajectories are model-agnostic and replayable.
- Rewards/labels/scores are **not** Trajectory fields — a consumer attaches them out-of-band keyed by
  step/episode. This keeps the record consumer-neutral.

## 5. The unified consumer surface — `Workload`

All consumers (eval, train, interactive, and the unforeseen) share **one name** and **one deliberately
under-specified interface** — it unifies *entry + discovery*, never *behaviour*:

```python
# shinken/runtime/workloads.py
@runtime_checkable
class Workload(Protocol):
    name: str
    def run(self, rt: Runtime, **params) -> Any: ...   # return type is the workload's own

# same registry mechanism as providers (so private workloads also load out-of-tree)
def register(name, factory) -> None: ...
def get(name, **kw) -> Workload: ...
def available() -> list[str]: ...
def load_plugins(env="SHINKEN_WORKLOAD_PLUGINS") -> None: ...
```

The protocol **must never** grow `score`, `reward`, `terminal`, or `task` — that is exactly what would
re-narrow the scope. `run(rt) -> Any` is as open as a plain callable; the value added is naming, a
registry, a uniform CLI (`shinken run <workload> --provider <p> …`), and out-of-tree plugin loading.

### Consumers as workloads (each is a thin sibling lib; adding one never touches the core)

| Workload | composition of core primitives | returns |
|---|---|---|
| `eval` | (golden `checkpoint` → `fork` N) → `rollout` → an **optional** `Scorer`(trajectory, session) | pass-rate summary |
| `train` | `rollout`(s) + a `RewardFn`; `fork` for best-of-N / tree-search / reset-from-golden | trajectories / dataset |
| `interactive` | one long `Session`, `observe`/`act`, no terminal, no score | session handle |
| *(robustness testing, regression, replay, A/B substrate, …)* | other compositions | consumer-defined |

`Scorer`, `RewardFn`, `TaskSource` live **in the consumer libs, never in the waist**. **OSWorld is one
example**: a `TaskSource` (its `evaluation_examples` task configs, loaded from `OSWORLD_PATH` — never
embedded in-tree) + a `Scorer` (its official metric/getter `evaluate()`), both consumed by the `eval`
workload, reusing the existing `osworld.py` shim + `parse_model_actions`. Adding a second benchmark =
a new `TaskSource`+`Scorer` and one `register()` line.

### Reference consumers and interop targets (2026-06)

The consumer model is externally validated; three shipped stacks are the named interop targets, in
priority order. None of this touches the waist — all are consumers/compositions.

1. **train (#223) — verl/uni-agent is the reference consumer, not a model to rebuild.** Shinken
   `Trajectory` must be losslessly convertible to verl's `AgentLoopOutput` (`prompt_ids`,
   `response_ids`, `response_mask` with 1=model/0=tool tokens, `response_logprobs`, `reward_score`,
   `num_turns`, `extra_fields{traj_masked, traj_exit_reason}`). **Token fidelity is an adapter
   requirement:** when collecting against a token-level inference server, adapters must pass through
   token ids/masks/logprobs — messages-only records are lossy for RL (retokenization mismatch).
   `Step` already reserves the optional fields (`prompt_token_ids`, `response_token_ids`,
   `response_mask` with 1=model/0=tool, `finish_reason` — all default `None`, populated by no
   current code path) and `Trajectory.exit_reason` covers `traj_exit_reason`, so the conversion is
   lossless once a token-level adapter fills them. The gym shape verl/TRL-style trainers consume
   **now ships in-process** — `shinken.gym` with reset()-as-fork (see the fork-native gym facade
   subsection below); the HTTP facade over it (`/reset`, `/step`, `/evaluate`) remains the train
   Workload's transport deliverable. The cheapest interop deliverable — a swerex-shaped
   deployment backend whose `start()` can fork from a golden checkpoint instead of cold-booting —
   **now ships in-tree** (`shinken.integrations.swerex`; see the subsection below).
   <https://github.com/verl-project/uni-agent>
2. **eval — CUA-Gym bundles as a second task source — SHIPPED** (`shinken.integrations.cua_gym`).
   Exported bundles are OSWorld-shape `config.json` (download + execute setup) plus an in-guest
   python evaluator printing `REWARD: X.X`; `CuaGymTaskSource` loads them (from
   `$CUA_GYM_TASKS`, never embedded in-tree) and `ShinkenCuaGymEnv` mirrors their VM-env method
   surface (screenshot/execute/run_python/upload/download/… — duck-typed, no cua-gym import)
   with **fork-native reset**: bundle setup runs once into a golden checkpoint and every
   `reset()` forks a replica from it, replacing their fresh-cloud-VM-per-environment lifecycle.
   This unlocks 32k oracle-validated RLVR tasks with zero authoring; consumer-side only
   (`TaskSource` never enters the waist). Example: `examples/cua_gym_shinken.py`.
   <https://github.com/xlang-ai/CUA-Gym>
3. **orchestrators — be provider-shaped — SHIPPED for Agentix** (`shinken.integrations.agentix`).
   `ShinkenAgentixProvider` exposes any Shinken provider (default `DockerLocalProvider`) + the
   typed ACI through the exact provider Protocol Agentix orchestrates — async
   `create/delete/get` + the scoped `session()` context manager + the
   `(sandbox_id, runtime_url, status)` handle shape — structurally (runtime-checkable, no
   agentix import; `register_with_agentix()` soft-registers into an installed Agentix). The
   handle carries the typed ACI (`sandbox.aci()`) instead of their pickle-RPC `remote()`, and
   `golden=<checkpoint>` makes every `create()` fork from a golden state — the runtime-state
   lifecycle their roadmap names and their backends lack. The same shape discipline keeps other
   layer-above orchestrators trivial out-of-tree targets. Example: `examples/agentix_shinken.py`.
   <https://github.com/Agentix-Project/Agentix>

### Consumers: RL rollout servers

A rollout-as-a-service control plane (reference: **ProRL-Agent-Server**,
<https://github.com/NVIDIA-NeMo/ProRL-Agent-Server>, Apache-2.0; paper
<https://arxiv.org/abs/2603.18815>) sits *above* the waist: trainers submit task instances over
HTTP and receive scored token-level trajectories, while distributed gateway nodes run an
INIT → RUN → EVAL assembly line, giving each rollout session **one long-lived sandbox runtime**
loaded as a plugin (`RuntimeSpec.import_path = "module:Class"`). The plugin contract is small and
sandbox-shaped — `start/stop/cancel`, `exec(command, cwd, env, timeout_sec) → ExecResult`
(timeout reported as `return_code == -1`), `upload_/download_ file/dir`, plus honest capability
properties — i.e. exactly a Provider-backed session minus the GUI. Shinken serves it **in-tree**:
`sdk/python/src/shinken/integrations/prorl_agent_server.py` maps one Shinken provider-managed
sandbox per rollout session, maps the INIT stage onto **resume-from-golden** (D5,
`kwargs.golden_snapshot`) instead of a cold boot, and leaves the in-guest ACI (`shinkend`)
available to the harness command for real GUI observation/action. Nothing of this touches the
waist: the rollout server is one more consumer; the runtime plugin is a thin shim over the
Provider layer (duck-typed, fixture-tested, no hard dependency on the external package).

### Consumers: uni-agent / verl (swerex-shaped deployment — built)

The narrow-waist story applied: **the trainer is a consumer, Shinken is the runtime.** uni-agent
(<https://github.com/verl-project/uni-agent>, studied at commit `75788ab9`) reaches every sandbox
backend through the SWE-ReX deployment/runtime protocol (<https://github.com/SWE-agent/SWE-ReX>) —
an async deployment (`start`/`stop`/`is_alive` + `runtime`) whose runtime exposes
`create_session`/`run_in_session`/`execute`/`read_file`/`write_file`/`upload`/`close`. Its
`AgentEnv` (the env layer verl rollouts drive) touches only that surface, so anything
deployment-shaped is a uni-agent backend.

`sdk/python/src/shinken/integrations/swerex.py` implements that shape fresh over the waist (no
SWE-ReX code vendored, no `swerex`/`uni_agent` import at module import time — responses/exceptions
duck-type the protocol and upgrade to the real classes when the package is installed):

| swerex operation | Shinken surface |
|---|---|
| `Deployment.start()` | `provider.create(spec)` + `provider.connect()` (ACI handshake = readiness); **fork-native**: `checkpoint=<golden id>` makes every `start()` a `provider.resume` from the committed golden state |
| `Deployment.stop()` / `is_alive()` | session `close()` + `provider.destroy()` / session `ping()` |
| `create_session` / `run_in_session` | bash session emulated over substrate exec (`docker exec bash -lc`, the channel `shinken.inject` proves), exported env + cwd persisted per session, real exit codes |
| `execute` | one-shot substrate exec (argv or shell) |
| `read_file` / `write_file` / `upload` | `sandbox.get_file` / `sandbox.put_file` (hash-verified, real guest boundary) |

What the verl-style RL stack gets for free by sitting on this seam: the GUI half of the ACI
(screenshot/screencast/pointer/keyboard) next to the shell half on the *same* sandbox, and the
runtime-state primitives — `checkpoint`/`fork`/`resume` and the `run_eval_forked`
golden-checkpoint → fork-N → score loop — so rollouts reset from a golden state instead of
cold-booting per episode (the cold-boot pattern uni-agent/CUA-Gym/Agentix all ship today, see
[status](../engineering/status.md)). Runnable wiring: `examples/uniagent_shinken.py`; protocol-shape
unit tests + a Docker-gated live test: `sdk/python/tests/test_swerex_integration.py`.

### Consumers: the fork-native gym facade (trainer-facing shape — built)

`shinken.gym` is **the trainer-facing shape over the narrow waist**: the
`make/reset/step/evaluate` surface every RL stack already consumes (cua-bench, CUA-Gym, OSWorld
wrappers all ship one), with the runtime-state semantics underneath instead of the
sandbox-re-provision every one of them pays per episode (cua-bench's own `Environment.reset()`
closes the session and creates a brand-new VM — `notes/cua-teardown.md` §4/§7):

| gym surface | composition of core primitives |
|---|---|
| `make(task, provider)` | `provider.create` → `task.setup` ONCE → `provider.checkpoint` (the golden state); the reference Docker tier records an immutable filesystem image |
| `reset()` | **a restore/fork**: `provider.resume(golden)` + connect; the measured fork→connected latency is exposed as `info["reset_ms"]` (the current safe Docker path has a historical ~0.60 s measurement; the former 60–120 ms live graft is disabled pending an equivalence-safe design) |
| `step(action)` | canonical ACI dicts OR **raw model text** through `shinken.dialect.parse_actions` (tag dialect + the wild-type XML tool-call grammars); screenshot observation fuses with the actions in ~1 RTT via the pipelined `Sandbox.step`; a `structured` knob returns the guest a11y tree instead |
| `evaluate()` | the task verifier (`shinken.eval` receipt plumbing → float reward; scorer faults are typed, never a fake 0.0) |
| `ShinkenGymPool(task, provider, n)` | N envs, ONE golden checkpoint, one `SharedLoop` + one `FrameCache`, **parallel reset** — the fan-out fork |
| `MultiTurnDataloader(pool)` | a cua-bench-`MultiTurnDataloader`-shaped collection iterator (duck-typed, **no TRL/torch dep**): observation batches out, raw responses in via `async_step`, auto-reset = re-fork, unparseable output typed as `agent_error` |
| `episodes_to_records` / `to_hf_dataset` | every episode is the existing typed `Trajectory`; the exporter flattens to the HF-`datasets` dict-of-lists shape (one row per step, images as PNG bytes, `exit_reason`/taxonomy columns) — `datasets` is lazily imported, plain-dict fallback |

Nothing of this touches the waist: tasks are duck-typed (`shinken.eval.Task` works as-is),
sessions come from any provider, and the gym attaches reward/episode semantics strictly on the
consumer side (the `Trajectory` itself stays verdict-free). Runnable wiring:
`examples/gym_rollout.py`; fixture tests + a Docker-gated live latency/state-inheritance gate:
`sdk/python/tests/test_gym.py`.

## 6. Desensitization — structural, not disciplinary

Private substrate/workloads (an internal cloud-sandbox provider; a private training pipeline) live **only**
in the git-ignored `internal/` tree and register themselves out-of-tree:

```
export SHINKEN_PROVIDER_PLUGINS=<module>     # imported from internal/ (on PYTHONPATH); calls register(...)
shinken run eval --provider <name> --…       # generic CLI; nothing private is named in-tree
```

Guarantees that hold by construction:
1. **Guard scans tracked files only** (`scripts/check-no-internal.sh` over `git ls-files`); `internal/` is
   git-ignored, so private plugins are invisible to the repo by construction.
2. **Generic identifiers only** in-tree: `register/get/available/load_plugins`, `SHINKEN_PROVIDER_PLUGINS`,
   `SHINKEN_WORKLOAD_PLUGINS`, and the official names `docker`/`external`. No private product/service name
   ever appears in a tracked file (add such names to the git-ignored `scripts/deny-list.local` as a tripwire).
3. **Byte-identical public clone**: plugin env vars default empty; `get(<private>)` on a clean clone raises a
   generic "unknown provider; available: [docker, external]" with no hint a private name was expected.
4. **External assets**: benchmark task configs load from `OSWORLD_PATH`/env, never embedded; trajectory
   metadata carries no secrets.

**Three-tier dependency split (host / base / runtime).** A Workload or Provider plugin declares its dependencies in three buckets: a shared **base** set, a **host**-only set (orchestrator-side: heavy evaluators, dataset tooling — e.g. an OSWorld evaluator's large dependency stack belongs here), and a **runtime**-only set (installed inside the sandbox guest image, never on the host). The host orchestrator and the sandbox image install disjoint sets, so a Workload carrying a 50-package evaluator never bloats the guest image, and the guest never pulls host-only tooling. This mapping onto the host/guest boundary is a packaging requirement for any non-trivial Workload, not an afterthought.

**Registry evolution (post-v0.0.1).** Graduate the plugin mechanism from env-var module lists to
`importlib.metadata` entry-point groups (`shinken.providers`, `shinken.workloads`) as the primary
out-of-tree path for *installed public* plugins, keeping `SHINKEN_*_PLUGINS` env vars as the fallback
and as the only mechanism for non-installed private trees (the registry guarantees above are
unchanged: entry points only ever name installed packages; nothing private appears in-tree). Adopt
the registry semantics proven by Agentix's provider plugin system
(https://github.com/Agentix-Project/Agentix): duplicate names raise a conflict error — never silent
last-wins; a broken plugin's import error is captured per-entry without poisoning other plugins; a
`shinken plugins list` command reports distribution provenance; in-process `register()` overrides
entry points for tests.

## 7. Migration (incremental, each PR keeps guard + CI green)

1. **Provider registry** (Layer 0). `register/get/available/load_plugins`; seed `docker`/`external`; port
   `sandbox_bench` off its if/else. No behaviour change elsewhere.
2. **Runtime core**: `Trajectory`/`Step` + `rollout` (semantic-free) + `Workload` protocol + WorkloadRegistry.
3. **`eval` workload + OSWorld**: lift `make_osworld_env`/`run_episode` into an OSWorld `TaskSource`+`Scorer`
   consumed by the `eval` workload; collapse `scripts/osworld_single.py` to a thin CLI over `shinken run eval`.
4. **`train` / `interactive` workloads** (later): rollout collection + reward; long-lived session.

Existing `eval.Task`/`run_eval`, `operator.drive`, `osworld.py`, and the adapters are reused, not rewritten;
the `eval` workload adapts a `rollout`+`Scorer` into the existing `eval.Task` shape so N-replica pass-rate
comes for free.

## 8. Open questions

- Should `run_eval` accept per-replica env injection so benchmark tasks compose without the `setup`-stash
  workaround? (Touches `eval.py`; defer until a 2nd benchmark needs it.)
- Trajectory capture policy: pixels vs. structured vs. hashed refs — per-consumer config; what is the default?
- Do we want a second plugin axis (`SHINKEN_WORKLOAD_PLUGINS`) now, or in-tree-only workloads until a private
  workload genuinely needs out-of-tree loading like providers do?
- For the `interactive` workload (#224): adopt chat-mode semantics from uni-agent — the SAME loop with
  "no tool call ⇒ turn done" instead of a format error, plus per-conversation transcript persistence —
  rather than a separate interactive loop (https://github.com/verl-project/uni-agent). For multi-party
  sessions, the session-identity model to study is caller-declared session ids + idle-TTL eviction +
  per-session visual cursors (trycua/cua's cua-driver — https://github.com/trycua/cua); the permission
  panel (D6) needs session identity anyway — design them together.
- A "Shinken-as-OSWorld-provider" path (serve OSWorld's controller HTTP contract) would let the official
  suite run unmodified on a Shinken substrate — separate spike.
