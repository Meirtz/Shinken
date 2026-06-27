"""Tiny eval harness (#87) + deterministic task fixtures (#86)."""

from __future__ import annotations

import collections

import jsonschema
import pytest

import shinken
from shinken import eval as ev


def _factory(addr):
    return lambda: shinken.connect(addr)


def test_eval_n_runs_summary(mock_shinkend, tmp_path):
    task = ev.click_then_type_task(10, 20, "hello")
    s = ev.run_eval(task, _factory(mock_shinkend), n=5, out_dir=str(tmp_path))
    assert s.n == 5 and s.passed == 5 and s.pass_rate == 1.0
    assert s.setup_errors == 0
    assert s.mean_steps == 2.0  # one click + one type_text dispatched per run
    assert s.to_dict()["task"] == "click_then_type"  # JSON-serializable summary


def test_eval_failed_run_reports_verifier_failure(mock_shinkend, tmp_path):
    def verify(_env):
        return ev.VerifierReceipt.from_checks([ev.check("impossible", False)])

    task = ev.Task("failing", run=lambda e: e.click(x=1, y=1), verify=verify)
    s = ev.run_eval(task, _factory(mock_shinkend), n=5, out_dir=str(tmp_path))
    assert s.passed == 0 and s.pass_rate == 0.0
    assert all(not r.passed for r in s.results)


def test_eval_setup_error_distinct_from_task_failure(mock_shinkend, tmp_path):
    def setup(env):
        raise ev.SetupError("display not ready")

    task = ev.Task(
        "t",
        run=lambda e: None,
        verify=lambda env: ev.VerifierReceipt.from_checks([ev.check("unused", True)]),
        setup=setup,
    )
    s = ev.run_eval(task, _factory(mock_shinkend), n=3, out_dir=str(tmp_path))
    assert s.setup_errors == 3 and s.passed == 0
    assert all(r.error and r.error.startswith("setup:") for r in s.results)


def test_key_sequence_fixture(mock_shinkend, tmp_path):
    task = ev.key_sequence_task(["ctrl+a", "delete", "enter"])
    s = ev.run_eval(task, _factory(mock_shinkend), n=5, out_dir=str(tmp_path))
    assert s.passed == 5 and s.mean_steps == 3.0  # three keys dispatched per run


def test_click_then_type_verifier_is_not_a_tautology(mock_shinkend, tmp_path):
    """A run that produces the WRONG observed effect must fail — proving the verifier
    reads environment state rather than echoing the task's own inputs."""
    good = ev.click_then_type_task(10, 20, "hello")
    sabotaged = ev.Task(
        name=good.name,
        run=lambda e: (e.click(x=10, y=20), e.type_text("WRONG"))[0],  # types wrong text
        verify=good.verify,
    )
    s = ev.run_eval(sabotaged, _factory(mock_shinkend), n=2, out_dir=str(tmp_path))
    assert s.passed == 0 and s.pass_rate == 0.0
    failed = [c["name"] for c in s.results[0].receipt.checks if not c["ok"]]
    assert "typed expected text" in failed and "clicked target" not in failed


class _ForkFakeProvider:
    """Records lifecycle calls; connect() hands back a fresh Sandbox to the mock server, so
    run_eval_forked's golden→checkpoint→fork-N orchestration is exercised without a real VM."""

    def __init__(self, connect_factory):
        self._cf = connect_factory
        self.calls: collections.Counter = collections.Counter()

    def create(self, spec=None):
        self.calls["create"] += 1
        return "base"

    def connect(self, handle):
        self.calls["connect"] += 1
        return self._cf()

    def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None):
        self.calls["checkpoint"] += 1
        return "ckpt-1"

    def resume(self, ckpt):
        # Every replica resumes the SAME golden checkpoint id — proves the corrected
        # contract (all N materialize from one golden state, not N live forks).
        self.calls["resume"] += 1
        assert ckpt == "ckpt-1"
        return f"replica-{self.calls['resume']}"

    def destroy(self, handle):
        self.calls["destroy"] += 1

    def delete_snapshot(self, ckpt):
        self.calls["delete_snapshot"] += 1


def test_run_eval_forked_checkpoints_once_and_resumes_golden_n(mock_shinkend, tmp_path):
    prov = _ForkFakeProvider(_factory(mock_shinkend))
    s = ev.run_eval_forked(ev.click_then_type_task(10, 20, "hi"), prov, n=3, out_dir=str(tmp_path))
    assert s.n == 3 and s.passed == 3 and s.pass_rate == 1.0
    assert prov.calls["checkpoint"] == 1  # one golden checkpoint
    assert prov.calls["resume"] == 3  # every replica resumes the one golden checkpoint
    assert prov.calls["destroy"] == 4  # 3 replicas + the base
    assert prov.calls["delete_snapshot"] == 1  # golden snapshot reclaimed
    assert s.mean_steps == 2.0  # click + type_text per fork


