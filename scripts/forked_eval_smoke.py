"""E9 proof: the tiny eval harness runs `run_eval_forked` over the Docker disk tier with a
REAL-state verifier — proving forks inherit the golden checkpoint's filesystem and that the
verdict reads observed guest state, not the task's own inputs.

The task's setup writes a golden file into the base sandbox (over the file-transfer channel);
`run_eval_forked` checkpoints that state once, then forks N replicas from the single golden
checkpoint; each replica's verifier `get_file`s the golden file and checks its content. A fork
that did not inherit the checkpointed state (or a broken fork) fails the check.

Run (Docker required):  PYTHONPATH=sdk/python/src python scripts/forked_eval_smoke.py [N]
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from shinken.eval import Task, VerifierReceipt, check, run_eval_forked
from shinken.providers.docker import DockerLocalProvider

MARKER = "golden-state-v1"
GUEST_FILE = "/tmp/shinken_golden.txt"


def _setup(env) -> None:
    # Reach the golden state once: write a known file into the base sandbox. The checkpoint
    # taken right after captures this; every fork must inherit it.
    src = Path(tempfile.mkdtemp()) / "golden.txt"
    src.write_text(MARKER)
    env.put_file(str(src), GUEST_FILE)


def _verify(env) -> VerifierReceipt:
    out = Path(tempfile.mkdtemp()) / "got.txt"
    try:
        env.get_file(GUEST_FILE, str(out))
        content = out.read_text().strip()
    except Exception as exc:  # noqa: BLE001 — a missing file is a real failed check
        return VerifierReceipt.from_checks(
            [check("golden file present", False, {"error": str(exc)})]
        )
    return VerifierReceipt.from_checks(
        [check("fork inherited golden state", content == MARKER, {"observed": content})]
    )


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    prov = DockerLocalProvider(image="shinken/sandbox-linux")
    task = Task(name="golden-file-inheritance", run=lambda _e: None, verify=_verify, setup=_setup)
    summary = run_eval_forked(task, prov, n=n)
    print(json.dumps(summary.to_dict(), indent=2))
    ok = summary.passed == n and summary.infra_errors == 0
    print(f"E9 {'PASS' if ok else 'FAIL'}: {summary.passed}/{n} forks inherited the golden state")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
