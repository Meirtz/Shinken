"""OSWorld-as-a-Workload: the `osworld-eval` workload + the thin `scripts/osworld_single.py`
CLI. Exercises model-text -> parse_model_actions -> actuate -> OSWorld-evaluator score with
no VM/model/GPU (fake env + scripted agent)."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

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


# --- runtime injection wiring for `--backend shinken` (mocked: no real docker/ssh/VM) ---


def test_inject_and_actuate_threads_addr_and_token(monkeypatch):
    # inject_shinkend returns (addr, token); the actuator must be built from exactly those.
    from shinken import inject as inj
    from shinken.inject import InjectionResult, InjectionTarget

    monkeypatch.setattr(
        inj, "inject_shinkend", lambda target, binary, *, method: InjectionResult("h:9000", "tok-x")
    )
    seen: dict = {}
    monkeypatch.setattr(
        oe, "make_shinken_actuator", lambda addr=None, token=None: seen.update(a=addr, t=token)
    )
    oe.inject_and_actuate(InjectionTarget(container="c"), "/bin/shinkend", method="docker")
    assert seen == {"a": "h:9000", "t": "tok-x"}


def test_cli_connects_to_running_shinkend_without_injection(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        osw, "make_shinken_actuator", lambda addr=None: seen.setdefault("addr", addr)
    )
    args = argparse.Namespace(inject_method=None, shk_addr="10.0.0.1:8765")
    osw._build_shinken_actuator(args)
    assert seen["addr"] == "10.0.0.1:8765"  # no injection -> connect to the configured addr


def test_cli_injects_via_user_chosen_method(monkeypatch):
    seen: dict = {}

    def fake(target, binary, *, method):
        seen.update(
            container=target.container,
            port=target.port,
            binary=binary,
            method=method,
            remote_bin=target.remote_bin,
            env=dict(target.env),
        )
        return "ACTUATOR"

    monkeypatch.setattr(osw, "inject_and_actuate", fake)
    args = argparse.Namespace(
        inject_method="docker",
        shinkend_binary="/b/shinkend",
        inject_port=8765,
        inject_reachable_addr="127.0.0.1:19000",
        inject_container="osw-vm",
        inject_ssh_host=None,
        inject_ssh_user=None,
        inject_ssh_port=22,
        inject_ssh_key=None,
        inject_controller_url=None,
        inject_remote_bin="/tmp/shinkend",
        inject_display=":0",
        shk_addr="x",
    )
    assert osw._build_shinken_actuator(args) == "ACTUATOR"
    assert seen == {
        "container": "osw-vm",
        "port": 8765,
        "binary": "/b/shinkend",
        "method": "docker",
        "remote_bin": "/tmp/shinkend",
        # the X11 pin reached the injection target (fail-loud on a missing display)
        "env": {"DISPLAY": ":0", "SHINKEND_EXECUTOR": "x11_xtest"},
    }


def test_cli_inject_method_requires_binary():
    # No silent default: choosing a method without a binary is a usage error, not a guess.
    with pytest.raises(SystemExit):
        osw.main(["--inject-method", "docker"])


def test_chat_agent_retries_transient_gateway_errors(monkeypatch):
    # A 502/503 from a hosted model gateway must be retried, not abort the episode.
    import urllib.error
    import urllib.request

    agent = oe.ChatModelAgent("http://m/v1", "k", "model")
    n = {"calls": 0}

    class _Resp:
        def read(self):
            return b'{"choices":[{"message":{"content":"act"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        n["calls"] += 1
        if n["calls"] < 3:
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda *_a: None)
    assert agent.act(None, None, "task", []) == "act"
    assert n["calls"] == 3  # two transient failures, then success


def test_run_episode_tolerates_unactuatable_action_without_crashing():
    # A single unactuatable model output (e.g. raw shell) is skipped; the episode still reaches
    # a scored terminal state instead of crashing.
    class _Boom:
        def step(self, action, pause=0.0):
            raise ValueError("no supported pyautogui call found")

        def close(self):
            pass

    class _Env:
        def _get_obs(self):
            return {"screenshot": None, "accessibility_tree": None}

        def evaluate(self):
            return 0.0

    agent = oe.ScriptedAgent(["```python\npyautogui.click(1, 2)\n```", "```DONE```"])
    res = oe.run_episode(_Env(), agent, _Boom(), "t", max_steps=5)
    assert res["terminal"] == "DONE" and res["steps"] >= 1  # survived the bad action


def test_parity_warnings_flag_deviations_from_upstream_defaults():
    ns = argparse.Namespace(max_steps=15, pause=0.0, observation="a11y_tree")
    assert osw._parity_warnings(ns) == []  # all at upstream defaults → no warnings
    ns2 = argparse.Namespace(max_steps=30, pause=2.0, observation="screenshot")
    warns = osw._parity_warnings(ns2)
    assert any("max_steps=30" in w for w in warns)
    assert any("sleep_after_execution=2.0" in w for w in warns)
    assert any("observation='screenshot'" in w for w in warns)


def test_emit_result_writes_out_file(tmp_path, capsys):
    out = tmp_path / "result.json"
    osw._emit_result({"task": "t", "passed": True, "score": 1.0}, str(out))
    import json as _json

    written = _json.loads(out.read_text())
    assert written["task"] == "t" and written["passed"] is True
    assert '"passed": true' in capsys.readouterr().out  # also printed to stdout


def test_run_episode_propagates_sandbox_death_not_skip():
    # Infrastructure death must NOT be recorded as a skipped action + scored 0 (#56).
    from shinken.errors import SandboxDied

    class _Dead:
        def step(self, action, pause=0.0):
            raise ConnectionError("websocket closed")

        def close(self):
            pass

    class _Env:
        def _get_obs(self):
            return {"screenshot": None, "accessibility_tree": None}

        def evaluate(self):
            return 0.0

    agent = oe.ScriptedAgent(["```python\npyautogui.click(1, 2)\n```", "```DONE```"])
    import pytest as _pytest

    with _pytest.raises(SandboxDied):
        oe.run_episode(_Env(), agent, _Dead(), "t", max_steps=5)


# --- trajectory-level exit_reason (#56) + T-5 scorer isolation in the workload ----------


class _ScorableEnv:
    """Minimal env whose evaluator behavior is injectable (the external-scorer seam)."""

    def __init__(self, evaluate):
        self._evaluate = evaluate

    def _get_obs(self):
        return {"screenshot": None, "accessibility_tree": None}

    def evaluate(self):
        return self._evaluate()


def test_run_episode_terminal_is_task_complete_and_max_steps_budget():
    done_agent = oe.ScriptedAgent(["```DONE```"])
    res = oe.run_episode(_ScorableEnv(lambda: 1.0), done_agent, oe.RecordingActuator(), "t")
    assert res["exit_reason"] == "task_complete" and res["error"] is None

    looping = oe.ScriptedAgent(["```python\npyautogui.click(1, 2)\n```"] * 10)
    res2 = oe.run_episode(
        _ScorableEnv(lambda: 0.0), looping, oe.RecordingActuator(), "t", max_steps=2
    )
    assert res2["steps"] == 2 and res2["terminal"] is None
    assert res2["exit_reason"] == "max_steps"


def test_isolated_scorer_crash_is_typed_scorer_error_not_a_task_failure():
    def boom() -> float:
        raise RuntimeError("evaluator exploded")

    agent = oe.ScriptedAgent(["```DONE```"])
    res = oe.run_episode(_ScorableEnv(boom), agent, oe.RecordingActuator(), "t")
    # scorer_error outranks task_complete (precedence) — score 0, typed, episode intact.
    assert res["exit_reason"] == "scorer_error"
    assert res["score"] == 0.0 and res["passed"] is False
    assert res["error"].startswith("scorer_error:")


def test_isolated_scorer_timeout_is_typed_scorer_error():
    import time as _time

    agent = oe.ScriptedAgent(["```DONE```"])
    res = oe.run_episode(
        _ScorableEnv(lambda: _time.sleep(60)),
        agent,
        oe.RecordingActuator(),
        "t",
        scorer_timeout=0.5,
    )
    assert res["exit_reason"] == "scorer_error" and res["passed"] is False


def test_in_process_scoring_remains_available_behind_the_flag():
    # isolate_scorer=False keeps the legacy in-process call (a scorer fault then raises).
    agent = oe.ScriptedAgent(["```DONE```"])
    res = oe.run_episode(
        _ScorableEnv(lambda: 1.0), agent, oe.RecordingActuator(), "t", isolate_scorer=False
    )
    assert res["passed"] is True and res["exit_reason"] == "task_complete"


def test_dry_run_receipt_records_exit_reason(capsys):
    import json as _json

    assert osw.main(["--dry-run"]) == 0
    receipt = _json.loads(capsys.readouterr().out)
    assert receipt["exit_reason"] == "task_complete" and receipt["passed"] is True


def _real_run_args(tmp_path, out):
    task = tmp_path / "task.json"
    task.write_text('{"id": "t-1", "snapshot": "s", "instruction": "do it"}')
    return ["--task", str(task), "--backend", "osworld", "--out", str(out)]


def test_receipt_failure_before_agent_loop_is_setup_error(tmp_path, monkeypatch):
    import json as _json

    out = tmp_path / "receipt.json"
    monkeypatch.setattr(
        osw, "make_osworld_env", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boot failed"))
    )
    assert osw.main(_real_run_args(tmp_path, out)) == 1
    receipt = _json.loads(out.read_text())
    # T-5 split: before the agent loop -> infra-side setup_error (retry a fresh sandbox).
    assert receipt["exit_reason"] == "setup_error"
    assert "boot failed" in receipt["error"] and receipt["passed"] is False


class _StubAgentFactory:
    @staticmethod
    def from_env():
        return object()


def test_receipt_failure_in_agent_loop_classifies_agent_error_and_sandbox_died(
    tmp_path, monkeypatch
):
    import json as _json

    from shinken.errors import SandboxDied

    rec = oe.RecordingActuator()
    monkeypatch.setattr(osw, "make_osworld_env", lambda *a, **k: oe.FakeOSWorldEnv(rec))
    monkeypatch.setattr(osw, "ChatModelAgent", _StubAgentFactory)

    class _Raises:
        def __init__(self, exc):
            self._exc = exc

        def run(self, *a, **k):
            raise self._exc

    out = tmp_path / "receipt.json"
    monkeypatch.setattr(osw.workloads, "get", lambda name: _Raises(ValueError("model nonsense")))
    assert osw.main(_real_run_args(tmp_path, out)) == 1
    receipt = _json.loads(out.read_text())
    assert receipt["exit_reason"] == "agent_error"  # after the agent loop began

    monkeypatch.setattr(
        osw.workloads, "get", lambda name: _Raises(SandboxDied("vm died", exit_code=137))
    )
    assert osw.main(_real_run_args(tmp_path, out)) == 1
    receipt = _json.loads(out.read_text())
    assert receipt["exit_reason"] == "sandbox_died"  # infra death is typed either way
