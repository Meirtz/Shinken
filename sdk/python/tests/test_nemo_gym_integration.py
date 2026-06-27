"""Fixture tests for shinken.integrations.nemo_gym — the resources-server engine against
fakes (no Docker, no nemo_gym install; the live proof is examples/nemo_gym/local_loop.py)."""

from __future__ import annotations

import asyncio
import json

import pytest

from shinken.integrations.cua_gym import CuaGymError, CuaGymTask
from shinken.integrations.nemo_gym import (
    COMPUTER_TOOLS,
    ShinkenComputerEngine,
    _install_engine_shutdown,
    _parse_click_target,
    extract_task_id,
    rollout_rows,
)

# ----------------------------------------------------------------- fakes


class FakeSess:
    def __init__(self, log):
        self.log = log

    def observe(self, structured=False, settle_ms=None, **_):
        self.log.append(("observe", settle_ms))
        return {"tree_text": "app: demo (revision 1, 2 nodes)\ne1 frame\ne2 text", "focus": "e2"}

    def observe_diff(self, settle_ms=None, **_):
        self.log.append(("observe_diff", settle_ms))
        return {"tree_text": '~ e2 text Value:"hi"', "tree": "diff"}

    def act_on(self, ref, verb="click"):
        self.log.append(("act_on", ref, verb))
        return {"ok": True}

    def click(self, x=None, y=None):
        self.log.append(("click", x, y))

    def type_text(self, text):
        self.log.append(("type_text", text))

    def key(self, keys):
        self.log.append(("key", keys))

    def scroll(self, dy=0, **_):
        self.log.append(("scroll", dy))

    def screen_size(self):
        return {"w": 1280, "h": 800}


class FakeEnv:
    """Stands in for ShinkenCuaGymEnv: same lifecycle surface, records everything."""

    def __init__(self, task, provider, spec=None, exec_timeout=60.0, **_):
        self.task = task
        self.provider = provider
        self.golden_checkpoint = None
        self.log: list = []
        self._sess = FakeSess(self.log)
        self.closed = False

    def _build_golden(self):
        self.log.append(("build_golden", self.task.task_id))
        return f"snap:{self.task.task_id}"

    def reset(self):
        self.log.append(("reset", self.golden_checkpoint))
        return {"instruction": self.task.instruction}

    def _session(self):
        return self._sess

    def _guest_exec(self):
        return lambda argv, **kw: (0, "", "")

    def _apply_setup_step(self, step, sess, run):
        self.log.append(("post_fork", step.get("type")))

    def execute(self, command):
        self.log.append(("execute", command))
        return {"output": "x" * 3000, "error": "", "returncode": 0}

    def evaluate(self):
        self.log.append(("evaluate",))
        return 1.0

    def screenshot(self):
        return b"\x89PNG" + b"0" * 64

    def close(self):
        self.closed = True


def make_task(tmp_path, task_id="t1", config=None):
    return CuaGymTask(
        task_id=task_id,
        instruction=f"do {task_id}",
        app_type="gui",
        path=tmp_path,
        config=config or {},
    )


def make_engine(tmp_path, **kw):
    task = make_task(tmp_path, config=kw.pop("task_config", None))
    engine = ShinkenComputerEngine(
        provider=object(), tasks={task.task_id: task}, env_factory=FakeEnv, **kw
    )
    return engine, task


# ----------------------------------------------------------------- schemas / rows


def test_tool_schemas_are_strict_and_dispatchable(tmp_path):
    engine, _ = make_engine(tmp_path)
    names = [t["name"] for t in COMPUTER_TOOLS]
    assert len(names) == len(set(names))
    for tool in COMPUTER_TOOLS:
        assert tool["type"] == "function"
        assert tool["parameters"]["additionalProperties"] is False
        short = tool["name"].removeprefix("computer_")
        assert hasattr(engine, f"_tool_{short}"), f"no handler for {tool['name']}"


def test_rollout_rows_route_by_task_id(tmp_path):
    tasks = [make_task(tmp_path, "alpha"), make_task(tmp_path, "beta")]
    rows = list(rollout_rows(tasks))
    assert [r["responses_create_params"]["metadata"]["task_id"] for r in rows] == ["alpha", "beta"]
    for row, task in zip(rows, tasks, strict=False):
        params = row["responses_create_params"]
        assert params["input"][0]["role"] == "system"
        assert params["input"][1] == {"role": "user", "content": task.instruction}
        assert params["tools"] == COMPUTER_TOOLS
    assert extract_task_id(rows[0]) == "alpha"
    assert extract_task_id({"responses_create_params": {}}) is None


def test_click_target_parsing():
    assert _parse_click_target("e7") == {"ref": "e7"}
    assert _parse_click_target(" 640, 420 ") == {"x": 640, "y": 420}
    with pytest.raises(CuaGymError):
        _parse_click_target("the OK button")


# ----------------------------------------------------------------- engine lifecycle


