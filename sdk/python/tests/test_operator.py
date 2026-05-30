"""Operator loop — drive an agent through a task end-to-end (#6 / M3)."""

from __future__ import annotations

import shinken
from shinken.eval import VerifierReceipt, check, run_eval
from shinken.operator import Decision, ScriptedAgent, agent_task, drive
from shinken.skn import Replay

# A deterministic ≥5-step "login" task: focus username, type, focus password, type, submit.
PLAN = [
    [{"verb": "click", "target": {"kind": "point_px", "x": 100, "y": 50}}],
    [{"verb": "type_text", "text": "alice"}],
    [{"verb": "click", "target": {"kind": "point_px", "x": 100, "y": 90}}],
    [{"verb": "type_text", "text": "secret"}],
    [{"verb": "key", "keys": "Return"}],
]
EXPECTED_VERBS = ["click", "type_text", "click", "type_text", "key"]


def _verify(rp: Replay) -> VerifierReceipt:
    # screenshots are now recorded as actions too (#160) — the task verbs are the rest
    verbs = [e["src"] for e in rp.events if e["kind"] == "action" and e["src"] != "screenshot"]
    return VerifierReceipt.from_checks(
        [
            check("at_least_5_actions", len(verbs) >= 5, evidence=len(verbs)),
            check("verb_sequence", verbs == EXPECTED_VERBS, evidence=verbs),
        ]
    )


def test_drive_completes_five_step_task_as_one_bundle(mock_shinkend, tmp_path):
    with shinken.connect(mock_shinkend, record=True) as env:
        res = drive(env, ScriptedAgent(PLAN), max_steps=20)
        path = env.save_replay(str(tmp_path / "run.skn"))
    assert res.done is True and res.stopped == "done"
    assert res.steps == 5 and res.actions == 5

    rp = Replay.load(path)
    rp.validate()  # one bundle, schema-valid, action/observation pairing intact
    # screenshots are recorded as actions too (#160); the task actions are the rest
    actions = [e for e in rp.events if e["kind"] == "action" and e["src"] != "screenshot"]
    obs = [e for e in rp.events if e["kind"] == "observation"]
    assert [e["src"] for e in actions] == EXPECTED_VERBS  # ≥5 steps, in order
    assert len(obs) >= 5  # the agent observed before each turn
    batch_ids = {e["batch_id"] for e in actions}
    assert batch_ids == {f"turn-{i}" for i in range(1, 6)}  # one batch per turn


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
        lambda: shinken.connect(mock_shinkend, record=True),
        n=2,
        out_dir=str(tmp_path),
    )
    assert summary.n == 2 and summary.passed == 2 and summary.pass_rate == 1.0
    assert summary.setup_errors == 0 and summary.mean_steps == 5.0  # ≥5-step task, every run


def test_drive_result_to_dict():
    from shinken.operator import DriveResult

    d = DriveResult(steps=5, actions=5, done=True, stopped="done").to_dict()
    assert d == {"steps": 5, "actions": 5, "done": True, "stopped": "done"}
