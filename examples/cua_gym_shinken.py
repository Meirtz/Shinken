"""CUA-Gym bundles on Shinken — fork-native reset, scripted end to end (no model API).

Demonstrates ``shinken.integrations.cua_gym``: a CUA-Gym exported task bundle
(<https://github.com/xlang-ai/CUA-Gym> ``output/final/<task_id>/`` shape) loaded through
:class:`CuaGymTaskSource` and run on a Shinken sandbox where **reset() is a golden-checkpoint
fork** — bundle setup runs once, every reset materializes a fresh replica from that single
checkpoint (sub-second-to-seconds on the Docker disk tier), where CUA-Gym's own env pays a
fresh cloud VM (~minutes of provisioning) per environment.

Run (Docker + the local image required):

    make sandbox-image                      # once: build shinken/sandbox-linux
    PYTHONPATH=sdk/python/src python examples/cua_gym_shinken.py [path/to/output/final]

External bundles default to process-memory fidelity. After auditing that all live process
state is recreated post-fork, opt in to Docker's disk tier with
``SHINKEN_STATE_FIDELITY=filesystem``.

With no argument a self-contained demo bundle is generated in a temp dir, so the script is
runnable with zero external assets; point it at a real CUA-Gym ``output/final/`` directory
(or set ``$CUA_GYM_TASKS``) to drive real bundles.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from shinken.integrations.cua_gym import CuaGymTaskSource, ShinkenCuaGymEnv
from shinken.providers.base import ProviderError, SandboxSpec
from shinken.providers.docker import DockerLocalProvider


def reset_with_retry(env: ShinkenCuaGymEnv) -> dict:
    """One infra retry: on a busy shared daemon a starved desktop boot can miss readiness;
    that class is retry-eligible (the SDK's own setup/sandbox_died taxonomy)."""
    try:
        return env.reset()
    except ProviderError:
        return env.reset()


def make_demo_bundle_root() -> Path:
    """A minimal bundle in the exported CUA-Gym shape (config.json + setup + reward)."""
    root = Path(tempfile.mkdtemp(prefix="cua-gym-demo-"))
    d = root / "demo_marker_task"
    d.mkdir()
    (d / "initial_setup.py").write_text("open('/tmp/cua_marker.txt', 'w').write('golden-state')\n")
    (d / "reward.py").write_text(
        textwrap.dedent(
            """\
            try:
                ok = open('/tmp/cua_marker.txt').read() == 'golden-state'
            except OSError:
                ok = False
            print('REWARD: 1.0' if ok else 'REWARD: 0.0')
            """
        )
    )
    (d / "config.json").write_text(
        json.dumps(
            {
                "instruction": "demo: golden marker must survive every fork",
                "id": "demo_marker_task",
                "app_type": "os",
                "config": [
                    {
                        "type": "download",
                        "parameters": {
                            "files": [
                                {"url": "oss://demo/initial_setup.py", "path": "/tmp/setup.py"}
                            ]
                        },
                    },
                    {"type": "execute", "parameters": {"command": "python3 /tmp/setup.py"}},
                ],
                "evaluator": {"type": "python", "url": "oss://demo/reward.py"},
            },
            indent=2,
        )
    )
    return root


def main() -> int:
    using_builtin_demo = len(sys.argv) == 1
    root = Path(sys.argv[1]) if not using_builtin_demo else make_demo_bundle_root()
    source = CuaGymTaskSource(root)
    print(f"task source: {len(source)} bundle(s) from {source.root}")
    for path, why in source.skipped:
        print(f"  skipped {path.name}: {why}")
    task = next(iter(source))
    print(f"task: {task.task_id!r} — {task.instruction!r}")

    provider = DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    # This in-tree demo is audited as filesystem-only. Imported bundles retain the
    # adapter's process-memory default unless their trusted caller explicitly opts in.
    fidelity = os.environ.get("SHINKEN_STATE_FIDELITY")
    if fidelity is None and using_builtin_demo:
        fidelity = "filesystem"
    if fidelity not in (None, "filesystem", "process_memory"):
        raise ValueError("SHINKEN_STATE_FIDELITY must be filesystem or process_memory")
    spec = SandboxSpec(state_fidelity=fidelity) if fidelity is not None else None
    with ShinkenCuaGymEnv(task, provider, spec=spec) as env:
        # First reset pays the one-time golden build (create + setup + checkpoint) AND a fork.
        t0 = time.perf_counter()
        obs = reset_with_retry(env)
        first_s = time.perf_counter() - t0
        print(
            f"reset #1 (golden build + fork): {first_s:.1f}s "
            f"— screenshot {len(obs['screenshot'] or b'')} bytes"
        )
        print(f"reward on replica 1: {env.evaluate()}")

        # Every later reset is just a fork from the SAME checkpoint — no setup rerun.
        t0 = time.perf_counter()
        reset_with_retry(env)
        fork_s = time.perf_counter() - t0
        print(f"reset #2 (fork only): {fork_s:.1f}s — golden checkpoint {env.golden_checkpoint}")
        print(f"reward on replica 2: {env.evaluate()}")
        print(f"screen: {env.get_screen_size()}")
        print(
            "fork-native reset replayed nothing — CUA-Gym's own env would have "
            "provisioned a fresh cloud VM per environment instead."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
