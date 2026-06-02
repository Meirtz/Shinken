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
        shk_addr="x",
    )
    assert osw._build_shinken_actuator(args) == "ACTUATOR"
    assert seen == {
        "container": "osw-vm",
        "port": 8765,
        "binary": "/b/shinkend",
        "method": "docker",
        "remote_bin": "/tmp/shinkend",
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
