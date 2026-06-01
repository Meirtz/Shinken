"""Tiny local eval harness (#87) + deterministic task fixtures (#86).

Proves v0 eval orchestration — setup → run → verify → N repeated runs → metrics —
without a model or a cloud service. Full trace capture is intentionally deferred.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class SetupError(RuntimeError):
    """Raised by a task's setup/readiness step — reported distinctly from a task
    (verifier) failure, so a flaky environment isn't scored as a failed task."""


def check(name: str, ok: bool, evidence: Any = None) -> dict:
    """Build one verifier check with optional evidence."""
    return {"name": name, "ok": bool(ok), "evidence": evidence}


#: JSON Schema for a verifier receipt (tested; the eval contract surface).
RECEIPT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["passed", "checks"],
    "properties": {
        "passed": {"type": "boolean"},
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "ok"],
                "properties": {
                    "name": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "evidence": {},
                },
            },
        },
    },
}


@dataclass
class VerifierReceipt:
    """The verdict for one run: an overall pass plus the individual checks + evidence."""

    passed: bool
    checks: list[dict] = field(default_factory=list)

    @classmethod
    def from_checks(cls, checks: list[dict]) -> VerifierReceipt:
        return cls(passed=all(c["ok"] for c in checks), checks=checks)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks}


@dataclass
class Task:
    """A deterministic eval task: ``run`` drives the session, ``verify`` judges it from
    the live session or external task state, ``setup`` prepares/readiness-checks the env."""

    name: str
    run: Callable[[Any], None]
    verify: Callable[[Any], VerifierReceipt]
    setup: Callable[[Any], None] | None = None


@dataclass
class RunResult:
    run: int
    passed: bool
    steps: int
    wall_s: float
    receipt: VerifierReceipt
    error: str | None = None  # setup/readiness or harness error — NOT a task failure

    def to_dict(self) -> dict:
        return {
            "run": self.run,
            "passed": self.passed,
            "steps": self.steps,
            "wall_s": round(self.wall_s, 6),
            "receipt": self.receipt.to_dict(),
            "error": self.error,
        }


@dataclass
class EvalSummary:
    task: str
    n: int
    passed: int
    setup_errors: int
    pass_rate: float
    mean_steps: float
    mean_wall_s: float
    results: list[RunResult]

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "n": self.n,
            "passed": self.passed,
            "setup_errors": self.setup_errors,
            "pass_rate": round(self.pass_rate, 4),
            "mean_steps": round(self.mean_steps, 3),
            "mean_wall_s": round(self.mean_wall_s, 6),
            "results": [r.to_dict() for r in self.results],
        }


