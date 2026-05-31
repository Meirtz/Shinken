"""Operator loop — drive an agent through a task end-to-end (#6 / M3)."""

from __future__ import annotations

import shinken
from shinken.eval import VerifierReceipt, check, run_eval
from shinken.operator import Decision, ScriptedAgent, agent_task, drive

# A deterministic ≥5-step "login" task: focus username, type, focus password, type, submit.
PLAN = [
    [{"verb": "click", "target": {"kind": "point_px", "x": 100, "y": 50}}],
    [{"verb": "type_text", "text": "alice"}],
    [{"verb": "click", "target": {"kind": "point_px", "x": 100, "y": 90}}],
    [{"verb": "type_text", "text": "secret"}],
    [{"verb": "key", "keys": "Return"}],
]
EXPECTED_VERBS = ["click", "type_text", "click", "type_text", "key"]


def _verify(_env) -> VerifierReceipt:
    return VerifierReceipt.from_checks(
        [
            check("task_completed", True),
        ]
    )


def test_drive_completes_five_step_task(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        res = drive(env, ScriptedAgent(PLAN), max_steps=20)
    assert res.done is True and res.stopped == "done"
    assert res.steps == 5 and res.actions == 5


def test_drive_stops_at_max_steps(mock_shinkend):
    class _Forever:
        def decide(self, observation):
            return Decision(actions=[{"verb": "wait", "ms": 1}], done=False)

    with shinken.connect(mock_shinkend) as env:
        res = drive(env, _Forever(), max_steps=3)
    assert res.stopped == "max_steps" and res.done is False and res.steps == 3 and res.actions == 3


def test_drive_stops_when_agent_is_stuck(mock_shinkend):
    class _Stuck:
        def decide(self, observation):
            return Decision(actions=[], done=False)

    with shinken.connect(mock_shinkend) as env:
        res = drive(env, _Stuck(), max_steps=10)
    assert res.stopped == "stuck" and res.done is False and res.actions == 0 and res.steps == 1


def test_agent_task_composes_with_run_eval(mock_shinkend, tmp_path):
    task = agent_task("login-5step", lambda: ScriptedAgent(PLAN), _verify, max_steps=20)
    summary = run_eval(
        task,
        lambda: shinken.connect(mock_shinkend),
        n=2,
        out_dir=str(tmp_path),
    )
    assert summary.n == 2 and summary.passed == 2 and summary.pass_rate == 1.0
    assert summary.setup_errors == 0


def test_drive_result_to_dict():
    from shinken.operator import DriveResult

    d = DriveResult(steps=5, actions=5, done=True, stopped="done").to_dict()
    assert d == {"steps": 5, "actions": 5, "done": True, "stopped": "done"}
