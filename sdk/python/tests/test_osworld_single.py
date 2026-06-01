"""OSWorld-as-a-Workload: the `osworld-eval` workload + the thin `scripts/osworld_single.py`
CLI. Exercises model-text -> parse_model_actions -> actuate -> OSWorld-evaluator score with
no VM/model/GPU (fake env + scripted agent)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from shinken import osworld_eval as oe
from shinken.runtime import workloads

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "osworld_single.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("osworld_single", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


osw = _load_cli()


def test_osworld_eval_workload_is_registered():
    # OSWorld is one example over the runtime — resolvable through the workload registry.
    assert "osworld-eval" in workloads.available()


def test_dry_run_cli_runs_full_loop_and_passes():
    assert osw.main(["--dry-run"]) == 0


def test_workload_scores_from_actuated_actions():
    rec = oe.RecordingActuator()
    env = oe.FakeOSWorldEnv(rec)
    agent = oe.ScriptedAgent(
        [
            "reflect.\n```python\npyautogui.click(5, 6)\n```",
            "type.\n```python\npyautogui.write('x')\n```",
            "```DONE```",
        ]
    )
    result = workloads.get("osworld-eval").run(
        None, env=env, agent=agent, actuator=rec, instruction="t", max_steps=10
    )
    assert result["terminal"] == "DONE" and result["passed"] is True and result["score"] == 1.0


def test_parse_model_actions_tolerates_non_string_response():
    # A thinking model can return content=null; the parser must not crash on None/non-str.
    from shinken.osworld import parse_model_actions

    assert parse_model_actions(None) == []
    assert parse_model_actions(123) == []


def test_workload_fails_without_required_actions():
    rec = oe.RecordingActuator()
    env = oe.FakeOSWorldEnv(rec)
    agent = oe.ScriptedAgent(["```python\npyautogui.click(5, 6)\n```", "```DONE```"])  # no write
    result = workloads.get("osworld-eval").run(
        None, env=env, agent=agent, actuator=rec, instruction="t", max_steps=10
    )
    assert result["passed"] is False and result["score"] == 0.0
