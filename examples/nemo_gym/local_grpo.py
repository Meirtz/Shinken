"""A fully-local GRPO loop: a small MLX policy learns a computer-use task on real Shinken
sandboxes. No CUDA, no NeMo RL — the *learning* half of the loop closes on Apple Silicon.

NeMo Gym + NeMo RL is the production training path (online GRPO on a GPU node; see the
README §4 handoff). This script is the laptop-scale proof that the same loop closes around
Shinken's environment: the fork-native env produces reward, group-relative advantage turns
it into a gradient, and a LoRA update moves the policy — measurably, in one process.

    env    = ShinkenComputerEngine          (seed = fork the task's golden checkpoint,
                                              verify = the CUA-Gym reward.py contract)
    policy = MLX <model> + LoRA adapters     (the only trainable parameters)
    update = GRPO core: per-task groups, group-relative advantage A_i=(r_i-mean)/std,
             reward-weighted policy-gradient on the assistant action tokens, Adam on LoRA.

Measured (2026-06-12, M4 Pro, Qwen2.5-1.5B-Instruct-4bit, group 8, task `hello-file`):
**mean reward 0.25 → 0.875 over 5 iterations in 62 s**, all local.

The agent speaks a one-line text protocol (small models emit it far more reliably than
OpenAI function-call JSON):  `ACTION: <verb> <arg>`  with verb in
observe|exec|click|type|key|done — mapped onto the same engine tools the NeMo Gym
resources server exposes.

Setup (Apple Silicon):
    python -m venv ~/venvs/mlxrl && ~/venvs/mlxrl/bin/pip install "mlx-lm>=0.21" -e sdk/python
Run (Docker up, shinken/sandbox-cua built):
    ~/venvs/mlxrl/bin/python examples/nemo_gym/local_grpo.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

try:
    import mlx.core as mx
    import mlx.nn as nn
    import mlx.optimizers as optim
    from mlx_lm import generate, load
    from mlx_lm.sample_utils import make_sampler
    from mlx_lm.tuner.utils import linear_to_lora_layers
except ImportError:  # pragma: no cover - optional, Apple-Silicon-only dependency
    sys.exit("this example needs mlx-lm on Apple Silicon:  pip install 'mlx-lm>=0.21'")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sdk" / "python" / "src"))
from shinken import DockerLocalProvider  # noqa: E402
from shinken.integrations.cua_gym import CuaGymTaskSource  # noqa: E402
from shinken.integrations.nemo_gym import ShinkenComputerEngine  # noqa: E402

MODEL = os.environ.get("GRPO_MODEL", "mlx-community/Qwen2.5-1.5B-Instruct-4bit")
TASK_ROOT = Path(
    os.environ.get("CUA_GYM_TASKS", REPO / "examples" / "nemo_gym" / "tasks")
)
TASK_ID = os.environ.get("GRPO_TASK", "hello-file")
IMAGE = os.environ.get("SHINKEN_IMAGE", "shinken/sandbox-cua:latest")
GROUP = int(os.environ.get("GRPO_GROUP", "8"))  # rollouts per GRPO group (the baseline)
ITERS = int(os.environ.get("GRPO_ITERS", "5"))  # policy-improvement iterations
MAX_STEPS = 4  # agent steps per rollout
TEMP = 0.8  # sampling temperature (group diversity)
LR = 1e-4

SYSTEM = (
    "You operate a Linux computer to finish a task. Each turn emit EXACTLY one line:\n"
    "ACTION: <verb> <arg>\n"
    "verbs: exec <shell command> | observe tree | click <e7 or x,y> | "
    "type <text> | key <keys> | done\n"
    "Use `exec` for file/CLI work. Emit `done` when the task is complete. "
    "Output ONLY the ACTION line, nothing else."
)
ACTION_RE = re.compile(r"ACTION:\s*(\w+)\s*(.*)", re.I)


def parse_action(text: str) -> tuple[str, str]:
    for line in text.splitlines():
        m = ACTION_RE.match(line.strip())
        if m:
            return m.group(1).lower(), m.group(2).strip()
    return "done", ""  # unparseable → end the episode (a wasted step, honestly)


def apply_action(engine, sid: str, verb: str, arg: str) -> str:
    tool = {
        "exec": ("computer_exec", {"command": arg}),
        "observe": ("computer_observe", {"mode": "diff" if "diff" in arg else "tree"}),
        "click": ("computer_click", {"target": arg}),
        "type": ("computer_type_text", {"text": arg}),
        "key": ("computer_key", {"keys": arg}),
    }.get(verb)
    return engine.tool(sid, *tool) if tool else ""  # done / unknown → no-op


def rollout(model, tok, engine, task_id, sampler):
    """One episode → (reward, [(context_ids, action_ids) per assistant turn])."""
    sid = f"grpo-{task_id}-{time.perf_counter_ns()}"
    seeded = engine.seed(sid, task_id)
    turns: list[tuple[list, list]] = []
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Task: {seeded['instruction']}"},
    ]
    try:
        for _ in range(MAX_STEPS):
            ctx_ids = tok.apply_chat_template(messages, add_generation_prompt=True)
            text = generate(
                model,
                tok,
                prompt=ctx_ids,
                max_tokens=80,
                sampler=sampler,
                verbose=False,
            )
            action_line = next(
                (ln for ln in text.splitlines() if ACTION_RE.match(ln.strip())),
                text.strip(),
            )
            turns.append((ctx_ids, tok.encode(action_line)))
            verb, arg = parse_action(text)
            messages.append({"role": "assistant", "content": action_line})
            if verb == "done":
                break
            obs = apply_action(engine, sid, verb, arg)
            messages.append({"role": "user", "content": (obs or "(no output)")[:600]})
        reward = engine.verify(sid)
    except Exception:  # noqa: BLE001 — a dead episode scores 0, never kills the loop
        engine.end(sid)
        reward = 0.0
    return reward, turns


def seq_logprob(model, ctx_ids, act_ids):
    ids = mx.array(ctx_ids + act_ids)[None]
    ce = nn.losses.cross_entropy(model(ids[:, :-1]), ids[:, 1:], reduction="none")[0]
    return (-ce[len(ctx_ids) - 1 :]).sum()  # summed logprob of the action tokens


def grpo_loss(model, batch):
    """batch: (ctx_ids, act_ids, advantage) — reward-weighted PG, token-normalized."""
    total = mx.array(0.0)
    ntok = 0
    for ctx, act, adv in batch:
        total = total + (-adv) * seq_logprob(model, ctx, act)
        ntok += len(act)
    return total / max(ntok, 1)


def main() -> int:
    print(f"loading {MODEL} …", flush=True)
    model, tok = load(MODEL)
    model.freeze()
    linear_to_lora_layers(model, 8, {"rank": 8, "scale": 20.0, "dropout": 0.0})
    sampler = make_sampler(temp=TEMP)
    opt = optim.Adam(learning_rate=LR)
    loss_and_grad = nn.value_and_grad(model, grpo_loss)

    src = CuaGymTaskSource(TASK_ROOT)
    engine = ShinkenComputerEngine(
        DockerLocalProvider(image=IMAGE, name_prefix="shinken-grpo"),
        {TASK_ID: src.get(TASK_ID)},
    )

    history = []
    try:
        for it in range(ITERS):
            t0 = time.perf_counter()
            rewards, all_turns = [], []
            for _ in range(GROUP):
                r, turns = rollout(model, tok, engine, TASK_ID, sampler)
                rewards.append(r)
                all_turns.append(turns)
            mean = sum(rewards) / len(rewards)
            std = (sum((r - mean) ** 2 for r in rewards) / len(rewards)) ** 0.5
            advs = [(r - mean) / (std + 1e-4) for r in rewards]

            batch = [
                (c, a, adv) for turns, adv in zip(all_turns, advs) for (c, a) in turns
            ]
            updated = std > 1e-4 and bool(
                batch
            )  # no spread → no gradient signal this group
            if updated:
                _loss, grads = loss_and_grad(model, batch)
                opt.update(model, grads)
                mx.eval(model.parameters(), opt.state)
            dt = time.perf_counter() - t0
            history.append(
                {
                    "iter": it,
                    "mean_reward": round(mean, 3),
                    "rewards": rewards,
                    "updated": updated,
                    "wall_s": round(dt, 1),
                }
            )
            print(
                f"iter {it}: mean_reward={mean:.3f} rewards={rewards} updated={updated} "
                f"({dt:.0f}s)",
                flush=True,
            )
    finally:
        engine.close()

    first, last = history[0]["mean_reward"], history[-1]["mean_reward"]
    print(f"\nmean_reward {first:.3f} -> {last:.3f} over {ITERS} iters", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