def test_seed_tool_verify_lifecycle(tmp_path):
    engine, task = make_engine(tmp_path)
    seeded = engine.seed("s1", task.task_id)
    assert seeded["task_id"] == task.task_id and seeded["reset_ms"] >= 0

    tree = engine.tool("s1", "computer_observe", {"mode": "tree"})
    assert "e1 frame" in tree and "focus: e2" in tree
    diff = engine.tool("s1", "computer_observe", {"mode": "diff"})
    assert diff.startswith("~ e2")  # second observe goes through observe_diff

    assert engine.tool("s1", "computer_click", {"target": "e2"}) == "clicked e2"
    assert engine.tool("s1", "computer_click", {"target": "10,20"}) == "clicked at (10, 20)"
    engine.tool("s1", "computer_type_text", {"text": "hi"})
    engine.tool("s1", "computer_key", {"keys": "Return"})
    engine.tool("s1", "computer_scroll", {"dy": -3})

    out = json.loads(engine.tool("s1", "computer_exec", {"command": "ls"}))
    assert out["returncode"] == 0 and len(out["stdout"]) == 2000  # truncated

    shot = engine.tool("s1", "computer_screenshot", {})
    assert "1280x800" in shot and "computer_observe" in shot

    assert engine.verify("s1") == 1.0
    with pytest.raises(CuaGymError, match="seed_session must run first"):
        engine.tool("s1", "computer_observe", {"mode": "tree"})


def test_unknown_tool_and_unknown_task(tmp_path):
    engine, task = make_engine(tmp_path)
    with pytest.raises(CuaGymError, match="unknown task_id"):
        engine.seed("s1", "nope")
    engine.seed("s1", task.task_id)
    with pytest.raises(CuaGymError, match="unknown tool"):
        engine.tool("s1", "computer_frobnicate", {})


def test_unknown_task_from_raising_task_source_is_normalized(tmp_path):
    class RaisingTaskSource:
        def get(self, task_id):
            raise KeyError(task_id)

    engine = ShinkenComputerEngine(
        provider=object(), tasks=RaisingTaskSource(), env_factory=FakeEnv
    )
    with pytest.raises(CuaGymError, match="unknown task_id"):
        engine.seed("s1", "missing")


def test_golden_is_built_once_and_shared(tmp_path):
    engine, task = make_engine(tmp_path)
    engine.seed("s1", task.task_id)
    engine.seed("s2", task.task_id)
    builds = [
        e for sid in ("s1", "s2") for e in engine._rollouts[sid].env.log if e[0] == "build_golden"
    ]
    assert len(builds) == 1  # second seed reuses the cached golden
    assert engine._rollouts["s2"].env.golden_checkpoint == f"snap:{task.task_id}"


def test_post_fork_steps_replay_per_seed(tmp_path):
    engine, task = make_engine(
        tmp_path,
        task_config={"shinken_post_fork": [{"type": "execute", "parameters": {"command": "x"}}]},
    )
    engine.seed("s1", task.task_id)
    assert ("post_fork", "execute") in engine._rollouts["s1"].env.log


def test_post_fork_failure_closes_unpublished_replica(tmp_path):
    created = []

    class FailingPostForkEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def _apply_setup_step(self, step, sess, run):
            raise RuntimeError("post-fork setup failed")

    task = make_task(
        tmp_path,
        config={"shinken_post_fork": [{"type": "execute", "parameters": {"command": "x"}}]},
    )
    engine = ShinkenComputerEngine(
        provider=object(), tasks={task.task_id: task}, env_factory=FailingPostForkEnv
    )
    with pytest.raises(RuntimeError, match="post-fork setup failed"):
        engine.seed("s1", task.task_id)
    assert created[0].closed
    assert "s1" not in engine._rollouts


def test_reseed_replaces_replica_and_close_reaps(tmp_path):
    engine, task = make_engine(tmp_path)
    engine.seed("s1", task.task_id)
    first_env = engine._rollouts["s1"].env
    engine.seed("s1", task.task_id)
    assert first_env.closed  # the old replica was torn down
    engine.close()
    assert not engine._rollouts


def test_webserver_shutdown_closes_engine_resources():
    class App:
        def add_event_handler(self, event, handler):
            assert event == "shutdown"
            self.shutdown = handler

    class Engine:
        closed = False

        def close(self):
            self.closed = True

    app, engine = App(), Engine()
    _install_engine_shutdown(app, engine)
    asyncio.run(app.shutdown())
    assert engine.closed


def test_idle_rollouts_are_reaped(tmp_path):
    engine, task = make_engine(tmp_path, idle_ttl_s=0.01)
    engine.seed("s1", task.task_id)
    stale_env = engine._rollouts["s1"].env
    engine._rollouts["s1"].last_used -= 10
    engine.seed("s2", task.task_id)  # any seed sweeps the idle table
    assert "s1" not in engine._rollouts and stale_env.closed
