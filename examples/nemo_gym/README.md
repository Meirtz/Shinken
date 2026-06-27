# NeMo Gym × Shinken — computer-use RL environments with fork-native rollouts

[NeMo Gym](https://github.com/NVIDIA-NeMo/Gym) standardizes RL rollout collection (and
feeds trainers like [NeMo RL](https://github.com/NVIDIA-NeMo/RL) GRPO). This example runs
**real desktop computer-use environments** behind its resources-server contract, where the
per-rollout resource is a **fork of the task's golden checkpoint** — task setup runs once,
every rollout starts from a byte-identical live desktop in well under a second, instead of
re-provisioning an environment per episode.

Observation is **text-first**: `computer_observe` returns the guest a11y engine's numbered
tree (stable `e<N>` ids) and `mode="diff"` returns the `~/+/-` delta — a few hundred bytes
per turn, trainable with any tool-calling LLM (no VLM image plumbing required).

Tasks are [CUA-Gym](https://github.com/xlang-ai/CUA-Gym)-format bundles
(`config.json` + `reward.py` with the `REWARD: X.X` contract); two demo bundles ship in
[`tasks/`](tasks). Point `CUA_GYM_TASKS` at a real CUA-Gym `output/final/` export to scale
the task set.

## 1. The contract smoke (no model, no nemo-gym install)

```bash
# Docker up, shinken/sandbox-linux built
python examples/nemo_gym/local_loop.py
```

A scripted agent drives the engine exactly like NeMo Gym's `simple_agent` would over HTTP
(seed_session → tools → verify) and asserts both demo tasks score `REWARD: 1.0`.

## 2. The real pipeline (`ng_collect_rollouts`)

Per-server venvs land in the workspace, so build it **outside** the repo:

```bash
python examples/nemo_gym/make_workspace.py --dest ~/nemogym-workspace
# wire a policy model (any OpenAI-compatible endpoint) — see the script's printed steps,
# then from the workspace:
ng_run "+config_paths=[resources_servers/shinken_cua/configs/shinken_cua.yaml,...]"
ng_collect_rollouts +agent_name=shinken_cua_simple_agent \
    +input_jsonl_fpath=data/smoke.jsonl +output_jsonl_fpath=data/rollouts.jsonl \
    +limit=2 +num_samples_in_parallel=1
```

Verified end-to-end (2026-06-11, Kimi K2.6 as the policy over the NVIDIA inference API):
both demo tasks at **reward 1.0**, with the GUI task's trajectory using exactly the
intended loop —

```text
zenity-entry: observe → click e7 → type_text "ACME GmbH" → observe(diff: Value confirmed) → key Return
hello-file:   observe → exec(printf … > /tmp/hello.txt) → exec(verify) → done
```

The rollout JSONL is OpenAI-Responses-format with rewards and token accounting — directly
consumable by NeMo RL GRPO / OpenRLHF (see NeMo Gym's training tutorials).

## 3. Real CUA-Gym tasks

Download the released bundle export (10,910 tasks) and triage it for what runs on the
task image (`images/linux/Dockerfile.cua`):

```bash
huggingface-cli download xlangai/CUA-Gym --repo-type dataset \
    --include "artifacts/*" --local-dir ~/datasets/cua-gym-dl
tar --use-compress-program=unzstd -xf ~/datasets/cua-gym-dl/artifacts/cua_gym_tasks_v1.tar.zst -C ~/datasets/cua-gym
python scripts/cua_gym_triage.py ~/datasets/cua-gym --json triage.json
```

Then emit probe-validated train/validation datasets into the workspace (tasks whose
reward is already 1.0 on the unsolved state are excluded — no learning signal):

```bash
python examples/nemo_gym/make_workspace.py --dest ~/nemogym-workspace \
    --task-root ~/datasets/cua-gym --ids-file <probe-receipt.json>
# data/train.jsonl + data/validation.jsonl; serve with CUA_GYM_TASKS=~/datasets/cua-gym
# and SHINKEN_IMAGE=shinken/sandbox-cua:latest in the resources server's environment.
# Only after auditing post-fork process replay, explicitly set:
# SHINKEN_STATE_FIDELITY=filesystem
```

Measured on this lane (2026-06-12, one M4 Pro laptop, Kimi K2.6 policy): a 24-task ×
2-repeat collection ran **48/48 rollouts in 11 m 48 s at parallel 6 (~244 rollouts/hour)**
with zero infrastructure failures — **mean reward 0.43** (25× 0.0 / 2× 0.2 / 1× 0.4 /
20× 1.0), **8 of 24 task groups with intra-group reward variance** (the non-zero-advantage
prompts GRPO trains on), 11 tool calls and ~27k tokens per rollout p50. Environment cost
per rollout is a **fork at p50 0.47 s** from the task's golden checkpoint; mechanical
runnability over the released export is 160/188 on the file_cli class and 10/12 on a
desktop_app sample once the task image carries LibreOffice.

## 4. Train with NeMo RL (GRPO) — the GPU-node handoff

NeMo RL's GRPO integration is **online**: the trainer hosts the policy in vLLM, NeMo Gym
drives rollouts against it live, and the resources server (this one — and therefore the
sandbox fleet) just has to be reachable over HTTP. Per
[NeMo Gym's NeMo RL tutorial](https://docs.nvidia.com/nemo/gym/latest/training-tutorials/index.html),
the trainer-side config points at the same artifacts this workspace already contains:

```yaml
env:
  should_use_nemo_gym: true
  nemo_gym:
    config_paths:
      - responses_api_models/vllm_model/configs/vllm_model_for_training.yaml  # theirs
      - resources_servers/shinken_cua/configs/shinken_cua.yaml                # ours
data:
  train_jsonl_fpath: data/train.jsonl
  validation_jsonl_fpath: data/validation.jsonl
grpo:
  num_prompts_per_step: 4
  num_generations_per_prompt: 4        # GRPO group — N forks of the same golden state
```

Their single-node tutorial trains a ~9B policy on one 8-GPU node (NGC NeMo RL container);
a 1-GPU validation mode exists (`cluster.gpus_per_node=1`). The environment side is
CPU-only and can run on a separate host (the Gym↔trainer link is plain HTTP) — which is
exactly the shape Shinken's fleet numbers are built for: `num_generations_per_prompt`
maps to N forks of one golden checkpoint at ~0.5 s each, where re-provisioning
environments per generation is the cost the trainer otherwise eats.

## 5. Local optimizer-step smoke (not GRPO equivalence)

NeMo RL is the production trainer, but it needs CUDA. [`local_grpo.py`](local_grpo.py)
provides a smaller Apple-Silicon smoke: an **MLX** policy
(Qwen2.5-1.5B-Instruct-4bit + LoRA) drives `ShinkenComputerEngine`; group-relative
advantages weight assistant-token log-probabilities; Adam updates the LoRA parameters.
This proves reward → gradient → optimizer plumbing only. It is not a full GRPO
implementation: it has no old-policy logprobs, importance ratios, clipping, KL term, or
distributed weight synchronization, and it does not establish learning or convergence.

```bash
python -m venv ~/venvs/mlxrl
~/venvs/mlxrl/bin/pip install "mlx-lm>=0.21" -e sdk/python
~/venvs/mlxrl/bin/python examples/nemo_gym/local_grpo.py
```

A recorded smoke (2026-06-12, M4 Pro, group 8, task `hello-file`) executed five LoRA
updates when reward spread was present. The sampled rewards below are a run artifact, not
a controlled learning-quality result:

```text
iter 0: mean_reward=0.250  [0,0,0,0,0,1,0,1]   updated=True (20s)
iter 1: mean_reward=0.250  [0,0,0,0,1,0,1,0]   updated=True (10s)
iter 2: mean_reward=0.125  [0,0,0,0,1,0,0,0]   updated=True (11s)
iter 3: mean_reward=0.875  [1,1,1,0,1,1,1,1]   updated=True (11s)
iter 4: mean_reward=0.875  [1,1,1,1,1,1,1,0]   updated=True (10s)
```

The environment-side grouping still matches the useful production shape:
`num_generations_per_prompt` rollouts can be N forks of one golden checkpoint. Algorithm
equivalence belongs to the real trainer connector, not this smoke.

## Notes

- **Golden vs post-fork setup**: file state belongs in the bundle's `config` steps (runs
  once into the golden checkpoint, on the disk tier). Setup that must exist as a *running
  process* (an open dialog) goes in our bundle extension `shinken_post_fork` — replayed on
  every replica. On the CRIU memory tier the golden carries processes and that list is
  typically empty. The in-tree entrypoints pass an audited
  `SandboxSpec(state_fidelity="filesystem")` for the two demo bundles. Bundle content
  cannot lower its own fidelity requirement: imported bundles default to
  `process_memory` and fail closed on the Docker filesystem tier until a trusted caller
  explicitly opts in after validating the post-fork replay, or selects a process-memory
  provider.
- Rollouts that never reach `/verify` are reaped after an idle TTL; `shinken gc` catches
  anything else.
