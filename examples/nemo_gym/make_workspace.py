"""Assemble a NeMo Gym workspace OUTSIDE the repo (per-server venvs must not live in a
cloud-synced tree) and print the exact run commands.

    python examples/nemo_gym/make_workspace.py [--dest ~/nemogym-workspace]

The workspace it builds:

    <dest>/
    ├─ resources_servers/shinken_cua/
    │  ├─ app.py             (copied from examples/nemo_gym/app.py)
    │  ├─ requirements.txt   (shinken from this checkout, editable)
    │  └─ configs/shinken_cua.yaml
    └─ data/smoke.jsonl      (rollout_rows over the demo bundles)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "sdk" / "python" / "src"))

from shinken.integrations.cua_gym import CuaGymTaskSource  # noqa: E402
from shinken.integrations.nemo_gym import rollout_rows  # noqa: E402

CONFIG_YAML = """\
shinken_cua:
  resources_servers:
    shinken_cua:
      entrypoint: app.py
      domain: agent
      verified: false
      description: Computer-use on Shinken sandboxes — every rollout forks a golden desktop checkpoint

shinken_cua_simple_agent:
  responses_api_agents:
    simple_agent:
      entrypoint: app.py
      max_steps: 12
      resources_server:
        type: resources_servers
        name: shinken_cua
      model_server:
        type: responses_api_models
        name: policy_model
      datasets:
      - name: smoke
        type: example
        jsonl_fpath: data/smoke.jsonl
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default="~/nemogym-workspace")
    ap.add_argument(
        "--task-root",
        help="real CUA-Gym bundle export (e.g. ~/datasets/cua-gym); "
        "default: the two in-repo demo bundles",
    )
    ap.add_argument(
        "--ids-file",
        help="JSON receipt with a 'runnable_ids' list (the probe "
        "output) — restrict the dataset to these tasks",
    )
    ap.add_argument(
        "--val-frac",
        type=float,
        default=0.15,
        help="with --task-root: emit train/validation splits (default 0.15)",
    )
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    dest = Path(args.dest).expanduser().resolve()
    if REPO in dest.parents or dest == REPO:
        ap.error(
            f"--dest must be OUTSIDE the repo ({REPO}) — per-server venvs land there"
        )

    server = dest / "resources_servers" / "shinken_cua"
    server.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "app.py", server / "app.py")
    (server / "requirements.txt").write_text(
        f"-e {REPO / 'sdk' / 'python'}\nnemo-gym\n"
    )
    (server / "configs").mkdir(exist_ok=True)
    (server / "configs" / "shinken_cua.yaml").write_text(CONFIG_YAML)

    (dest / "data").mkdir(exist_ok=True)
    tasks = CuaGymTaskSource(HERE / "tasks")
    with (dest / "data" / "smoke.jsonl").open("w") as f:
        for row in rollout_rows(tasks):
            f.write(json.dumps(row) + "\n")

    if args.task_root:
        import random

        src = CuaGymTaskSource(args.task_root)
        ids = sorted(t.task_id for t in src)
        if args.ids_file:
            receipt = json.loads(Path(args.ids_file).expanduser().read_text())
            keep = set(receipt["runnable_ids"])
            # tasks whose unsolved reward is already 1.0 carry no learning signal
            solved = {
                t for t, r in receipt.get("unsolved_rewards", {}).items() if r >= 1.0
            }
            ids = [t for t in ids if t in keep and t not in solved]
        random.Random(args.seed).shuffle(ids)
        n_val = max(1, int(len(ids) * args.val_frac))
        splits = {"validation": ids[:n_val], "train": ids[n_val:]}
        for name, split_ids in splits.items():
            path = dest / "data" / f"{name}.jsonl"
            with path.open("w") as f:
                for row in rollout_rows(src.get(t) for t in split_ids):
                    f.write(json.dumps(row) + "\n")
            print(f"{name}: {len(split_ids)} tasks -> {path}")

    print(f"workspace ready: {dest}\n")
    print("run (needs the nemo-gym CLI on PATH, e.g. `uv tool install nemo-gym`):")
    print(f"  export CUA_GYM_TASKS={HERE / 'tasks'}")
    print("  export POLICY_API_KEY=...   # any OpenAI-compatible endpoint")
    print(f"  cd {dest}")
    print(
        '  ng_run "+config_paths=[resources_servers/shinken_cua/configs/shinken_cua.yaml,'
        'responses_api_models/openai_model/configs/openai_model.yaml]"'
    )
    print("  ng_collect_rollouts +agent_name=shinken_cua_simple_agent \\")
    print(
        "      +input_jsonl_fpath=data/smoke.jsonl +output_jsonl_fpath=data/rollouts.jsonl \\"
    )
    print("      +limit=2 +num_samples_in_parallel=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
