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

from .errors import SandboxDied, ScorerError, ShinkenError, is_connection_loss

__all__ = [
    "RECEIPT_SCHEMA",
    "EvalSummary",
    "RunResult",
    "ScorerError",
    "SetupError",
    "Task",
    "VerifierReceipt",
    "check",
    "click_then_type_task",
    "key_sequence_task",
    "run_eval",
    "run_eval_forked",
]


class SetupError(ShinkenError):
    """Raised by a task's setup/readiness step — reported distinctly from a task
    (verifier) failure, so a flaky environment isn't scored as a failed task."""


#: ``kind`` ↔ ``exit_reason`` (#56). ``RunResult.kind`` is the eval summary taxonomy
#: ("what does this run count as": ``pass | fail | setup | sandbox_died | error``);
#: the trajectory-level ``exit_reason`` (``shinken.runtime.trajectory.EXIT_REASONS``,
#: with documented precedence) is the finer field ("why did the trajectory stop").
#: The mapping — documented once here so the two taxonomies never drift:
#:
#:   pass | fail   <->  task_complete | max_steps   (a scored verdict; eval.py's tasks
#:                       are unbudgeted, so the derived default is ``task_complete`` —
#:                       a budgeted caller passes ``exit_reason="max_steps"`` explicitly)
#:   setup         <->  setup_error
#:   sandbox_died  <->  sandbox_died
#:   error         <->  agent_error | scorer_error   (``exit_reason`` is finer: a
#:                       :class:`ScorerError` refines ``error`` to ``scorer_error``)
_EXIT_REASON_FOR_KIND = {
    "pass": "task_complete",
    "fail": "task_complete",
    "setup": "setup_error",
    "sandbox_died": "sandbox_died",
    "error": "agent_error",
}


def _classify_run_failure(exc: BaseException) -> str:
    """Typed `kind` for an exception raised while running/verifying a replica: `setup`
    (env readiness), `sandbox_died` (infra death — retry-eligible), or `error` (other
    harness failure). A passing/failing verdict is classified by the caller, not here.
    Note `sandbox_died` here means "sandbox unreachable/transport lost" — it is only
    *confirmed* substrate death when provider exit detail is attached (_confirm_death)."""
    if isinstance(exc, SetupError):
        return "setup"
    if isinstance(exc, SandboxDied) or is_connection_loss(exc):
        return "sandbox_died"
    return "error"


def _failure_exit_reason(exc: BaseException, kind: str) -> str:
    """The finer trajectory-level ``exit_reason`` for a failed run: the documented
    ``_EXIT_REASON_FOR_KIND`` projection, except a :class:`ScorerError` (an isolated
    external scorer that crashed/timed out — ``shinken.scorer_proc``) refines the
    harness-`error` kind to ``scorer_error``."""
    if isinstance(exc, ScorerError):
        return "scorer_error"
    return _EXIT_REASON_FOR_KIND[kind]


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


def _coerce_receipt(verdict: Any) -> VerifierReceipt:
    """Eagerly validate a verifier's return into a :class:`VerifierReceipt`.

    Accepts a real receipt, or anything receipt-SHAPED — an object/dict with a boolean
    ``passed`` (+ optional ``checks``) is coerced, so a verifier returning a plain
    ``{"passed": True, "checks": [...]}`` dict scores normally instead of surfacing as
    a baffling ``agent_error`` (the audit's repro: a dict return showed up as
    ``agent_error`` with ``setup_errors=n``). Anything else raises the typed
    :class:`~shinken.errors.ScorerError` (``kind="garbage"``) → the run records
    ``exit_reason="scorer_error"``, never a fake verdict."""
    if isinstance(verdict, VerifierReceipt):
        return verdict
    passed = getattr(verdict, "passed", None)
    checks = getattr(verdict, "checks", None)
    if passed is None and isinstance(verdict, dict):
        passed = verdict.get("passed")
        checks = verdict.get("checks")
    if isinstance(passed, bool):
        return VerifierReceipt(passed, list(checks or []))
    raise ScorerError(
        f"verifier returned {type(verdict).__name__!r}; expected a VerifierReceipt "
        "(or a receipt-shaped object/dict with a boolean `passed`)",
        kind="garbage",
    )


