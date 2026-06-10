"""Subprocess scorer isolation (T-5, #56): a misbehaving external evaluator — stray
stdout, a non-zero exit after a correct verdict, a crash, a hang — must yield either an
authoritative atomically-written verdict or a typed ScorerError, never a corrupted score."""

from __future__ import annotations

import os
import sys
import time

import pytest

from shinken import scorer_proc as sp
from shinken.errors import ScorerError

# --- scripted children for the command lane (each plays one misbehavior) --------------

# Writes a correct verdict atomically, then misbehaves: stray non-JSON stdout AND a
# non-zero exit. The written result file must stay authoritative.
_WRITE_THEN_FAIL = """
import json, os, sys
task = json.load(sys.stdin)
path = sys.argv[1]
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump({"score": task["want"]}, fh)
os.replace(tmp, path)
print("stray evaluator chatter, not JSON")
sys.exit(17)
"""

# Writes the verdict, then hangs past the deadline — killed, but the verdict stands.
_WRITE_THEN_HANG = """
import json, os, sys, time
task = json.load(sys.stdin)
path = sys.argv[1]
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump({"score": 0.5}, fh)
os.replace(tmp, path)
time.sleep(60)
"""

_HANG_NO_WRITE = "import time\ntime.sleep(60)\n"
_CRASH = 'import sys\nprint("boom: evaluator blew up", file=sys.stderr)\nsys.exit(3)\n'
_GARBAGE = 'print("not a verdict")\n'


def _run(script: str, task: dict, timeout: float = 30.0) -> dict:
    return sp.run_scorer_command([sys.executable, "-c", script], task, timeout=timeout)


def test_result_file_is_authoritative_over_stray_stdout_and_nonzero_exit():
    assert _run(_WRITE_THEN_FAIL, {"want": 1.0}) == {"score": 1.0}


def test_result_file_is_authoritative_over_a_timeout_after_the_write():
    assert _run(_WRITE_THEN_HANG, {}, timeout=2.0) == {"score": 0.5}


def test_timeout_with_no_verdict_is_typed():
    with pytest.raises(ScorerError) as ei:
        _run(_HANG_NO_WRITE, {}, timeout=0.5)
    assert ei.value.kind == "timeout"


def test_crash_with_no_verdict_is_typed_and_carries_exit_detail():
    with pytest.raises(ScorerError) as ei:
        _run(_CRASH, {})
    assert ei.value.kind == "crash" and ei.value.exit_code == 3
    assert "boom" in (ei.value.detail or "")


def test_garbage_stdout_with_clean_exit_is_typed():
    with pytest.raises(ScorerError) as ei:
        _run(_GARBAGE, {})
    assert ei.value.kind == "garbage"
    assert "not a verdict" in (ei.value.detail or "")


# --- run_scorer: the python -m shinken.scorer_proc entrypoint lane --------------------


def test_run_scorer_imports_entrypoint_and_returns_normalized_verdict(tmp_path):
    (tmp_path / "scorer_fixture.py").write_text(
        "def score(task):\n    return {'score': task['x'] * 0.5, 'evidence': 'halved'}\n"
    )
    v = sp.run_scorer(
        "scorer_fixture:score", {"x": 2.0}, timeout=60, env={"PYTHONPATH": str(tmp_path)}
    )
    assert v == {"score": 1.0, "evidence": "halved"}


def test_run_scorer_entrypoint_crash_is_typed(tmp_path):
    (tmp_path / "scorer_fixture_bad.py").write_text(
        "def score(task):\n    raise RuntimeError('evaluator exploded')\n"
    )
    with pytest.raises(ScorerError) as ei:
        sp.run_scorer("scorer_fixture_bad:score", {}, timeout=60, env={"PYTHONPATH": str(tmp_path)})
    assert ei.value.kind == "crash"
    assert "evaluator exploded" in (ei.value.detail or "")


# --- run_scorer_callable: the fork lane for live-object scorers (OSWorld evaluate) ----


def test_callable_success_and_dict_verdict_normalization():
    assert sp.run_scorer_callable(lambda: 0.75) == {"score": 0.75}
    v = sp.run_scorer_callable(lambda: {"score": 1, "evidence": ["a"]})
    assert v == {"score": 1.0, "evidence": ["a"]}


def test_callable_crash_is_typed_with_traceback_detail():
    def boom() -> float:
        raise RuntimeError("live evaluator died")

    with pytest.raises(ScorerError) as ei:
        sp.run_scorer_callable(boom)
    assert ei.value.kind == "crash" and ei.value.exit_code == 1
    assert "live evaluator died" in (ei.value.detail or "")


def test_callable_timeout_is_typed():
    with pytest.raises(ScorerError) as ei:
        sp.run_scorer_callable(lambda: time.sleep(60), timeout=0.5)
    assert ei.value.kind == "timeout"


def test_callable_silent_exit_without_verdict_is_garbage():
    with pytest.raises(ScorerError) as ei:
        sp.run_scorer_callable(lambda: os._exit(0))  # exits 0 but never writes a verdict
    assert ei.value.kind == "garbage"


def test_callable_contains_stray_child_stdout(capfd):
    def chatty() -> float:
        print("evaluator noise that must not reach the parent", flush=True)
        return 1.0

    assert sp.run_scorer_callable(chatty) == {"score": 1.0}
    out, _err = capfd.readouterr()
    assert "evaluator noise" not in out  # redirected to the child log, not the harness fds
