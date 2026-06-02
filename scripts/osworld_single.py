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
    inject_and_actuate,
    make_osworld_env,
    make_shinken_actuator,
)
from shinken.runtime import Runtime, workloads


def _build_shinken_actuator(args: argparse.Namespace) -> object:
    """Build the Shinken actuator for ``--backend shinken``.

    With ``--inject-method`` the runner injects shinkend into the OSWorld sandbox at runtime
    via the user-chosen transport (``docker``/``ssh``/``osworld-exec``) — the user supplies the
    target (container / ssh host / controller URL); injection errors loudly if it can't reach the
    sandbox (no silent fallback). Without it, we connect to an already-running shinkend at
    ``--shk-addr`` / ``$SHK_ADDR`` (e.g. a pre-baked image)."""
    if args.inject_method:
        from shinken.inject import InjectionTarget

        kw = dict(
            port=args.inject_port,
            reachable_addr=args.inject_reachable_addr,
            container=args.inject_container,
            ssh_host=args.inject_ssh_host,
            ssh_user=args.inject_ssh_user,
            ssh_port=args.inject_ssh_port,
            ssh_key=args.inject_ssh_key,
            controller_url=args.inject_controller_url,
        )
        if args.inject_remote_bin:  # non-root controllers (OSWorld) can't write /usr/local/bin
            kw["remote_bin"] = args.inject_remote_bin
        target = InjectionTarget(**kw)
        return inject_and_actuate(target, args.shinkend_binary, method=args.inject_method)
    return make_shinken_actuator(args.shk_addr)


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
    # --- Shinken actuation: connect to a running shinkend, or inject one at runtime ---
    ap.add_argument("--shk-addr", default=os.environ.get("SHK_ADDR", "127.0.0.1:8765"))
    ap.add_argument(
        "--inject-method",
        choices=["docker", "ssh", "osworld-exec"],
        help="inject shinkend into the OSWorld sandbox via this transport before actuating",
    )
    ap.add_argument("--shinkend-binary", help="path to the (target-arch) shinkend to inject")
    ap.add_argument("--inject-port", type=int, default=8765)
    ap.add_argument("--inject-reachable-addr", help="host:port the SDK connects to (if remapped)")
    ap.add_argument("--inject-container", help="docker: container name/id")
    ap.add_argument("--inject-ssh-host", help="ssh: host")
    ap.add_argument("--inject-ssh-user", help="ssh: user")
    ap.add_argument("--inject-ssh-port", type=int, default=22)
    ap.add_argument("--inject-ssh-key", help="ssh: identity file")
    ap.add_argument("--inject-controller-url", help="osworld-exec: controller base URL")
    ap.add_argument(
        "--inject-remote-bin",
        help="guest path to place shinkend (default /usr/local/bin/shinkend; use e.g. "
        "/tmp/shinkend when the inject transport runs non-root, like an OSWorld controller)",
    )
    args = ap.parse_args(argv)
    if args.inject_method and not args.shinkend_binary:
        ap.error("--inject-method requires --shinkend-binary")

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
    actuator = _build_shinken_actuator(args) if args.backend == "shinken" else env
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