def test_run_eval_forked_golden_setup_error_skips_forks(mock_shinkend, tmp_path):
    def setup(_env):
        raise ev.SetupError("display not ready")

    task = ev.Task(
        "t",
        run=lambda e: None,
        verify=lambda e: ev.VerifierReceipt.from_checks([ev.check("unused", True)]),
        setup=setup,
    )
    prov = _ForkFakeProvider(_factory(mock_shinkend))
    s = ev.run_eval_forked(task, prov, n=2, out_dir=str(tmp_path))
    assert s.setup_errors == 2 and s.passed == 0
    assert prov.calls["fork"] == 0 and prov.calls["checkpoint"] == 0  # no golden -> no forks


def test_verifier_receipt_schema():
    r = ev.VerifierReceipt.from_checks([ev.check("c", True, {"x": 1})])
    jsonschema.validate(r.to_dict(), ev.RECEIPT_SCHEMA)  # valid receipt passes
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"passed": "yes", "checks": []}, ev.RECEIPT_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"passed": False, "checks": []}, ev.RECEIPT_SCHEMA)


def test_verifier_receipt_rejects_empty_checks():
    with pytest.raises(ValueError, match="at least one check"):
        ev.VerifierReceipt.from_checks([])
    with pytest.raises(ValueError, match="at least one check"):
        ev.VerifierReceipt(True, [])


@pytest.mark.parametrize(
    "verdict",
    [
        {"passed": True, "checks": []},
        {"passed": False, "checks": []},
    ],
)
def test_eval_empty_verifier_checks_are_scorer_error(verdict, mock_shinkend, tmp_path):
    task = ev.Task("empty-receipt", run=lambda _e: None, verify=lambda _e: verdict)
    summary = ev.run_eval(task, _factory(mock_shinkend), n=1, out_dir=str(tmp_path))
    (result,) = summary.results
    assert result.kind == "error"
    assert result.exit_reason == "scorer_error"
    assert result.passed is False
    assert result.receipt.checks


# --- kind <-> exit_reason alignment (#56): one documented mapping, no drift -------------


def test_run_result_exit_reason_derives_from_kind():
    receipt = ev.VerifierReceipt.from_checks([ev.check("placeholder", False)])
    # The documented projection (eval.py tasks are unbudgeted -> verdicts complete):
    expect = {
        "pass": "task_complete",
        "fail": "task_complete",
        "setup": "setup_error",
        "sandbox_died": "sandbox_died",
        "error": "agent_error",
    }
    for kind, reason in expect.items():
        rr = ev.RunResult(0, kind == "pass", 0, 0.0, receipt, None, kind)
        assert rr.exit_reason == reason, kind
        assert rr.to_dict()["exit_reason"] == reason
    # An explicit finer value (a budgeted run, a scorer fault) is never overwritten:
    rr = ev.RunResult(0, False, 0, 0.0, receipt, "error: x", "error", "scorer_error")
    assert rr.exit_reason == "scorer_error"


def test_eval_scorer_error_refines_to_scorer_exit_reason(mock_shinkend, tmp_path):
    from shinken.errors import ScorerError

    def verify(_env):
        raise ScorerError("evaluator subprocess died", kind="crash", exit_code=1)

    task = ev.Task("t", run=lambda e: None, verify=verify)
    s = ev.run_eval(task, _factory(mock_shinkend), n=1, out_dir=str(tmp_path))
    (r,) = s.results
    # kind stays the coarse harness "error" (non-verdict); exit_reason is the finer field.
    assert r.kind == "error" and r.exit_reason == "scorer_error"
    assert not r.infra_failure  # a scorer fault is not retry-eligible infra death


def test_eval_failure_paths_set_exit_reason(mock_shinkend, tmp_path):
    def setup(_env):
        raise ev.SetupError("display not ready")

    task = ev.Task(
        "t",
        run=lambda e: None,
        verify=lambda e: ev.VerifierReceipt.from_checks([ev.check("unused", True)]),
        setup=setup,
    )
    s = ev.run_eval(task, _factory(mock_shinkend), n=1, out_dir=str(tmp_path))
    assert s.results[0].exit_reason == "setup_error"

    def run_dead(_env):
        raise ConnectionError("socket closed")

    task2 = ev.Task(
        "t2",
        run=run_dead,
        verify=lambda e: ev.VerifierReceipt.from_checks([ev.check("unused", True)]),
    )
    s2 = ev.run_eval(task2, _factory(mock_shinkend), n=1, out_dir=str(tmp_path))
    assert s2.results[0].kind == "sandbox_died"
    assert s2.results[0].exit_reason == "sandbox_died"