def run_eval(
    task: Task,
    connect_factory: Callable[[], Any],
    n: int = 5,
    out_dir: str | None = None,
) -> EvalSummary:
    """Run ``task`` ``n`` times, each in a fresh session from ``connect_factory``, verify
    each run, and summarize. ``out_dir`` is reserved for future artifacts; setup/readiness
    failures are reported via ``RunResult.error``, distinct from verifier failures."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="shinken-eval-")
    os.makedirs(out_dir, exist_ok=True)
    results: list[RunResult] = []
    for i in range(n):
        env = None  # created inside the try so a connect failure is a per-replica error (#147)
        err: str | None = None
        passed = False
        steps = 0
        receipt = VerifierReceipt(False, [])
        t0 = time.perf_counter()
        wall = 0.0
        try:
            env = connect_factory()
            if task.setup is not None:
                task.setup(env)
            actions_before = getattr(env, "actions_dispatched", 0)
            task.run(env)
            steps = max(0, getattr(env, "actions_dispatched", 0) - actions_before)
            wall = time.perf_counter() - t0
            receipt = task.verify(env)
            passed = receipt.passed
        except SetupError as exc:
            wall = time.perf_counter() - t0
            err = f"setup: {exc}"
        except Exception as exc:  # connect/harness/run error — distinct from a task failure
            wall = time.perf_counter() - t0
            err = f"error: {exc}"
        finally:
            if env is not None:
                with contextlib.suppress(Exception):
                    env.close()
        results.append(RunResult(i, passed, steps, wall, receipt, err))

    return _summarize(task.name, n, results)


def _summarize(task_name: str, n: int, results: list[RunResult]) -> EvalSummary:
    """Aggregate per-replica results into an EvalSummary (pass-rate, mean steps/wall)."""
    n_passed = sum(1 for r in results if r.passed)
    setup_errors = sum(1 for r in results if r.error is not None)
    completed = [r for r in results if r.error is None]
    mean_steps = sum(r.steps for r in completed) / len(completed) if completed else 0.0
    mean_wall = sum(r.wall_s for r in results) / len(results) if results else 0.0
    return EvalSummary(
        task=task_name,
        n=n,
        passed=n_passed,
        setup_errors=setup_errors,
        pass_rate=(n_passed / n if n else 0.0),
        mean_steps=mean_steps,
        mean_wall_s=mean_wall,
        results=results,
    )


def _score_replica(i: int, env: Any, task: Task) -> RunResult:
    """Run + verify ``task`` on an already-provisioned ``env`` (no setup); never raises."""
    err: str | None = None
    passed = False
    steps = 0
    receipt = VerifierReceipt(False, [])
    t0 = time.perf_counter()
    wall = 0.0
    try:
        before = getattr(env, "actions_dispatched", 0)
        task.run(env)
        steps = max(0, getattr(env, "actions_dispatched", 0) - before)
        wall = time.perf_counter() - t0
        receipt = task.verify(env)
        passed = receipt.passed
    except SetupError as exc:
        wall = time.perf_counter() - t0
        err = f"setup: {exc}"
    except Exception as exc:
        wall = time.perf_counter() - t0
        err = f"error: {exc}"
    return RunResult(i, passed, steps, wall, receipt, err)


def run_eval_forked(
    task: Task,
    provider: Any,
    *,
    n: int = 5,
    spec: Any = None,
    out_dir: str | None = None,
) -> EvalSummary:
    """Score ``task`` over ``n`` replicas **forked from a single golden checkpoint** — the
    runtime-state eval loop (D5/D1). Create a base sandbox, run ``task.setup`` once to reach the
    golden state, ``checkpoint`` it, then for each replica ``fork`` a fresh sandbox from the base,
    run + verify, and destroy it. Requires a provider that supports checkpoint + fork (e.g. the
    Docker disk tier); a flaky golden setup/checkpoint yields a clean all-error summary.

    This is what makes high-N eval cheap and is the seam training reuses (best-of-N / tree-search):
    one golden state, many cheap forks, each scored independently."""
    out_dir = out_dir or tempfile.mkdtemp(prefix="shinken-fork-eval-")
    os.makedirs(out_dir, exist_ok=True)
    base = provider.create(spec)
    try:
        # --- reach the golden state once, then checkpoint it ---
        try:
            env0 = provider.connect(base)
            try:
                if task.setup is not None:
                    task.setup(env0)
                provider.checkpoint(base)
            finally:
                with contextlib.suppress(Exception):
                    env0.close()
        except Exception as exc:  # golden setup/checkpoint failed -> no replicas run
            label = "setup" if isinstance(exc, SetupError) else "error"
            res = [
                RunResult(i, False, 0, 0.0, VerifierReceipt(False, []), f"{label}: {exc}")
                for i in range(n)
            ]
            return _summarize(task.name, n, res)
        # --- N forked replicas from the golden checkpoint ---
        results: list[RunResult] = []
        for i in range(n):
            env = None
            handle = None
            try:
                handle = provider.fork(base)
                env = provider.connect(handle)
                results.append(_score_replica(i, env, task))
            except Exception as exc:  # fork/connect failure for this replica
                results.append(
                    RunResult(i, False, 0, 0.0, VerifierReceipt(False, []), f"error: {exc}")
                )
            finally:
                if env is not None:
                    with contextlib.suppress(Exception):
                        env.close()
                if handle is not None:
                    with contextlib.suppress(Exception):
                        provider.destroy(handle)
        return _summarize(task.name, n, results)
    finally:
        with contextlib.suppress(Exception):
            provider.destroy(base)


# --- deterministic task fixtures (#86) ------------------------------------------------


def click_then_type_task(x: int, y: int, text: str) -> Task:
    """Fixture: click a target point, then type ``text``.

    The verifier reads the environment's **observed** state (``env.query("state")``) and
    checks that a click actually landed at ``(x, y)`` and that the typed text actually
    appeared — so a broken ``run`` (wrong coordinate, wrong/no text) fails the check. It is
    not a tautology: the assertions compare observed effects, not the task's own inputs.
    (Verification rides the reference/stateful environment; real-GUI OS/file-state scoring
    is the OSWorld evaluator path — see ``scripts/osworld_single.py``.)"""

    def run(env: Any) -> None:
        env.click(x=x, y=y)
        env.type_text(text)

    def verify(env: Any) -> VerifierReceipt:
        state = env.query("state")
        clicks = state.get("clicks", [])
        typed = state.get("typed", "")
        clicked = any(c.get("x") == x and c.get("y") == y for c in clicks)
        return VerifierReceipt.from_checks(
            [
                check("clicked target", clicked, {"x": x, "y": y, "observed_clicks": clicks}),
                check("typed expected text", typed == text, {"expected": text, "observed": typed}),
            ]
        )

    return Task(name="click_then_type", run=run, verify=verify)


def key_sequence_task(keys: list[str]) -> Task:
    """Fixture: press a sequence of keys; the verifier confirms the exact order from the
    environment's **observed** key log (``env.query("state")``), not from the task's own
    input — a wrong/missing keypress fails the check."""

    def run(env: Any) -> None:
        for k in keys:
            env.key(k)

    def verify(env: Any) -> VerifierReceipt:
        pressed = env.query("state").get("keys", [])
        return VerifierReceipt.from_checks(
            [
                check(
                    "key sequence matches", pressed == keys, {"expected": keys, "observed": pressed}
                )
            ]
        )

    return Task(name="key_sequence", run=run, verify=verify)
