"""Runtime narrow waist: Trajectory, rollout, Runtime, and the Workload registry.

The core is semantic-free — these tests never touch a Scorer/Reward/Task, because those
do not exist here."""

from __future__ import annotations

import textwrap

import pytest

import shinken
from shinken import runtime
from shinken.operator import ScriptedAgent
from shinken.runtime import Runtime, Step, Trajectory, rollout, workloads

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
    assert len(traj.steps) == 2 and traj.num_actions == 2
    assert traj.steps[0].actions[0]["verb"] == "click"


def test_rollout_stuck_when_agent_emits_no_actions(mock_shinkend):
    # First turn is empty and NOT the last, so ScriptedAgent reports done=False -> stuck.
    plan = [[], [{"verb": "type_text", "text": "x"}]]
    with shinken.connect(mock_shinkend) as session:
        traj = rollout(session, ScriptedAgent(plan), max_steps=5)
    assert traj.terminal == "stuck" and traj.num_actions == 0


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
