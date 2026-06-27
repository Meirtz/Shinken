"""A local group-relative optimizer-step smoke on real Shinken sandboxes.

NeMo Gym + NeMo RL is the production training path (online GRPO on a GPU node; see the
README §4 handoff). This laptop script proves only that environment rewards can produce a
group-relative policy-gradient and an Adam update to LoRA parameters in one process. It is
not a NeMo RL or GRPO implementation: there are no old-policy logprobs, importance ratios,
clipping, KL term, or distributed weight synchronization, and it makes no convergence claim.

    env    = ShinkenComputerEngine          (seed = fork the task's golden checkpoint,
                                              verify = the CUA-Gym reward.py contract)
    policy = MLX <model> + LoRA adapters     (the only trainable parameters)
    update = smoke: per-task group-relative advantage A_i=(r_i-mean)/std,
             reward-weighted policy-gradient on assistant action tokens, Adam on LoRA.

A recorded 2026-06-12 run executed five update steps when reward variance was present. The
sampled reward sequence is smoke evidence, not a controlled learning-quality evaluation.

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
from shinken import DockerLocalProvider, SandboxSpec  # noqa: E402
from shinken.integrations.cua_gym import CuaGymTaskSource  # noqa: E402
from shinken.integrations.nemo_gym import ShinkenComputerEngine  # noqa: E402

MODEL = os.environ.get("GRPO_MODEL", "mlx-community/Qwen2.5-1.5B-Instruct-4bit")
DEFAULT_TASK_ROOT = REPO / "examples" / "nemo_gym" / "tasks"
TASK_ROOT = Path(os.environ.get("CUA_GYM_TASKS", DEFAULT_TASK_ROOT))
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


def apply_action(engine, sid: str, generation: int, verb: str, arg: str) -> str:
    tool = {
        "exec": ("computer_exec", {"command": arg}),
        "observe": ("computer_observe", {"mode": "diff" if "diff" in arg else "tree"}),
        "click": ("computer_click", {"target": arg}),
        "type": ("computer_type_text", {"text": arg}),
        "key": ("computer_key", {"keys": arg}),
    }.get(verb)
    return engine.tool(sid, *tool, generation=generation) if tool else ""  # done → no-op


def rollout(model, tok, engine, task_id, sampler):
    """One episode → (reward, [(context_ids, action_ids) per assistant turn])."""
    sid = f"grpo-{task_id}-{time.perf_counter_ns()}"
    seeded = engine.seed(sid, task_id, generation=None)
    generation = seeded["generation"]
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
            obs = apply_action(engine, sid, generation, verb, arg)
            messages.append({"role": "user", "content": (obs or "(no output)")[:600]})
        reward = engine.verify(sid, generation=generation)
    except Exception:
        # Infrastructure/configuration failure is not a policy verdict. Turning it into
        # reward 0 would train on a fabricated negative and hide deterministic provider
        # mismatches (for example, requesting process-memory state from the disk tier).
        engine.end(sid, generation=generation)
        raise
    return reward, turns


def seq_logprob(model, ctx_ids, act_ids):
    ids = mx.array(ctx_ids + act_ids)[None]
    ce = nn.losses.cross_entropy(model(ids[:, :-1]), ids[:, 1:], reduction="none")[0]
    return (-ce[len(ctx_ids) - 1 :]).sum()  # summed logprob of the action tokens


def group_relative_pg_loss(model, batch):
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
    loss_and_grad = nn.value_and_grad(model, group_relative_pg_loss)

    src = CuaGymTaskSource(TASK_ROOT)
    fidelity = os.environ.get("SHINKEN_STATE_FIDELITY")
    if fidelity is None and TASK_ROOT.resolve() == DEFAULT_TASK_ROOT.resolve():
        fidelity = "filesystem"
    if fidelity not in (None, "filesystem", "process_memory"):
        raise ValueError("SHINKEN_STATE_FIDELITY must be filesystem or process_memory")
    spec = SandboxSpec(state_fidelity=fidelity) if fidelity is not None else None
    engine = ShinkenComputerEngine(
        DockerLocalProvider(image=IMAGE, name_prefix="shinken-grpo"),
        {TASK_ID: src.get(TASK_ID)},
        spec=spec,
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
                (c, a, adv) for turns, adv in zip(all_turns, advs, strict=True) for (c, a) in turns
            ]
            updated = std > 1e-4 and bool(batch)  # no spread → no gradient signal this group
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
