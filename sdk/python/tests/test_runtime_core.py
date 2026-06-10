"""Runtime narrow waist: Trajectory, rollout, Runtime, and the Workload registry.

The core is semantic-free — these tests never touch a Scorer/Reward/Task, because those
do not exist here."""

from __future__ import annotations

import textwrap

import pytest

import shinken
from shinken import runtime
from shinken.operator import ScriptedAgent
from shinken.runtime import (
    EXIT_REASONS,
    Runtime,
    Step,
    Trajectory,
    resolve_exit_reason,
    rollout,
    workloads,
)

_PLAN = [
    [{"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}}],
    [{"verb": "type_text", "text": "hi"}],
]


def test_trajectory_record_shape():
    t = Trajectory(steps=[Step(0, {"png": b""}, [{"verb": "click"}]), Step(1, {}, [])])
    assert t.num_actions == 1
    d = t.to_dict()
    assert d["num_actions"] == 1 and len(d["steps"]) == 2 and d["terminal"] is None


def test_rollout_records_trajectory_and_terminal(mock_shinkend):
    with shinken.connect(mock_shinkend) as session:
        traj = rollout(session, ScriptedAgent(_PLAN), max_steps=10)
    assert traj.terminal == "done"
    assert traj.exit_reason == "task_complete"
    assert len(traj.steps) == 2 and traj.num_actions == 2
    assert traj.steps[0].actions[0]["verb"] == "click"


def test_rollout_stuck_when_agent_emits_no_actions(mock_shinkend):
    # First turn is empty and NOT the last, so ScriptedAgent reports done=False -> stuck.
    plan = [[], [{"verb": "type_text", "text": "x"}]]
    with shinken.connect(mock_shinkend) as session:
        traj = rollout(session, ScriptedAgent(plan), max_steps=5)
    assert traj.terminal == "stuck" and traj.num_actions == 0
    assert traj.exit_reason == "agent_error"  # no actions and not done is an agent fault


# --- trajectory-level exit_reason (#56): documented precedence + typed defaults ---------


def test_exit_reason_precedence_each_cause_beats_every_lower_one():
    # The documented order: sandbox_died > setup_error > agent_error > scorer_error
    # > max_steps > task_complete. Each cause must win over all causes below it,
    # regardless of argument order.
    assert EXIT_REASONS == (
        "sandbox_died",
        "setup_error",
        "agent_error",
        "scorer_error",
        "max_steps",
        "task_complete",
    )
    for i, high in enumerate(EXIT_REASONS):
        for low in EXIT_REASONS[i + 1 :]:
            assert resolve_exit_reason(high, low) == high
            assert resolve_exit_reason(low, high) == high


def test_resolve_exit_reason_ignores_none_and_rejects_unknown_values():
    assert resolve_exit_reason(None, "max_steps", None) == "max_steps"
    assert resolve_exit_reason() is None and resolve_exit_reason(None) is None
    with pytest.raises(ValueError):
        resolve_exit_reason("flaky")  # a typo must never become a low-precedence reason


def test_rollout_max_steps_sets_budget_exit_reason(mock_shinkend):
    with shinken.connect(mock_shinkend) as session:
        traj = rollout(session, ScriptedAgent(_PLAN), max_steps=1)  # plan needs 2 turns
    assert traj.terminal == "max_steps" and traj.exit_reason == "max_steps"


def test_rollout_records_sandbox_death_as_exit_reason_not_a_crash():
    class _DeadSession:
        def observe(self):
            return {}

        def act_batch(self, actions):
            raise ConnectionError("socket closed under us")

    traj = rollout(_DeadSession(), ScriptedAgent(_PLAN), max_steps=5)
    assert traj.terminal == "aborted" and traj.exit_reason == "sandbox_died"
    assert traj.metadata["error"].startswith("sandbox_died:")
    assert traj.steps == []  # the failed turn is not recorded as a completed step


def test_rollout_records_agent_fault_as_exit_reason_not_a_crash(mock_shinkend):
    class _BadAgent:
        def decide(self, obs):
            raise ValueError("adapter emitted nonsense")

    with shinken.connect(mock_shinkend) as session:
        traj = rollout(session, _BadAgent(), max_steps=5)
    assert traj.terminal == "aborted" and traj.exit_reason == "agent_error"
    assert "adapter emitted nonsense" in traj.metadata["error"]


def test_step_token_fidelity_fields_are_reserved_and_unpopulated():
    # A-2 (#223): the fields exist for lossless RL-trainer conversion, default None,
    # and are serialized so a record's schema is stable before the train Workload lands.
    s = Step(0, {"png": b""})
    assert s.prompt_token_ids is None and s.response_token_ids is None
    assert s.response_mask is None and s.finish_reason is None
    d = s.to_dict()
    for k in ("prompt_token_ids", "response_token_ids", "response_mask", "finish_reason"):
        assert k in d and d[k] is None


def test_trajectory_exit_reason_defaults_none_and_serializes():
    t = Trajectory()
    assert t.exit_reason is None and t.to_dict()["exit_reason"] is None
    t2 = Trajectory(exit_reason="task_complete")
    assert t2.to_dict()["exit_reason"] == "task_complete"


def test_runtime_opens_session_and_rolls_out(mock_shinkend):
    rt = Runtime("external", addr=mock_shinkend)
    session = rt.open()
    try:
        traj = rt.rollout(session, ScriptedAgent(_PLAN), max_steps=10)
    finally:
        session.close()
    assert traj.terminal == "done" and traj.num_actions == 2


# --- Workload registry (mirrors the provider registry; semantic-free) ---


@pytest.fixture
def clean_workloads():
    snap = dict(workloads._REGISTRY)
    loaded = workloads._PLUGINS_LOADED
    try:
        yield
    finally:
        workloads._REGISTRY.clear()
        workloads._REGISTRY.update(snap)
        workloads._PLUGINS_LOADED = loaded


def test_workload_register_get_run(clean_workloads, mock_shinkend):
    class RolloutWorkload:
        name = "rollout-demo"

        def run(self, rt, *, plan, max_steps=10):
            session = rt.open()
            try:
                return rt.rollout(session, ScriptedAgent(plan), max_steps=max_steps)
            finally:
                session.close()

    workloads.register("rollout-demo", RolloutWorkload)
    assert "rollout-demo" in workloads.available()
    wl = workloads.get("rollout-demo")
    result = wl.run(Runtime("external", addr=mock_shinkend), plan=_PLAN)
    assert isinstance(result, Trajectory) and result.terminal == "done"


def test_unknown_workload_raises_listing_available(clean_workloads):
    with pytest.raises(workloads.WorkloadError) as ei:
        workloads.get("not-a-workload")
    assert "unknown workload" in str(ei.value)


def test_out_of_tree_workload_plugin_loads_via_env(clean_workloads, tmp_path, monkeypatch):
    (tmp_path / "shk_wl_plugin.py").write_text(
        textwrap.dedent(
            """
            from shinken.runtime import workloads
            class PluginWorkload:
                name = "plugin-wl"
                def run(self, rt, **p): return "ran"
            workloads.register("plugin-wl", PluginWorkload)
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("SHINKEN_WORKLOAD_PLUGINS", "shk_wl_plugin")
    workloads._PLUGINS_LOADED = False
    assert "plugin-wl" in workloads.available()
    assert workloads.get("plugin-wl").run(None) == "ran"


def test_core_has_no_eval_train_semantics():
    # The waist must stay semantic-free: no Scorer/Reward/Task leaking into the core.
    for banned in ("Scorer", "Reward", "RewardFn", "Task", "TaskSource", "score"):
        assert not hasattr(runtime, banned), f"{banned} must not live in shinken.runtime"
