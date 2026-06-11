"""The full resources-server loop, locally, with NO model and NO nemo_gym install:
a scripted agent drives ``ShinkenComputerEngine`` exactly the way NeMo Gym's
``simple_agent`` would over HTTP — seed_session → tool calls → verify — and asserts
both demo tasks score ``REWARD: 1.0``.

This is the contract smoke for ``shinken.integrations.nemo_gym``: if this passes, the
engine half of the resources server is proven against real sandboxes; what remains
between you and ``ng_collect_rollouts`` is only the HTTP shell (``app.py``) and a model.

Run:  python examples/nemo_gym/local_loop.py        (Docker up, shinken/sandbox-linux built)
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "sdk" / "python" / "src"))

from shinken import DockerLocalProvider  # noqa: E402
from shinken.integrations.cua_gym import CuaGymTaskSource  # noqa: E402
from shinken.integrations.nemo_gym import ShinkenComputerEngine, rollout_rows  # noqa: E402


def scripted_hello(engine: ShinkenComputerEngine, sid: str) -> None:
    """Solve hello-file the way a CLI-leaning model would: one exec call."""
    out = engine.tool(sid, "computer_exec", {"command": "printf 'shinken' > /tmp/hello.txt"})
    assert '"returncode": 0' in out, out


def scripted_zenity(engine: ShinkenComputerEngine, sid: str) -> None:
    """Solve zenity-entry the way a GUI model would: observe → click by id → type → diff."""
    tree = engine.tool(sid, "computer_observe", {"mode": "tree"})
    assert "Vendor" in tree, f"dialog not in tree:\n{tree}"
    entry = re.search(r"^\s*(e\d+) (?:text|entry) ", tree, re.M)
    assert entry, f"no entry element in tree:\n{tree}"
    print(engine.tool(sid, "computer_click", {"target": entry.group(1)}))
    print(engine.tool(sid, "computer_type_text", {"text": "ACME GmbH"}))
    diff = engine.tool(sid, "computer_observe", {"mode": "diff"})
    assert "ACME GmbH" in diff, f"typed value missing from diff:\n{diff}"
    print(f"diff confirms typed value:\n{diff}")
    print(engine.tool(sid, "computer_key", {"keys": "Return"}))
    time.sleep(0.8)  # zenity flushes stdout on exit


def main() -> int:
    tasks = CuaGymTaskSource(HERE / "tasks")
    assert not tasks.skipped, f"malformed bundles: {tasks.skipped}"
    rows = list(rollout_rows(tasks))
    assert all(r["responses_create_params"]["metadata"]["task_id"] for r in rows)
    print(f"dataset rows: {len(rows)} (ng_collect_rollouts-ready)")

    provider = DockerLocalProvider(name_prefix="shinken-nemogym")
    engine = ShinkenComputerEngine(provider, tasks)
    solvers = {"hello-file": scripted_hello, "zenity-entry": scripted_zenity}
    try:
        for task in tasks:
            sid = f"local-{task.task_id}"
            seeded = engine.seed(sid, task.task_id)
            print(f"\n=== {task.task_id}: seeded (reset {seeded['reset_ms']:.0f} ms) ===")
            solvers[task.task_id](engine, sid)
            reward = engine.verify(sid)
            print(f"=== {task.task_id}: REWARD {reward} ===")
            assert reward == 1.0, f"{task.task_id} scored {reward}"
    finally:
        engine.close()
    print("\nlocal loop PASSED: 2/2 tasks at reward 1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
