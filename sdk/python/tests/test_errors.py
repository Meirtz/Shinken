"""Typed failure taxonomy (#56): error types, exception classification, eval `kind`,
per-action batch status, and provider sandbox-death detection."""

from __future__ import annotations

import shinken
from shinken.errors import SandboxDied, ShinkenError, classify_exception
from shinken.eval import RunResult, _classify_run_failure


def test_sandbox_died_carries_exit_detail_and_is_a_shinken_error():
    e = SandboxDied("container gone", exit_code=137, signal=9, detail="OOMKilled")
    assert isinstance(e, ShinkenError)
    assert e.exit_code == 137 and e.signal == 9 and e.detail == "OOMKilled"
    assert "exit_code=137" in str(e) and "signal=9" in str(e)
    assert shinken.SandboxDied is SandboxDied  # exported from the package root


def test_classify_exception_maps_to_action_statuses():
    assert classify_exception(SandboxDied("x")) == "sandbox_died"
    assert classify_exception(TimeoutError("rpc")) == "timeout"
    assert classify_exception(ConnectionError("dropped")) == "sandbox_died"
    assert classify_exception(ValueError("bad arg")) == "error"


def test_eval_classify_run_failure():
    from shinken.eval import SetupError

    assert _classify_run_failure(SetupError("not ready")) == "setup"
    assert _classify_run_failure(SandboxDied("died")) == "sandbox_died"
    assert _classify_run_failure(ConnectionError("drop")) == "sandbox_died"
    assert _classify_run_failure(RuntimeError("boom")) == "error"


def test_run_result_infra_failure_property():
    assert RunResult(0, False, 0, 0.0, _empty(), "x", "sandbox_died").infra_failure is True
    assert RunResult(0, False, 0, 0.0, _empty(), "x", "setup").infra_failure is True
    assert RunResult(0, False, 0, 0.0, _empty(), "x", "error").infra_failure is False
    assert RunResult(0, False, 0, 0.0, _empty(), None, "fail").infra_failure is False
    assert RunResult(0, True, 1, 0.1, _empty(), None, "pass").infra_failure is False


def _empty():
    from shinken.eval import VerifierReceipt

    return VerifierReceipt(False, [])


def test_run_eval_classifies_connection_drop_as_sandbox_died(tmp_path):
    # a connect_factory that fails with a dropped connection is infra death, not a task fail
    from shinken.eval import Task, VerifierReceipt, run_eval

    def boom():
        raise ConnectionError("connection lost mid-screencast")

    task = Task(name="t", run=lambda e: None, verify=lambda e: VerifierReceipt(True, []))
    s = run_eval(task, boom, n=2, out_dir=str(tmp_path))
    assert s.passed == 0
    assert s.kinds.get("sandbox_died") == 2
    assert s.infra_errors == 2  # retry-eligible, not scored as failed tasks
    assert all(r.kind == "sandbox_died" and r.infra_failure for r in s.results)


def test_run_eval_forked_upgrades_drop_to_sandbox_died_with_detail(mock_shinkend, tmp_path):
    # when a replica's connection drops, the harness asks the provider whether the sandbox
    # actually died and surfaces its exit detail (check_alive raises SandboxDied).
    import collections

    from shinken.eval import click_then_type_task, run_eval_forked

    class _DyingProvider:
        def __init__(self):
            self.calls = collections.Counter()

        def create(self, spec=None):
            return "base"

        def connect(self, handle):
            self.calls["connect"] += 1
            raise ConnectionError("websocket closed")  # replica connect drops

        def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None):
            return "ckpt-1"

        def resume(self, ckpt):
            self.calls["resume"] += 1
            return f"replica-{self.calls['resume']}"

        def check_alive(self, handle):
            raise SandboxDied("container exited", exit_code=137, signal=9, detail="OOMKilled")

        def destroy(self, handle):
            pass

    prov = _DyingProvider()
    # golden setup connects once (also drops) — so the whole run is sandbox_died, with detail.
    task = click_then_type_task(10, 20, "hi")
    s = run_eval_forked(task, prov, n=2, out_dir=str(tmp_path))
    assert s.passed == 0
    assert all(r.kind == "sandbox_died" for r in s.results)
    assert any("exit_code=137" in (r.error or "") for r in s.results)


