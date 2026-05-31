"""Tiny eval harness (#87) + deterministic task fixtures (#86)."""

from __future__ import annotations

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
        "t", run=lambda e: None, verify=lambda env: ev.VerifierReceipt(True, []), setup=setup
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


def test_verifier_receipt_schema():
    r = ev.VerifierReceipt.from_checks([ev.check("c", True, {"x": 1})])
    jsonschema.validate(r.to_dict(), ev.RECEIPT_SCHEMA)  # valid receipt passes
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"passed": "yes", "checks": []}, ev.RECEIPT_SCHEMA)
