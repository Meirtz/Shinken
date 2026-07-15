"""Fixture tests for shinken.integrations.nemo_gym — the resources-server engine against
fakes (no Docker, no nemo_gym install; the live proof is examples/nemo_gym/local_loop.py)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

from shinken.integrations.cua_gym import CuaGymError, CuaGymTask
from shinken.integrations.nemo_gym import (
    COMPUTER_TOOLS,
    ShinkenComputerEngine,
    _install_engine_shutdown,
    _parse_click_target,
    _require_single_worker,
    build_resources_server_cls,
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
        self.close_calls = 0
        self.closed_event = threading.Event()

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
        if (self.task.config or {}).get("_scorer_raises"):
            raise CuaGymError("reward.py failed (rc=1): AssertionError: Expected 1.0, got 0.0")
        return 1.0

    def screenshot(self):
        return b"\x89PNG" + b"0" * 64

    def close(self):
        self.closed = True
        self.close_calls += 1
        self.closed_event.set()


class FakeProvider:
    def __init__(self):
        self.deleted: list[str] = []

    def delete_snapshot(self, snapshot):
        self.deleted.append(snapshot)


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
    provider = kw.pop("provider", FakeProvider())
    kw.setdefault("reap_interval_s", 0)
    engine = ShinkenComputerEngine(
        provider=provider, tasks={task.task_id: task}, env_factory=FakeEnv, **kw
    )
    return engine, task


def start_call(fn):
    result: list = []
    done = threading.Event()

    def run():
        try:
            result.append(fn())
        except BaseException as exc:
            result.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, done, result


def join_call(thread, done, timeout=2.0):
    assert done.wait(timeout), "concurrent engine call did not finish"
    thread.join(timeout)
    assert not thread.is_alive(), "concurrent engine call deadlocked"


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
    seeded = engine.seed("s1", task.task_id, generation=None)
    generation = seeded["generation"]
    assert seeded["task_id"] == task.task_id and seeded["reset_ms"] >= 0

    tree = engine.tool("s1", "computer_observe", {"mode": "tree"}, generation=generation)
    assert "e1 frame" in tree and "focus: e2" in tree
    diff = engine.tool("s1", "computer_observe", {"mode": "diff"}, generation=generation)
    assert diff.startswith("~ e2")  # second observe goes through observe_diff

    assert (
        engine.tool("s1", "computer_click", {"target": "e2"}, generation=generation) == "clicked e2"
    )
    assert (
        engine.tool("s1", "computer_click", {"target": "10,20"}, generation=generation)
        == "clicked at (10, 20)"
    )
    engine.tool("s1", "computer_type_text", {"text": "hi"}, generation=generation)
    engine.tool("s1", "computer_key", {"keys": "Return"}, generation=generation)
    engine.tool("s1", "computer_scroll", {"dy": -3}, generation=generation)

    out = json.loads(engine.tool("s1", "computer_exec", {"command": "ls"}, generation=generation))
    assert out["returncode"] == 0 and len(out["stdout"]) == 2000  # truncated

    shot = engine.tool("s1", "computer_screenshot", {}, generation=generation)
    assert "1280x800" in shot and "computer_observe" in shot

    assert engine.verify("s1", generation=generation) == 1.0
    with pytest.raises(CuaGymError, match="seed_session must run first"):
        engine.tool("s1", "computer_observe", {"mode": "tree"}, generation=generation)


def test_unknown_tool_and_unknown_task(tmp_path):
    engine, task = make_engine(tmp_path)
    with pytest.raises(CuaGymError, match="unknown task_id"):
        engine.seed("s1", "nope", generation=None)
    seeded = engine.seed("s1", task.task_id, generation=None)
    with pytest.raises(CuaGymError, match="unknown tool"):
        engine.tool("s1", "computer_frobnicate", {}, generation=seeded["generation"])


def test_unknown_task_from_raising_task_source_is_normalized(tmp_path):
    class RaisingTaskSource:
        def get(self, task_id):
            raise KeyError(task_id)

    engine = ShinkenComputerEngine(
        provider=FakeProvider(),
        tasks=RaisingTaskSource(),
        env_factory=FakeEnv,
        reap_interval_s=0,
    )
    with pytest.raises(CuaGymError, match="unknown task_id"):
        engine.seed("s1", "missing", generation=None)
    engine.close()


def test_golden_is_built_once_and_shared(tmp_path):
    engine, task = make_engine(tmp_path)
    engine.seed("s1", task.task_id, generation=None)
    engine.seed("s2", task.task_id, generation=None)
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
    engine.seed("s1", task.task_id, generation=None)
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
        provider=FakeProvider(),
        tasks={task.task_id: task},
        env_factory=FailingPostForkEnv,
        reap_interval_s=0,
    )
    with pytest.raises(RuntimeError, match="post-fork setup failed"):
        engine.seed("s1", task.task_id, generation=None)
    assert created[0].closed
    assert "s1" not in engine._rollouts
    engine.close()


def test_reseed_replaces_replica_and_close_reaps(tmp_path):
    engine, task = make_engine(tmp_path)
    first = engine.seed("s1", task.task_id, generation=None)
    first_env = engine._rollouts["s1"].env
    engine.seed("s1", task.task_id, generation=first["generation"])
    assert first_env.closed  # the old replica was torn down
    engine.close()
    assert not engine._rollouts


def test_webserver_shutdown_closes_engine_resources():
    class App:
        def add_event_handler(self, event, handler):
            setattr(self, event, handler)

    class Engine:
        started = False
        closed = False

        def start_maintenance(self):
            self.started = True

        def close(self):
            self.closed = True

    app, engine = App(), Engine()
    _install_engine_shutdown(app, engine)
    app.startup()
    asyncio.run(app.shutdown())
    assert engine.started and engine.closed


def test_webserver_shutdown_wraps_lifespan_when_add_event_handler_is_gone():
    """Newer Starlette removed ``add_event_handler`` (lifespan-only apps); the engine
    lifetime binding must wrap the router's lifespan context instead of crashing at
    server startup with AttributeError."""
    import contextlib

    events: list[str] = []

    @contextlib.asynccontextmanager
    async def base_lifespan(app):
        events.append("base-enter")
        yield {"base": True}
        events.append("base-exit")

    class Router:
        def __init__(self):
            self.lifespan_context = base_lifespan

    class App:  # deliberately NO add_event_handler attribute
        def __init__(self):
            self.router = Router()

    class Engine:
        def start_maintenance(self):
            events.append("engine-start")

        def close(self):
            events.append("engine-close")

    app, engine = App(), Engine()
    _install_engine_shutdown(app, engine)

    async def run():
        async with app.router.lifespan_context(app) as state:
            assert state == {"base": True}
            assert "engine-start" in events

    asyncio.run(run())
    assert events == ["engine-start", "base-enter", "base-exit", "engine-close"]


def test_current_generation_recovers_the_active_rollout(tmp_path):
    """When the client session cookie doesn't thread the generation (multi-server
    SessionMiddleware cookie collision), the engine can recover the active rollout's
    generation from the reliably-threaded session_id."""
    engine, task = make_engine(tmp_path)
    try:
        assert engine.current_generation("s1") is None  # nothing seeded yet
        seeded = engine.seed("s1", task.task_id, generation=None)
        assert engine.current_generation("s1") == seeded["generation"]
        # a distinct session is independent
        assert engine.current_generation("other") is None
    finally:
        engine.close()


def test_current_generation_lets_tool_and_verify_proceed_without_the_cookie(tmp_path):
    """The recovered generation is the SAME value seed issued, so tool/verify calls
    that pass it succeed exactly as a cookie-threaded generation would."""
    engine, task = make_engine(tmp_path)
    try:
        engine.seed("s1", task.task_id, generation=None)
        gen = engine.current_generation("s1")
        assert isinstance(gen, int)
        # tool + verify accept the recovered generation (no CuaGymError)
        engine.tool("s1", "computer_observe", {"mode": "tree"}, generation=gen)
        engine.verify("s1", generation=gen)  # tears the replica down
        assert engine.current_generation("s1") is None  # gone after verify
    finally:
        engine.close()


def test_scorer_crash_raises_by_default(tmp_path):
    """Strict default (eval contract): a reward.py that exits non-zero is a typed fault."""
    engine, task = make_engine(tmp_path, task_config={"_scorer_raises": True})
    try:
        engine.seed("s1", task.task_id, generation=None)
        gen = engine.current_generation("s1")
        with pytest.raises(CuaGymError, match="reward.py failed"):
            engine.verify("s1", generation=gen)
    finally:
        engine.close()


def test_scorer_crash_scores_configured_reward_for_rl(tmp_path):
    """RL-collection resilience: a badly-authored corpus task whose reward.py crashes on
    the unsolved state (common in CUA-Gym: `assert reward == 1.0` self-tests) must NOT
    abort the whole batch. With scorer_error_reward set, a scorer crash yields that
    reward and the replica is still torn down."""
    engine, task = make_engine(
        tmp_path, scorer_error_reward=0.0, task_config={"_scorer_raises": True}
    )
    try:
        engine.seed("s1", task.task_id, generation=None)
        gen = engine.current_generation("s1")
        assert engine.verify("s1", generation=gen) == 0.0
        assert engine.current_generation("s1") is None  # rollout torn down, batch continues
    finally:
        engine.close()


def test_idle_rollouts_are_reaped(tmp_path):
    engine, task = make_engine(tmp_path, idle_ttl_s=0.01)
    engine.seed("s1", task.task_id, generation=None)
    stale_env = engine._rollouts["s1"].env
    engine._rollouts["s1"].last_used -= 10
    engine.maintenance_once()
    assert "s1" not in engine._rollouts and stale_env.closed


def test_same_session_seeds_are_serialized_without_replica_leak(tmp_path):
    created = []
    first_reset_entered = threading.Event()
    release_first_reset = threading.Event()

    class BlockingFirstResetEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.index = len(created)
            created.append(self)

        def reset(self):
            if self.index == 0:
                first_reset_entered.set()
                assert release_first_reset.wait(2)
            return super().reset()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=BlockingFirstResetEnv,
        reap_interval_s=0,
    )
    first_thread, first_done, first_result = start_call(
        lambda: engine.seed("s1", task.task_id, generation=None)
    )
    assert first_reset_entered.wait(1)

    second_started = threading.Event()

    def second_seed():
        second_started.set()
        return engine.seed("s1", task.task_id, generation=None)

    second_thread, second_done, second_result = start_call(second_seed)
    assert second_started.wait(1)
    assert not second_done.wait(0.05)
    assert len(created) == 1  # second seed cannot construct behind the same-session gate

    release_first_reset.set()
    join_call(first_thread, first_done)
    join_call(second_thread, second_done)
    assert isinstance(first_result[0], dict) and isinstance(second_result[0], dict)
    assert len(created) == 1
    assert first_result[0]["generation"] == second_result[0]["generation"]
    assert engine._rollouts["s1"].env is created[0]
    assert created[0].close_calls == 0
    engine.close()


def test_failed_reseed_preserves_previous_generation(tmp_path):
    created = []

    class FailingSecondResetEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.index = len(created)
            created.append(self)

        def reset(self):
            if self.index == 1:
                raise RuntimeError("replacement reset failed")
            return super().reset()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=FailingSecondResetEnv,
        reap_interval_s=0,
    )
    first = engine.seed("s1", task.task_id, generation=None)
    engine._rollouts["s1"].last_used -= 10
    with pytest.raises(RuntimeError, match="replacement reset failed"):
        engine.seed("s1", task.task_id, generation=first["generation"])

    assert engine._rollouts["s1"].env is created[0]
    assert engine._rollouts["s1"].generation == first["generation"]
    assert not created[0].closed
    assert created[1].close_calls == 1
    engine.close()


def test_verify_and_reseed_are_linearized_by_generation(tmp_path):
    evaluate_entered = threading.Event()
    release_evaluate = threading.Event()
    created = []

    class BlockingEvaluateEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def evaluate(self):
            evaluate_entered.set()
            assert release_evaluate.wait(2)
            return super().evaluate()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=BlockingEvaluateEnv,
        reap_interval_s=0,
    )
    initial = engine.seed("s1", task.task_id, generation=None)
    verify_thread, verify_done, verify_result = start_call(
        lambda: engine.verify("s1", generation=initial["generation"])
    )
    assert evaluate_entered.wait(1)

    seed_started = threading.Event()

    def reseed():
        seed_started.set()
        return engine.seed("s1", task.task_id, generation=initial["generation"])

    seed_thread, seed_done, seed_result = start_call(reseed)
    assert seed_started.wait(1)
    assert not seed_done.wait(0.05)
    assert not created[0].closed

    release_evaluate.set()
    join_call(verify_thread, verify_done)
    join_call(seed_thread, seed_done)
    assert verify_result == [1.0]
    assert isinstance(seed_result[0], dict)
    assert created[0].close_calls == 1
    assert engine._rollouts["s1"].env is created[1]
    assert not created[1].closed
    engine.close()


def test_active_tool_is_not_reaped_and_refreshes_idle_deadline(tmp_path):
    observe_entered = threading.Event()
    release_observe = threading.Event()

    class BlockingSess(FakeSess):
        def observe(self, structured=False, settle_ms=None, **kwargs):
            observe_entered.set()
            assert release_observe.wait(2)
            return super().observe(structured=structured, settle_ms=settle_ms, **kwargs)

    class BlockingToolEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._sess = BlockingSess(self.log)

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=BlockingToolEnv,
        idle_ttl_s=0.01,
        reap_interval_s=0,
    )
    first = engine.seed("s1", task.task_id, generation=None)
    second_session = next(
        f"s2-{index}"
        for index in range(1000)
        if engine._session_lock(f"s2-{index}") is not engine._session_lock("s1")
    )
    engine.seed(second_session, task.task_id, generation=None)
    rollout = engine._rollouts["s1"]
    other = engine._rollouts[second_session]
    other.last_used -= 10
    tool_thread, tool_done, tool_result = start_call(
        lambda: engine.tool(
            "s1", "computer_observe", {"mode": "tree"}, generation=first["generation"]
        )
    )
    assert observe_entered.wait(1)
    rollout.last_used -= 10  # emulate a call whose execution exceeds the configured TTL

    maintenance_started = threading.Event()

    def maintain():
        maintenance_started.set()
        return engine.maintenance_once()

    reaper_thread, reaper_done, reaper_result = start_call(maintain)
    assert maintenance_started.wait(1)
    join_call(reaper_thread, reaper_done)
    assert not rollout.env.closed
    assert other.env.closed

    release_observe.set()
    join_call(tool_thread, tool_done)
    assert "e1 frame" in tool_result[0]
    assert reaper_result == [{"rollouts_reaped": 1, "goldens_evicted": 0}]
    assert engine._rollouts["s1"] is rollout
    assert second_session not in engine._rollouts
    assert not rollout.env.closed
    engine.close()


def test_background_reaper_collects_without_a_later_seed(tmp_path):
    engine, task = make_engine(tmp_path, idle_ttl_s=0.01, reap_interval_s=0.005)
    assert engine._reaper_thread is None  # core construction is fork-safe and thread-free
    engine.start_maintenance()
    engine.seed("s1", task.task_id, generation=None)
    rollout = engine._rollouts["s1"]
    rollout.last_used -= 10

    assert rollout.env.closed_event.wait(1), "background reaper did not collect idle rollout"
    assert "s1" not in engine._rollouts
    thread = engine._reaper_thread
    engine.close()
    assert thread is not None and not thread.is_alive()


def test_close_is_a_barrier_and_rejects_new_work(tmp_path):
    reset_entered = threading.Event()
    release_reset = threading.Event()
    created = []

    class BlockingResetEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def reset(self):
            reset_entered.set()
            assert release_reset.wait(2)
            return super().reset()

    provider = FakeProvider()
    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        provider,
        {task.task_id: task},
        env_factory=BlockingResetEnv,
        reap_interval_s=0.01,
    )
    engine.start_maintenance()
    seed_thread, seed_done, seed_result = start_call(
        lambda: engine.seed("s1", task.task_id, generation=None)
    )
    assert reset_entered.wait(1)

    close_started = threading.Event()

    def close_engine():
        close_started.set()
        engine.close()

    close_thread, close_done, close_result = start_call(close_engine)
    assert close_started.wait(1)
    assert not close_done.wait(0.05)
    assert not created[0].closed

    second_close_thread, second_close_done, second_close_result = start_call(engine.close)
    release_reset.set()
    join_call(seed_thread, seed_done)
    join_call(close_thread, close_done)
    join_call(second_close_thread, second_close_done)

    assert isinstance(seed_result[0], dict)
    assert close_result == [None] and second_close_result == [None]
    assert created[0].close_calls == 1
    assert not engine._rollouts and not engine._goldens
    assert provider.deleted == [f"snap:{task.task_id}"]
    assert engine._reaper_thread is not None and not engine._reaper_thread.is_alive()
    with pytest.raises(CuaGymError, match="computer engine is closed"):
        engine.seed("s2", task.task_id, generation=None)
    with pytest.raises(CuaGymError, match="computer engine is closed"):
        engine.tool("s1", "computer_observe", {"mode": "tree"}, generation=1)
    with pytest.raises(CuaGymError, match="computer engine is closed"):
        engine.verify("s1", generation=1)
    engine.end("s1", generation=1)  # idempotent even after terminal shutdown


def test_close_join_interrupt_cannot_strand_engine_in_closing(tmp_path):
    class InterruptingThread:
        def join(self):
            raise KeyboardInterrupt("synthetic join interrupt")

    engine, task = make_engine(tmp_path)
    engine.seed("s1", task.task_id, generation=None)
    engine._reaper_thread = InterruptingThread()

    with pytest.raises(KeyboardInterrupt, match="synthetic join interrupt"):
        engine.close()
    assert engine._state == "closed"
    assert engine._reaper_stop.is_set()
    assert "s1" in engine._rollouts

    engine._reaper_thread = None
    engine.close()
    assert not engine._rollouts and not engine._goldens


def test_stale_generation_cannot_touch_reseeded_rollout(tmp_path):
    engine, task = make_engine(tmp_path)
    first = engine.seed("s1", task.task_id, generation=None)
    second = engine.seed("s1", task.task_id, generation=first["generation"])
    current = engine._rollouts["s1"]

    with pytest.raises(CuaGymError, match="stale rollout generation"):
        engine.tool("s1", "computer_observe", {"mode": "tree"}, generation=first["generation"])
    with pytest.raises(CuaGymError, match="stale rollout generation"):
        engine.verify("s1", generation=first["generation"])
    engine.end("s1", generation=first["generation"])
    assert engine._rollouts["s1"] is current and not current.env.closed

    assert "e1 frame" in engine.tool(
        "s1", "computer_observe", {"mode": "tree"}, generation=second["generation"]
    )
    assert engine.verify("s1", generation=second["generation"]) == 1.0


def test_same_task_concurrent_seeds_singleflight_golden_build(tmp_path):
    build_entered = threading.Event()
    release_build = threading.Event()
    build_calls = 0

    class BlockingBuildEnv(FakeEnv):
        def _build_golden(self):
            nonlocal build_calls
            build_calls += 1
            build_entered.set()
            assert release_build.wait(2)
            return super()._build_golden()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=BlockingBuildEnv,
        reap_interval_s=0,
    )
    first_thread, first_done, first_result = start_call(
        lambda: engine.seed("s1", task.task_id, generation=None)
    )
    assert build_entered.wait(1)
    second_thread, second_done, second_result = start_call(
        lambda: engine.seed("s2", task.task_id, generation=None)
    )
    assert not second_done.wait(0.05)
    assert build_calls == 1

    release_build.set()
    join_call(first_thread, first_done)
    join_call(second_thread, second_done)
    assert isinstance(first_result[0], dict) and isinstance(second_result[0], dict)
    assert build_calls == 1
    assert set(engine._rollouts) == {"s1", "s2"}
    engine.close()


def test_golden_cache_evicts_lru_and_deletes_each_snapshot_once(tmp_path):
    provider = FakeProvider()
    tasks = {task_id: make_task(tmp_path, task_id) for task_id in ("t1", "t2", "t3")}
    engine = ShinkenComputerEngine(
        provider,
        tasks,
        env_factory=FakeEnv,
        max_goldens=2,
        golden_ttl_s=0,
        reap_interval_s=0,
    )
    for index, task_id in enumerate(("t1", "t2"), start=1):
        seeded = engine.seed(f"s{index}", task_id, generation=None)
        engine.end(f"s{index}", generation=seeded["generation"])
    engine._goldens["t1"].last_used -= 10

    seeded = engine.seed("s3", "t3", generation=None)
    engine.end("s3", generation=seeded["generation"])
    assert set(engine._goldens) == {"t2", "t3"}
    assert provider.deleted == ["snap:t1"]

    engine.close()
    assert sorted(provider.deleted) == ["snap:t1", "snap:t2", "snap:t3"]
    assert all(provider.deleted.count(snapshot) == 1 for snapshot in provider.deleted)


def test_golden_in_reset_is_not_evicted_under_cache_pressure(tmp_path):
    reset_entered = threading.Event()
    release_reset = threading.Event()
    provider = FakeProvider()

    class BlockingT1ResetEnv(FakeEnv):
        def reset(self):
            if self.task.task_id == "t1":
                reset_entered.set()
                assert release_reset.wait(2)
            return super().reset()

    first_task = make_task(tmp_path, "t1")
    engine = ShinkenComputerEngine(
        provider,
        {"t1": first_task},
        env_factory=BlockingT1ResetEnv,
        max_goldens=0,
        golden_ttl_s=0,
        reap_interval_s=0,
    )
    second_task_id = next(
        f"t2-{index}"
        for index in range(1000)
        if engine._golden_lock(f"t2-{index}") is not engine._golden_lock("t1")
    )
    engine.tasks = {"t1": first_task, second_task_id: make_task(tmp_path, second_task_id)}
    second_session = next(
        f"s2-{index}"
        for index in range(1000)
        if engine._session_lock(f"s2-{index}") is not engine._session_lock("s1")
    )

    first_thread, first_done, first_result = start_call(
        lambda: engine.seed("s1", "t1", generation=None)
    )
    assert reset_entered.wait(1)
    second_thread, second_done, second_result = start_call(
        lambda: engine.seed(second_session, second_task_id, generation=None)
    )
    join_call(second_thread, second_done)
    assert isinstance(second_result[0], dict)
    assert f"snap:{second_task_id}" in provider.deleted
    assert "snap:t1" not in provider.deleted

    release_reset.set()
    join_call(first_thread, first_done)
    assert isinstance(first_result[0], dict)
    assert provider.deleted.count("snap:t1") == 1
    engine.close()


def test_seed_generation_cas_is_idempotent_and_rejects_stale_request(tmp_path):
    created = []

    class CountingEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(), {task.task_id: task}, env_factory=CountingEnv, reap_interval_s=0
    )
    first = engine.seed("s1", task.task_id, generation=None)
    retry = engine.seed("s1", task.task_id, generation=None)
    assert retry["generation"] == first["generation"]
    assert len(created) == 1

    second = engine.seed("s1", task.task_id, generation=first["generation"])
    assert second["generation"] != first["generation"]
    assert len(created) == 2 and created[0].close_calls == 1
    with pytest.raises(CuaGymError, match="stale rollout generation"):
        engine.seed("s1", task.task_id, generation=first["generation"])
    assert engine._rollouts["s1"].generation == second["generation"]
    assert not created[1].closed
    engine.close()


def test_direct_engine_operations_require_generation_token(tmp_path):
    engine, task = make_engine(tmp_path)
    seeded = engine.seed("s1", task.task_id, generation=None)
    with pytest.raises(TypeError, match="generation"):
        engine.tool("s1", "computer_observe", {"mode": "tree"})
    with pytest.raises(TypeError, match="generation"):
        engine.verify("s1")
    with pytest.raises(TypeError, match="generation"):
        engine.end("s1")
    assert engine.verify("s1", generation=seeded["generation"]) == 1.0


def test_end_rejects_bool_and_float_generation_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr("shinken.integrations.nemo_gym.secrets.randbits", lambda bits: 1)
    engine, task = make_engine(tmp_path)
    seeded = engine.seed("s1", task.task_id, generation=None)
    assert seeded["generation"] == 1

    engine.end("s1", generation=True)
    engine.end("s1", generation=1.0)
    assert "s1" in engine._rollouts
    engine.end("s1", generation=1)
    assert "s1" not in engine._rollouts
    engine.close()


def test_snapshot_delete_failure_remains_owned_and_retries(tmp_path):
    class FlakyDeleteProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.attempts: list[str] = []

        def delete_snapshot(self, snapshot):
            self.attempts.append(snapshot)
            if len(self.attempts) == 1:
                raise RuntimeError("transient delete failure")
            super().delete_snapshot(snapshot)

    provider = FlakyDeleteProvider()
    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        provider,
        {task.task_id: task},
        env_factory=FakeEnv,
        max_goldens=0,
        golden_ttl_s=0,
        reap_interval_s=0,
    )
    seeded = engine.seed("s1", task.task_id, generation=None)
    assert provider.attempts == ["snap:t1"]
    assert engine._pending_snapshot_deletes == ["snap:t1"]

    engine.maintenance_once()
    assert provider.attempts == ["snap:t1", "snap:t1"]
    assert provider.deleted == ["snap:t1"]
    assert not engine._pending_snapshot_deletes
    engine.end("s1", generation=seeded["generation"])
    engine.close()


def test_close_surfaces_permanent_cleanup_failure_and_allows_retry(tmp_path):
    class ToggleDeleteProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.failing = True
            self.attempts = 0

        def delete_snapshot(self, snapshot):
            self.attempts += 1
            if self.failing:
                raise RuntimeError("persistent delete failure")
            super().delete_snapshot(snapshot)

    provider = ToggleDeleteProvider()
    engine, task = make_engine(tmp_path, provider=provider)
    engine.seed("s1", task.task_id, generation=None)
    with pytest.raises(CuaGymError, match="cleanup pending"):
        engine.close()
    assert engine._state == "closed"
    assert task.task_id in engine._goldens
    assert not engine._pending_snapshot_deletes

    provider.failing = False
    engine.close()
    assert provider.deleted == [f"snap:{task.task_id}"]
    assert not engine._pending_snapshot_deletes and not engine._goldens


def test_cleanup_backpressure_blocks_new_seed_at_high_water(tmp_path):
    class ToggleDeleteProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.failing = True

        def delete_snapshot(self, snapshot):
            if self.failing:
                raise RuntimeError("persistent delete failure")
            super().delete_snapshot(snapshot)

    created = []

    class CountingEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    provider = ToggleDeleteProvider()
    tasks = {task_id: make_task(tmp_path, task_id) for task_id in ("t1", "t2")}
    engine = ShinkenComputerEngine(
        provider,
        tasks,
        env_factory=CountingEnv,
        max_goldens=0,
        golden_ttl_s=0,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("s1", "t1", generation=None)
    with pytest.raises(CuaGymError, match="cleanup backlog is at capacity"):
        engine.seed("s2", "t2", generation=None)
    assert len(created) == 1
    assert set(engine._rollouts) == {"s1"}
    assert engine._pending_snapshot_deletes == ["snap:t1"]

    provider.failing = False
    engine.maintenance_once()
    engine.seed("s2", "t2", generation=None)
    assert len(created) == 2 and set(engine._rollouts) == {"s1", "s2"}
    engine.close()


def test_cleanup_retry_inflight_still_applies_backpressure(tmp_path):
    class BlockingDeleteProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.failing = True

        def delete_snapshot(self, snapshot):
            self.entered.set()
            assert self.release.wait(2), "test did not release blocked cleanup"
            if self.failing:
                raise RuntimeError("persistent delete failure")
            super().delete_snapshot(snapshot)

    created = []

    class CountingEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    provider = BlockingDeleteProvider()
    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        provider,
        {task.task_id: task},
        env_factory=CountingEnv,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine._pending_snapshot_deletes.append("snap:pending")
    thread, done, result = start_call(engine.maintenance_once)
    assert provider.entered.wait(2), "cleanup retry did not start"

    with pytest.raises(CuaGymError, match="cleanup backlog is at capacity"):
        engine.seed("s1", task.task_id, generation=None)
    assert not created
    with engine._lock:
        assert engine._cleanup_pressure_locked() == engine.max_pending_cleanup

    provider.release.set()
    join_call(thread, done)
    assert result == [{"rollouts_reaped": 0, "goldens_evicted": 0}]
    assert engine._cleanup_inflight == 0
    assert engine._pending_snapshot_deletes == ["snap:pending"]

    provider.failing = False
    engine.close()


def test_concurrent_golden_eviction_reserves_capacity_before_drain(tmp_path, monkeypatch):
    tasks = {task_id: make_task(tmp_path, task_id) for task_id in ("t1", "t2")}
    engine = ShinkenComputerEngine(
        FakeProvider(),
        tasks,
        env_factory=FakeEnv,
        max_goldens=2,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("s1", "t1", generation=None)
    engine.seed("s2", "t2", generation=None)
    engine.max_goldens = 0

    drain = engine._drain_snapshot_deletes
    both_selected = threading.Barrier(2)
    monkeypatch.setattr(engine, "_drain_snapshot_deletes", lambda: both_selected.wait(timeout=2))
    first_thread, first_done, first_result = start_call(engine._evict_goldens)
    second_thread, second_done, second_result = start_call(engine._evict_goldens)
    join_call(first_thread, first_done)
    join_call(second_thread, second_done)

    assert sorted(first_result + second_result) == [0, 1]
    with engine._lock:
        assert len(engine._pending_snapshot_deletes) == 1
        assert len(engine._goldens) == 1
        assert engine._cleanup_pressure_locked() == engine.max_pending_cleanup

    monkeypatch.setattr(engine, "_drain_snapshot_deletes", drain)
    engine.close()


def test_terminal_close_bounds_failed_snapshot_cleanup(tmp_path):
    class ToggleDeleteProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.failing = False

        def delete_snapshot(self, snapshot):
            if self.failing:
                raise RuntimeError("persistent delete failure")
            super().delete_snapshot(snapshot)

    provider = ToggleDeleteProvider()
    tasks = {task_id: make_task(tmp_path, task_id) for task_id in ("t1", "t2")}
    engine = ShinkenComputerEngine(
        provider,
        tasks,
        env_factory=FakeEnv,
        max_goldens=2,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("s1", "t1", generation=None)
    engine.seed("s2", "t2", generation=None)

    provider.failing = True
    with pytest.raises(CuaGymError, match="2 retained golden"):
        engine.close()
    with engine._lock:
        assert engine._cleanup_pressure_locked() == 0
        assert not engine._pending_snapshot_deletes
        assert len(engine._goldens) == 2
        assert not engine._rollouts

    provider.failing = False
    engine.close()
    assert sorted(provider.deleted) == ["snap:t1", "snap:t2"]
    assert not engine._pending_snapshot_deletes and not engine._goldens


def test_terminal_close_bounds_failed_rollout_cleanup(tmp_path):
    should_fail = {"value": False}

    class ToggleCloseEnv(FakeEnv):
        def close(self):
            if should_fail["value"]:
                raise RuntimeError("persistent close failure")
            super().close()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=ToggleCloseEnv,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("s1", task.task_id, generation=None)
    engine.seed("s2", task.task_id, generation=None)

    should_fail["value"] = True
    with pytest.raises(CuaGymError, match="2 retained rollout"):
        engine.close(delete_goldens=False)
    with engine._lock:
        assert engine._cleanup_pressure_locked() == 0
        assert not engine._pending_env_closes
        assert len(engine._rollouts) == 2

    should_fail["value"] = False
    engine.close(delete_goldens=True)
    assert not engine._pending_env_closes and not engine._rollouts and not engine._goldens


def test_terminal_close_retries_transient_rollout_failures_to_fixed_point(tmp_path):
    created = []

    class FailOnceCloseEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_attempts = 0
            created.append(self)

        def close(self):
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("transient close failure")
            super().close()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=FailOnceCloseEnv,
        max_pending_cleanup=2,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    for index in range(5):
        engine.seed(f"s{index}", task.task_id, generation=None)

    engine.close(delete_goldens=False)
    assert len(created) == 5 and all(env.closed for env in created)
    assert [env.close_attempts for env in created] == [2] * 5
    assert not engine._pending_env_closes and not engine._rollouts
    engine.close(delete_goldens=True)


def test_terminal_poison_rollout_does_not_starve_healthy_rollout(tmp_path):
    created = []

    class PoisonFirstEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.poisoned = not created
            self.close_attempts = 0
            created.append(self)

        def close(self):
            self.close_attempts += 1
            if self.poisoned:
                raise RuntimeError("permanent close failure")
            super().close()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=PoisonFirstEnv,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("bad", task.task_id, generation=None)
    engine.seed("good", task.task_id, generation=None)

    with pytest.raises(CuaGymError, match="1 retained rollout"):
        engine.close(delete_goldens=False)
    assert created[0].close_attempts == 2
    assert created[1].close_attempts == 1 and created[1].closed
    assert set(engine._rollouts) == {"bad"}
    assert not engine._pending_env_closes

    created[0].poisoned = False
    engine.close(delete_goldens=True)
    assert not engine._rollouts and not engine._goldens


def test_terminal_poison_golden_does_not_starve_healthy_golden(tmp_path):
    class PoisonFirstProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.poisoned = True
            self.attempts: dict[str, int] = {}

        def delete_snapshot(self, snapshot):
            self.attempts[snapshot] = self.attempts.get(snapshot, 0) + 1
            if self.poisoned and snapshot == "snap:t1":
                raise RuntimeError("permanent delete failure")
            super().delete_snapshot(snapshot)

    provider = PoisonFirstProvider()
    tasks = {task_id: make_task(tmp_path, task_id) for task_id in ("t1", "t2")}
    engine = ShinkenComputerEngine(
        provider,
        tasks,
        env_factory=FakeEnv,
        max_goldens=2,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("s1", "t1", generation=None)
    engine.seed("s2", "t2", generation=None)

    with pytest.raises(CuaGymError, match="1 retained golden"):
        engine.close()
    assert provider.attempts == {"snap:t1": 2, "snap:t2": 1}
    assert provider.deleted == ["snap:t2"]
    assert set(engine._goldens) == {"t1"}
    assert not engine._pending_snapshot_deletes

    provider.poisoned = False
    engine.close()
    assert sorted(provider.deleted) == ["snap:t1", "snap:t2"]
    assert not engine._goldens


def test_reaper_stops_before_cleanup_backlog_exceeds_high_water(tmp_path):
    should_fail = {"value": True}

    class ToggleCloseEnv(FakeEnv):
        def close(self):
            if should_fail["value"]:
                raise RuntimeError("persistent close failure")
            super().close()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=ToggleCloseEnv,
        idle_ttl_s=0.01,
        max_pending_cleanup=1,
        cleanup_retry_batch=1,
        reap_interval_s=0,
    )
    engine.seed("s1", task.task_id, generation=None)
    second_session = next(
        f"s2-{index}"
        for index in range(1000)
        if engine._session_lock(f"s2-{index}") is not engine._session_lock("s1")
    )
    engine.seed(second_session, task.task_id, generation=None)
    engine._rollouts["s1"].last_used -= 10
    engine._rollouts[second_session].last_used -= 10

    result = engine.maintenance_once()
    assert result["rollouts_reaped"] == 1
    assert len(engine._pending_env_closes) == 1
    assert len(engine._rollouts) == 1

    should_fail["value"] = False
    engine.close()


def test_rollout_close_failure_remains_owned_and_retries(tmp_path):
    class FlakyCloseEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_attempts = 0

        def close(self):
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("transient close failure")
            super().close()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(), {task.task_id: task}, env_factory=FlakyCloseEnv, reap_interval_s=0
    )
    seeded = engine.seed("s1", task.task_id, generation=None)
    env = engine._rollouts["s1"].env
    engine.end("s1", generation=seeded["generation"])
    assert engine._pending_env_closes == [env]

    engine.maintenance_once()
    assert env.closed and env.close_attempts == 2
    assert not engine._pending_env_closes
    engine.close()


def test_rollout_close_interrupt_remains_owned_before_propagating(tmp_path):
    class InterruptOnceCloseEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_attempts = 0

        def close(self):
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise KeyboardInterrupt("synthetic interrupt")
            super().close()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=InterruptOnceCloseEnv,
        reap_interval_s=0,
    )
    seeded = engine.seed("s1", task.task_id, generation=None)
    env = engine._rollouts["s1"].env

    with pytest.raises(KeyboardInterrupt, match="synthetic interrupt"):
        engine.end("s1", generation=seeded["generation"])
    assert "s1" not in engine._rollouts
    assert engine._pending_env_closes == [env]

    engine.maintenance_once()
    assert env.closed and env.close_attempts == 2
    assert not engine._pending_env_closes
    engine.close()


def test_generation_epoch_does_not_alias_across_engine_restart(tmp_path, monkeypatch):
    epochs = iter((111, 222))
    monkeypatch.setattr("shinken.integrations.nemo_gym.secrets.randbits", lambda bits: next(epochs))
    task = make_task(tmp_path)

    first_engine = ShinkenComputerEngine(
        FakeProvider(), {task.task_id: task}, env_factory=FakeEnv, reap_interval_s=0
    )
    first = first_engine.seed("s1", task.task_id, generation=None)
    first_engine.close()

    second_engine = ShinkenComputerEngine(
        FakeProvider(), {task.task_id: task}, env_factory=FakeEnv, reap_interval_s=0
    )
    second = second_engine.seed("s1", task.task_id, generation=first["generation"])
    assert (first["generation"], second["generation"]) == (111, 222)
    with pytest.raises(CuaGymError, match="stale rollout generation"):
        second_engine.tool(
            "s1", "computer_observe", {"mode": "tree"}, generation=first["generation"]
        )
    second_engine.close()


def test_generation_entropy_failure_happens_before_environment_creation(tmp_path, monkeypatch):
    created = []

    class CountingEnv(FakeEnv):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    def fail_entropy(_bits):
        raise RuntimeError("entropy unavailable")

    monkeypatch.setattr("shinken.integrations.nemo_gym.secrets.randbits", fail_entropy)
    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(), {task.task_id: task}, env_factory=CountingEnv, reap_interval_s=0
    )
    with pytest.raises(RuntimeError, match="entropy unavailable"):
        engine.seed("s1", task.task_id, generation=None)
    assert not created and not engine._rollouts and not engine._goldens
    assert engine._cleanup_reservations == 0
    engine.close()


def test_rollout_idle_age_starts_after_reset_completes(tmp_path, monkeypatch):
    now = {"value": 0.0}
    monkeypatch.setattr("shinken.integrations.nemo_gym.time.monotonic", lambda: now["value"])

    class ClockAdvancingResetEnv(FakeEnv):
        def reset(self):
            now["value"] += 100.0
            return super().reset()

    task = make_task(tmp_path)
    engine = ShinkenComputerEngine(
        FakeProvider(),
        {task.task_id: task},
        env_factory=ClockAdvancingResetEnv,
        idle_ttl_s=10,
        reap_interval_s=0,
    )
    seeded = engine.seed("s1", task.task_id, generation=None)
    assert engine._rollouts["s1"].last_used == 100.0
    assert engine.maintenance_once()["rollouts_reaped"] == 0
    engine.end("s1", generation=seeded["generation"])
    engine.close()


def test_provider_cleanup_and_lifetime_config_fail_closed():
    with pytest.raises(ValueError, match="delete_snapshot"):
        ShinkenComputerEngine(object(), {})
    for name in ("idle_ttl_s", "golden_ttl_s", "reap_interval_s"):
        with pytest.raises(ValueError, match=rf"{name} must be finite"):
            ShinkenComputerEngine(FakeProvider(), {}, **{name: float("nan")})
        with pytest.raises(ValueError, match=rf"{name} must be >= 0"):
            ShinkenComputerEngine(FakeProvider(), {}, **{name: -1})
    with pytest.raises(ValueError, match="max_pending_cleanup must be >= 1"):
        ShinkenComputerEngine(FakeProvider(), {}, max_pending_cleanup=0)
    with pytest.raises(ValueError, match="cleanup_retry_batch must be >= 1"):
        ShinkenComputerEngine(FakeProvider(), {}, cleanup_retry_batch=0)


def test_example_app_wires_cleanup_environment_knobs(monkeypatch):
    import shinken.integrations.nemo_gym as nemo_integration

    monkeypatch.setattr(
        nemo_integration,
        "build_resources_server_cls",
        lambda _factory: types.SimpleNamespace(run_webserver=lambda: None),
    )
    app_path = Path(__file__).resolve().parents[3] / "examples" / "nemo_gym" / "app.py"
    spec = importlib.util.spec_from_file_location("shinken_test_nemo_example_app", app_path)
    assert spec is not None and spec.loader is not None
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    monkeypatch.setattr(app, "DockerLocalProvider", lambda **_kwargs: FakeProvider())
    monkeypatch.setattr(app, "CuaGymTaskSource", lambda _root: {})

    knobs = (
        "CUA_GYM_TASKS",
        "SHINKEN_IMAGE",
        "SHINKEN_STATE_FIDELITY",
        "SHINKEN_IDLE_TTL_S",
        "SHINKEN_MAX_GOLDENS",
        "SHINKEN_GOLDEN_TTL_S",
        "SHINKEN_REAP_INTERVAL_S",
        "SHINKEN_MAX_PENDING_CLEANUP",
        "SHINKEN_CLEANUP_RETRY_BATCH",
    )
    for knob in knobs:
        monkeypatch.delenv(knob, raising=False)
    default_engine = app.engine_factory(None)
    assert default_engine.max_pending_cleanup == 64
    assert default_engine.cleanup_retry_batch == 16
    default_engine.close()

    monkeypatch.setenv("SHINKEN_MAX_PENDING_CLEANUP", "7")
    monkeypatch.setenv("SHINKEN_CLEANUP_RETRY_BATCH", "3")
    configured_engine = app.engine_factory(None)
    assert configured_engine.max_pending_cleanup == 7
    assert configured_engine.cleanup_retry_batch == 3
    configured_engine.close()

    for knob, value in (
        ("SHINKEN_MAX_PENDING_CLEANUP", "0"),
        ("SHINKEN_CLEANUP_RETRY_BATCH", "-1"),
        ("SHINKEN_CLEANUP_RETRY_BATCH", "not-an-integer"),
    ):
        monkeypatch.setenv("SHINKEN_MAX_PENDING_CLEANUP", "7")
        monkeypatch.setenv("SHINKEN_CLEANUP_RETRY_BATCH", "3")
        monkeypatch.setenv(knob, value)
        with pytest.raises(ValueError):
            app.engine_factory(None)


def test_close_can_retain_then_explicitly_delete_goldens(tmp_path):
    provider = FakeProvider()
    engine, task = make_engine(tmp_path, provider=provider)
    seeded = engine.seed("s1", task.task_id, generation=None)
    env = engine._rollouts["s1"].env

    engine.close(delete_goldens=False)
    assert env.closed
    assert task.task_id in engine._goldens
    assert not provider.deleted
    assert engine.maintenance_once() == {"rollouts_reaped": 0, "goldens_evicted": 0}

    engine.close(delete_goldens=True)
    assert provider.deleted == [f"snap:{task.task_id}"]
    assert not engine._goldens and not engine._pending_snapshot_deletes
    engine.end("s1", generation=seeded["generation"])


def test_multi_worker_resources_server_config_is_rejected():
    _require_single_worker({"num_workers": 1})
    _require_single_worker({})
    with pytest.raises(CuaGymError, match="num_workers must be 1"):
        _require_single_worker({"num_workers": 2})
    with pytest.raises(CuaGymError, match="num_workers must be 1"):
        _require_single_worker({"num_workers": "2"})
    with pytest.raises(CuaGymError, match="num_workers must be 1"):
        _require_single_worker(types.SimpleNamespace(num_workers=2))


def test_nemo_adapter_threads_cookie_generation_through_routes(monkeypatch):
    class Model:
        def __init__(self, **values):
            self.__dict__.update(values)

        def model_dump(self):
            return dict(self.__dict__)

    class SimpleResourcesServer:
        def model_post_init(self, context):
            return None

        def setup_webserver(self):
            return None

    class PlainTextResponse:
        def __init__(self, content):
            self.body = content

    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = object
    fastapi.Request = object
    responses = types.ModuleType("fastapi.responses")
    responses.PlainTextResponse = PlainTextResponse
    nemo_gym = types.ModuleType("nemo_gym")
    nemo_gym.__path__ = []
    base = types.ModuleType("nemo_gym.base_resources_server")
    base.BaseSeedSessionRequest = Model
    base.BaseSeedSessionResponse = Model
    base.BaseVerifyRequest = Model
    base.BaseVerifyResponse = Model
    base.SimpleResourcesServer = SimpleResourcesServer
    server_utils = types.ModuleType("nemo_gym.server_utils")
    server_utils.SESSION_ID_KEY = "sid"
    for name, module in {
        "fastapi": fastapi,
        "fastapi.responses": responses,
        "nemo_gym": nemo_gym,
        "nemo_gym.base_resources_server": base,
        "nemo_gym.server_utils": server_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    calls = []

    class Engine:
        def seed(self, session_id, task_id, *, generation):
            calls.append(("seed", session_id, task_id, generation))
            return {"generation": 7 if generation is None else 8}

        def tool(self, session_id, name, arguments, *, generation):
            calls.append(("tool", session_id, name, arguments, generation))
            return "observed"

        def verify(self, session_id, *, generation):
            calls.append(("verify", session_id, generation))
            return 0.75

        def current_generation(self, session_id):
            return None  # no server-side rollout in this cookie-threading fixture

    class Request:
        def __init__(self):
            self.session = {"sid": "session-1"}

        async def json(self):
            return {"mode": "tree"}

    server_cls = build_resources_server_cls(lambda _config: Engine())
    server = object.__new__(server_cls)
    server._engine = Engine()
    request = Request()
    body = Model(responses_create_params={"metadata": {"task_id": "task-1"}})
    asyncio.run(server.seed_session(request, body))
    assert request.session["shinken_rollout_generation"] == 7
    asyncio.run(server.seed_session(request, body))
    assert request.session["shinken_rollout_generation"] == 8

    response = asyncio.run(server._make_tool_route("computer_observe")(request))
    assert response.body == "observed"
    # The tool response re-asserts the generation into the session so Starlette re-emits
    # the Set-Cookie — otherwise the multi-server agent's cookie chain drops it and the
    # NEXT call arrives session-less (the live failure this fixes).
    assert request.session["shinken_rollout_generation"] == 8
    verified = asyncio.run(server.verify(request, Model(request_id="r1")))
    assert verified.reward == 0.75 and verified.request_id == "r1"
    assert request.session["shinken_rollout_generation"] == 8  # verify re-asserts too
    assert calls == [
        ("seed", "session-1", "task-1", None),
        ("seed", "session-1", "task-1", 7),
        ("tool", "session-1", "computer_observe", {"mode": "tree"}, 8),
        ("verify", "session-1", 8),
    ]

    missing_generation = Request()
    with pytest.raises(CuaGymError, match="no Shinken rollout generation"):
        asyncio.run(server._make_tool_route("computer_observe")(missing_generation))
    missing_generation.session["shinken_rollout_generation"] = True
    with pytest.raises(CuaGymError, match="no Shinken rollout generation"):
        asyncio.run(server.verify(missing_generation, Model()))

    # A tool call whose cookie DROPPED the generation but whose engine still owns the
    # rollout recovers it AND re-asserts it into the session, so the Set-Cookie is
    # re-emitted and the next call in the agent's cookie chain carries it again.
    class EngineWithRollout(Engine):
        def current_generation(self, session_id):
            return 99

    healed = build_resources_server_cls(lambda _c: EngineWithRollout())
    server2 = object.__new__(healed)
    server2._engine = EngineWithRollout()
    recovered = Request()  # session has session_id but no generation
    asyncio.run(server2._make_tool_route("computer_observe")(recovered))
    assert recovered.session["shinken_rollout_generation"] == 99


def test_reentrant_close_is_rejected_instead_of_deadlocking(tmp_path):
    engine, _task = make_engine(tmp_path)
    with engine._operation():
        with pytest.raises(CuaGymError, match="active engine operation"):
            engine.close()
    engine.close()