def test_docker_check_alive_raises_sandbox_died_on_exited_container(monkeypatch):
    import subprocess as sp

    from shinken.providers.base import SandboxHandle
    from shinken.providers.docker import DockerLocalProvider

    prov = DockerLocalProvider()
    handle = SandboxHandle(
        provider="docker-local", sandbox_id="c", addr="x", metadata={"container_id": "abc"}
    )

    def fake_run(cmd, **kw):
        assert "inspect" in cmd
        return sp.CompletedProcess(cmd, 0, stdout="exited 137 true\n", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    import pytest

    with pytest.raises(SandboxDied) as ei:
        prov.check_alive(handle)
    assert ei.value.exit_code == 137 and ei.value.signal == 9  # OOMKilled -> SIGKILL


def test_docker_check_alive_noop_when_running(monkeypatch):
    import subprocess as sp

    from shinken.providers.base import SandboxHandle
    from shinken.providers.docker import DockerLocalProvider

    prov = DockerLocalProvider()
    handle = SandboxHandle(
        provider="docker-local", sandbox_id="c", addr="x", metadata={"container_id": "abc"}
    )
    monkeypatch.setattr(
        sp,
        "run",
        lambda cmd, **kw: sp.CompletedProcess(cmd, 0, stdout="running 0 false\n", stderr=""),
    )
    prov.check_alive(handle)  # must not raise


def test_docker_check_alive_does_not_assert_death_when_daemon_is_down(monkeypatch):
    # `docker inspect` exits nonzero both when the container is gone AND when the daemon is
    # unreachable — only the former is confirmed death (base-class fail-open contract).
    import subprocess as sp

    from shinken.providers.base import SandboxHandle
    from shinken.providers.docker import DockerLocalProvider

    prov = DockerLocalProvider()
    handle = SandboxHandle(
        provider="docker-local", sandbox_id="c", addr="x", metadata={"container_id": "abc"}
    )
    monkeypatch.setattr(
        sp,
        "run",
        lambda cmd, **kw: sp.CompletedProcess(
            cmd, 1, stdout="", stderr="Cannot connect to the Docker daemon at unix:///..."
        ),
    )
    prov.check_alive(handle)  # daemon down ⇒ cannot introspect ⇒ must NOT raise


def test_docker_check_alive_asserts_death_on_no_such_container(monkeypatch):
    import subprocess as sp

    import pytest

    from shinken.providers.base import SandboxHandle
    from shinken.providers.docker import DockerLocalProvider

    prov = DockerLocalProvider()
    handle = SandboxHandle(
        provider="docker-local", sandbox_id="c", addr="x", metadata={"container_id": "abc"}
    )
    monkeypatch.setattr(
        sp,
        "run",
        lambda cmd, **kw: sp.CompletedProcess(
            cmd, 1, stdout="", stderr="Error: No such object: abc"
        ),
    )
    with pytest.raises(SandboxDied):
        prov.check_alive(handle)


def test_run_result_post_init_derives_kind_when_omitted():
    # the old positional signature (no kind) must never silently produce a passing row.
    assert RunResult(0, True, 1, 0.1, _empty()).kind == "pass"
    assert RunResult(0, False, 0, 0.0, _empty()).kind == "fail"
    assert RunResult(0, False, 0, 0.0, _empty(), "boom").kind == "error"


def test_is_connection_loss_recognizes_websockets_connection_closed():
    from websockets.exceptions import ConnectionClosedError

    from shinken.errors import is_connection_loss

    cc = ConnectionClosedError(None, None)
    assert is_connection_loss(cc) is True  # NOT a ConnectionError subclass, but counts
    assert is_connection_loss(ConnectionResetError("reset")) is True
    assert is_connection_loss(ValueError("x")) is False
    assert classify_exception(cc) == "sandbox_died"


def test_rpc_send_normalizes_connection_closed_and_batch_marks_sandbox_died():
    # The headline scenario (#56 review): the sandbox dies while idle; the NEXT send raises
    # websockets ConnectionClosed (not a ConnectionError). _rpc must normalize it, and a
    # continue-mode batch must stop and mark the rest skipped with failure_kind sandbox_died.
    import asyncio

    from websockets.exceptions import ConnectionClosedError

    from shinken.client import AsyncSandbox, Capabilities

    class _DeadWS:
        async def send(self, _data):
            raise ConnectionClosedError(None, None)

    caps = Capabilities(
        schema_version=0, verbs=["click"], targets=["point_px"], observation_types=[]
    )

    async def go():
        sb = AsyncSandbox(_DeadWS(), caps, "linux")
        # a single act() surfaces the drop as a builtin ConnectionError (normalized)
        try:
            await sb.act("click", {"kind": "point_px", "x": 1, "y": 2})
        except ConnectionError:
            pass
        else:
            raise AssertionError("expected ConnectionError from a dead send")
        # a continue-mode batch must not keep dispatching into the dead sandbox
        res = await sb.act_batch(
            [
                {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 1}},
                {"verb": "click", "target": {"kind": "point_px", "x": 2, "y": 2}},
            ],
            stop_on_error=False,
        )
        return res

    res = asyncio.run(go())
    assert res["completed"] is False
    assert res["failure_kind"] == "sandbox_died"
    assert res["results"][0]["status"] == "sandbox_died"
    assert res["results"][1]["status"] == "skipped"
