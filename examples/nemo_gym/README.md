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

## Notes

- **Golden vs post-fork setup**: file state belongs in the bundle's `config` steps (runs
  once into the golden checkpoint, on the disk tier). Setup that must exist as a *running
  process* (an open dialog) goes in our bundle extension `shinken_post_fork` — replayed on
  every replica. On the CRIU memory tier the golden carries processes and that list is
  typically empty.
- Rollouts that never reach `/verify` are reaped after an idle TTL; `shinken gc` catches
  anything else.