@dataclass
class Task:
    """A deterministic task — **the ONE dataclass shared by ``shinken.eval`` and
    ``shinken.gym``** (``shinken.gym.GymTask`` is a deprecated alias of this).

    ``run`` drives the session (eval-harness flows; unused by the gym, where the
    *policy* drives ``step()``), ``verify`` judges it from the live session or
    external task state, ``setup`` prepares/readiness-checks the env (the gym runs it
    once into the golden checkpoint). The gym's extra fields are optional here:
    ``instruction`` (the natural-language goal handed to a policy) and ``metadata``."""

    name: str
    run: Callable[[Any], None] | None = None
    verify: Callable[[Any], Any] | None = None
    setup: Callable[[Any], None] | None = None
    instruction: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class RunResult:
    run: int
    passed: bool
    steps: int
    wall_s: float
    receipt: VerifierReceipt
    error: str | None = None  # setup/readiness or harness error — NOT a task failure
    # Typed outcome (#56), so a consumer branches without string-matching `error`:
    #   pass | fail (task verdict) · setup | sandbox_died | error (not a verdict).
    # `sandbox_died` is infrastructure death (retry on a fresh sandbox/fork), distinct from
    # `fail` (the agent did the wrong thing — a real 0 that must NOT be retried).
    # None derives a safe value in __post_init__ — a constructor that omits `kind` must
    # never produce a row that silently counts as a pass.
    kind: str | None = None
    # The finer trajectory-level field (#56): one of
    # shinken.runtime.trajectory.EXIT_REASONS. Derived from `kind` via the documented
    # `_EXIT_REASON_FOR_KIND` mapping when omitted; a caller that knows better (a
    # ScorerError -> "scorer_error", a budgeted run -> "max_steps") passes it explicitly.
    exit_reason: str | None = None

    def __post_init__(self) -> None:
        if self.kind is None:
            self.kind = "pass" if self.passed else ("error" if self.error is not None else "fail")
        if self.exit_reason is None:
            self.exit_reason = _EXIT_REASON_FOR_KIND.get(self.kind)

    @property
    def infra_failure(self) -> bool:
        """True when the run did not produce a verdict because the environment failed —
        the retry-eligible class (setup readiness or sandbox death)."""
        return self.kind in ("setup", "sandbox_died")

    def to_dict(self) -> dict:
        return {
            "run": self.run,
            "passed": self.passed,
            "steps": self.steps,
            "wall_s": round(self.wall_s, 6),
            "receipt": self.receipt.to_dict(),
            "error": self.error,
            "kind": self.kind,
            "exit_reason": self.exit_reason,
        }


