"""Run ONE official OSWorld task with a hosted agentic model (e.g. Kimi K2.6) over Shinken.

Thin CLI over the ``osworld-eval`` workload (see ``shinken/osworld_eval.py``). OSWorld
boots + scores (its official evaluator); the model drives in OSWorld's pixel-pyautogui
form; Shinken actuates. Secret-free: model creds via ``SHK_SMOKE_MODEL_*`` env; the
substrate/provider is resolved by name (a private substrate loads out-of-tree via
``$SHINKEN_PROVIDER_PLUGINS``). Try the wiring with no VM, model, or GPU::

    python scripts/osworld_single.py --dry-run

A real run (you supply the image, evaluator, model endpoint, and a shinkend in the VM)::

    python scripts/osworld_single.py --task path/to/task.json --backend shinken --max-steps 15
"""

from __future__ import annotations

import argparse
import json
import os

from shinken.osworld_eval import (
    ChatModelAgent,
    FakeOSWorldEnv,
    RecordingActuator,
    ScriptedAgent,
    make_osworld_env,
    make_shinken_actuator,
)
from shinken.runtime import Runtime, workloads


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run one OSWorld task with a chat agent over Shinken.")
    ap.add_argument("--task", help="path to an OSWorld task_config JSON")
    ap.add_argument("--backend", choices=["shinken", "osworld"], default="shinken")
    ap.add_argument("--provider", default=os.environ.get("OSWORLD_PROVIDER", "docker"))
    ap.add_argument(
        "--observation", choices=["screenshot", "screenshot_a11y_tree"], default="screenshot"
    )
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--max-steps", type=int, default=15)
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true", help="exercise the loop with no VM/model/GPU")
    args = ap.parse_args(argv)

    workload = workloads.get("osworld-eval")

    if args.dry_run:
        recorder = RecordingActuator()
        env = FakeOSWorldEnv(recorder)
        agent = ScriptedAgent(
            [
                "click the field.\n```python\npyautogui.click(960, 540)\n```",
                "type the text.\n```python\npyautogui.write('hello shinken')\n```",
                "task complete.\n```DONE```",
            ]
        )
        result = workload.run(
            Runtime(),
            env=env,
            agent=agent,
            actuator=recorder,
            instruction="dry-run task",
            max_steps=args.max_steps,
            observation="screenshot",
        )
        result.update(task="(dry-run)", backend="recording", model="(scripted)")
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1

    if not args.task:
        ap.error("--task is required (or use --dry-run)")
    with open(args.task) as fh:
        task_config = json.load(fh)
    env = make_osworld_env(args.provider, args.width, args.height, args.observation)
    env.reset(task_config)
    agent = ChatModelAgent.from_env()
    actuator = make_shinken_actuator() if args.backend == "shinken" else env
    try:
        result = workload.run(
            Runtime(),
            env=env,
            agent=agent,
            actuator=actuator,
            instruction=task_config.get("instruction", ""),
            max_steps=args.max_steps,
            observation=args.observation,
            pause=args.pause,
        )
    finally:
        if actuator is not env:
            actuator.close()
        env.close()
    result.update(
        task=os.path.basename(args.task),
        backend=args.backend,
        model=os.environ.get("SHK_SMOKE_MODEL_NAME"),
    )
    print(json.dumps(result, indent=2))  # secret-free: no URL/key
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
