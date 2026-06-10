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
import contextlib
import json
import os
import sys
import time

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
        from shinken.inject import InjectionTarget, pin_x11_display

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
        # Force the X11 backend on the guest's display: a missing/unreachable display then
        # fails the readiness poll loudly instead of silently binding the no-op virtual
        # backend (which screenshots a dead display and scores every task 0).
        pin_x11_display(target, display=args.inject_display)
        return inject_and_actuate(target, args.shinkend_binary, method=args.inject_method)
    return make_shinken_actuator(args.shk_addr)


# Upstream OSWorld run.py argparse defaults (references/OSWorld/run.py) — a deviation from
# any of these makes a Shinken score NOT directly comparable to the official harness, so we
# warn at run start (a parity ledger, not a hard gate).
_OSWORLD_DEFAULTS = {
    "max_steps": 15,
    "sleep_after_execution": 0.0,
    "max_trajectory_length": 3,
    "max_tokens": 1500,
    "observation": "a11y_tree",
}


def _parity_warnings(args: argparse.Namespace) -> list[str]:
    """One line per knob that deviates from the upstream OSWorld default — silent divergence
    is the fastest way to produce an unreproducible number."""
    actual = {
        "max_steps": args.max_steps,
        "sleep_after_execution": args.pause,
        "max_tokens": int(os.environ.get("SHK_SMOKE_MODEL_MAX_TOKENS", _OSWORLD_DEFAULTS["max_tokens"])),
        "observation": args.observation,
    }
    out = []
    for k, want in ((k, _OSWORLD_DEFAULTS[k]) for k in actual):
        if actual[k] != want:
            out.append(f"OSWorld parity warning: {k}={actual[k]!r} (upstream default {want!r})")
    return out


def _emit_result(result: dict, out_path: str | None) -> None:
    print(json.dumps(result, indent=2))  # secret-free: no URL/key
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(result, fh, indent=2)
        print(f"wrote result -> {out_path}", file=sys.stderr)


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
    ap.add_argument(
        "--inject-display",
        default=os.environ.get("OSWORLD_DISPLAY", ":0"),
        help="guest X display the injected shinkend binds (default :0); forces x11_xtest",
    )
    ap.add_argument("--out", help="write the result JSON to this path (in addition to stdout)")
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
    for w in _parity_warnings(args):
        print(w, file=sys.stderr)

    # The result record is M5's acceptance artifact: it must capture task identity, backend,
    # snapshot, wall time, and an error reason — written even when the run raises, so a
    # failed/crashed gate run still leaves an analyzable receipt.
    record: dict = {
        "task": os.path.basename(args.task),
        "task_id": task_config.get("id"),
        "snapshot": task_config.get("snapshot"),
        "backend": args.backend,
        "provider": args.provider,
        "model": os.environ.get("SHK_SMOKE_MODEL_NAME"),
        "max_steps": args.max_steps,
        "observation": args.observation,
        "parity_warnings": _parity_warnings(args),
        "passed": False,
        "score": 0.0,
        "error": None,
        "wall_s": 0.0,
    }
    t0 = time.monotonic()
    env = actuator = None
    try:
        env = make_osworld_env(args.provider, args.width, args.height, args.observation)
        env.reset(task_config)
        agent = ChatModelAgent.from_env()
        actuator = _build_shinken_actuator(args) if args.backend == "shinken" else env
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
        record.update(result)
    except Exception as exc:  # noqa: BLE001 — record the failure as the receipt, then re-raise context
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        record["wall_s"] = round(time.monotonic() - t0, 3)
        if actuator is not None and actuator is not env:
            with contextlib.suppress(Exception):
                actuator.close()
        if env is not None:
            with contextlib.suppress(Exception):
                env.close()
    _emit_result(record, args.out)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