@dataclass
class EvalSummary:
    task: str
    n: int
    passed: int
    setup_errors: int  # legacy: ALL non-verdict runs (setup + sandbox_died + harness error)
    pass_rate: float
    mean_steps: float
    mean_wall_s: float
    results: list[RunResult]
    # Per-kind breakdown (#56): {pass, fail, setup, sandbox_died, error}. `setup_errors`
    # keeps its historical meaning (every run that produced no verdict, including harness
    # `error` runs); `infra_errors` is the precise retry-eligible subset
    # (setup + sandbox_died only); `kinds` exposes the full split.
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def infra_errors(self) -> int:
        return sum(1 for r in self.results if r.infra_failure)

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "n": self.n,
            "passed": self.passed,
            "setup_errors": self.setup_errors,
            "infra_errors": self.infra_errors,
            "kinds": self.kinds,
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
    failures are reported via ``RunResult.error``, distinct from verifier failures.
    Verifier returns are validated EAGERLY: a receipt-shaped dict/object is coerced into
    a :class:`VerifierReceipt`, and a garbage return classifies that run as the typed
    :class:`~shinken.errors.ScorerError` (``exit_reason="scorer_error"``) — never a
    fake verdict."""
    if task.run is None or task.verify is None:
        raise ValueError(f"task {task.name!r} needs both run and verify for run_eval()")
    out_dir = out_dir or tempfile.mkdtemp(prefix="shinken-eval-")
    os.makedirs(out_dir, exist_ok=True)
    results: list[RunResult] = []
    for i in range(n):
        env = None  # created inside the try so a connect failure is a per-replica error (#147)
        err: str | None = None
        passed = False
        steps = 0
        receipt = VerifierReceipt(False, [])
        kind = "pass"
        reason: str | None = None  # None -> derived from kind (verdicts are task_complete)
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
            receipt = _coerce_receipt(task.verify(env))
            passed = receipt.passed
            kind = "pass" if passed else "fail"
        except Exception as exc:  # noqa: BLE001 — classify, never crash the eval loop
            wall = time.perf_counter() - t0
            kind = _classify_run_failure(exc)
            reason = _failure_exit_reason(exc, kind)
            err = f"{kind}: {exc}"
        finally:
            if env is not None:
                with contextlib.suppress(Exception):
                    env.close()
        results.append(RunResult(i, passed, steps, wall, receipt, err, kind, reason))

    return _summarize(task.name, n, results)


def _summarize(task_name: str, n: int, results: list[RunResult]) -> EvalSummary:
    """Aggregate per-replica results into an EvalSummary (pass-rate, mean steps/wall)."""
    n_passed = sum(1 for r in results if r.passed)
    non_verdict = sum(1 for r in results if r.error is not None)
    completed = [r for r in results if r.error is None]
    mean_steps = sum(r.steps for r in completed) / len(completed) if completed else 0.0
    mean_wall = sum(r.wall_s for r in results) / len(results) if results else 0.0
    kinds: dict[str, int] = {}
    for r in results:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    return EvalSummary(
        task=task_name,
        n=n,
        passed=n_passed,
        # legacy name = runs that produced no verdict (setup + sandbox_died + harness error);
        # use `infra_errors` for the precise retry-eligible subset (setup + sandbox_died).
        setup_errors=non_verdict,
        pass_rate=(n_passed / n if n else 0.0),
        mean_steps=mean_steps,
        mean_wall_s=mean_wall,
        results=results,
        kinds=kinds,
    )


def _confirm_death(provider: Any, handle: Any, exc: BaseException) -> BaseException:
    """If ``exc`` looks like a lost/hung connection and the provider can introspect the
    sandbox, return a ``SandboxDied`` (with substrate exit detail) when the sandbox really
    exited; otherwise return ``exc`` unchanged. A timeout is probed too — a half-open
    socket on a dead host times out rather than closing, so the probe is the only way to
    tell a hung-dead sandbox from a merely slow one. Best-effort — a provider without
    ``check_alive`` or a still-alive sandbox leaves the original exception."""
    if isinstance(exc, SandboxDied) or handle is None:
        return exc
    if not (is_connection_loss(exc) or isinstance(exc, TimeoutError)):
        return exc
    check = getattr(provider, "check_alive", None)
    if check is None:
        return exc
    try:
        check(handle)  # raises SandboxDied(exit detail) if the sandbox exited
    except SandboxDied as died:
        died.__cause__ = exc  # keep the original failure for forensics
        return died
    except Exception:  # noqa: BLE001 — introspection failed; keep the original error
        return exc
    return exc


def _refine_with_provider(rr: RunResult, provider: Any, handle: Any) -> RunResult:
    """Upgrade a coarse ``sandbox_died`` RunResult with provider-confirmed exit detail,
    keeping the original failure context (which call failed) alongside the substrate
    detail (why the sandbox died)."""
    refined = _confirm_death(provider, handle, ConnectionError(rr.error or "connection lost"))
    if isinstance(refined, SandboxDied):
        return RunResult(
            rr.run,
            False,
            rr.steps,
            rr.wall_s,
            rr.receipt,
            f"sandbox_died: {refined} (during: {rr.error or 'connection lost'})",
            "sandbox_died",
        )
    return rr


def _score_replica(i: int, env: Any, task: Task) -> RunResult:
    """Run + verify ``task`` on an already-provisioned ``env`` (no setup); never raises."""
    err: str | None = None
    passed = False
    steps = 0
    receipt = VerifierReceipt(False, [])
    kind = "pass"
    reason: str | None = None  # None -> derived from kind (verdicts are task_complete)
    t0 = time.perf_counter()
    wall = 0.0
    try:
        before = getattr(env, "actions_dispatched", 0)
        task.run(env)
        steps = max(0, getattr(env, "actions_dispatched", 0) - before)
        wall = time.perf_counter() - t0
        receipt = _coerce_receipt(task.verify(env))
        passed = receipt.passed
        kind = "pass" if passed else "fail"
    except Exception as exc:  # noqa: BLE001 — classify, never raise into the fork loop
        wall = time.perf_counter() - t0
        kind = _classify_run_failure(exc)
        reason = _failure_exit_reason(exc, kind)
        err = f"{kind}: {exc}"
    return RunResult(i, passed, steps, wall, receipt, err, kind, reason)


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
    if task.run is None or task.verify is None:
        raise ValueError(f"task {task.name!r} needs both run and verify for run_eval_forked()")
    out_dir = out_dir or tempfile.mkdtemp(prefix="shinken-fork-eval-")
    os.makedirs(out_dir, exist_ok=True)
    base = provider.create(spec)
    ckpt: str | None = None
    try:
        # --- reach the golden state once, then checkpoint it ---
        try:
            env0 = provider.connect(base)
            try:
                if task.setup is not None:
                    task.setup(env0)
                ckpt = provider.checkpoint(base)
            finally:
                with contextlib.suppress(Exception):
                    env0.close()
        except Exception as exc:  # golden setup/checkpoint failed -> no replicas run
            exc = _confirm_death(provider, base, exc)  # add exit detail if the base died
            kind = _classify_run_failure(exc)
            reason = _failure_exit_reason(exc, kind)
            res = [
                RunResult(
                    i, False, 0, 0.0, VerifierReceipt(False, []), f"{kind}: {exc}", kind, reason
                )
                for i in range(n)
            ]
            return _summarize(task.name, n, res)
        # --- N replicas materialized from the SINGLE golden checkpoint ---
        # resume(ckpt) launches each replica from the exact committed image, so all N
        # start from the identical golden state. (Previously this forked the live base,
        # taking N separate commits at different moments — replicas could drift apart.)
        results: list[RunResult] = []
        for i in range(n):
            env = None
            handle = None
            try:
                handle = provider.resume(ckpt)
                env = provider.connect(handle)
                rr = _score_replica(i, env, task)
                # If the replica dropped its connection, ask the provider whether the
                # sandbox actually died — upgrades a coarse "sandbox_died" to one carrying
                # the substrate exit/signal detail a retry policy can act on.
                if rr.kind == "sandbox_died" and handle is not None:
                    rr = _refine_with_provider(rr, provider, handle)
                results.append(rr)
            except Exception as exc:  # resume/connect failure for this replica
                exc = _confirm_death(provider, handle, exc)
                kind = _classify_run_failure(exc)
                reason = _failure_exit_reason(exc, kind)
                results.append(
                    RunResult(
                        i, False, 0, 0.0, VerifierReceipt(False, []), f"{kind}: {exc}", kind, reason
                    )
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
        # Reclaim the golden snapshot image so repeated eval runs don't accumulate
        # committed images until the Docker disk fills (#... image leak).
        if ckpt is not None and hasattr(provider, "delete_snapshot"):
            with contextlib.suppress(Exception):
                provider.delete_snapshot(ckpt)


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
