"""Subprocess scorer isolation (T-5, #56) — the external-evaluator lane.

A third-party scorer (the OSWorld evaluator, a task-bundle verifier, any judge someone
else wrote) must not be able to corrupt a score or wedge an eval by misbehaving: stray
stdout, a non-zero exit *after* a correct verdict, a crash, or a hang. The contract
(``docs/engineering/v0.0.1-plan.md`` §6):

- the scorer runs in a **fresh subprocess**, receiving its task as JSON on stdin;
- it writes its verdict to a **result file via atomic write+fsync+rename** and mirrors
  it as JSON on stdout (the file is the authoritative channel; stdout is for logs);
- the parent enforces a **bounded timeout** and treats a written result file as
  authoritative **even if the child then exits non-zero or times out** — only a child
  that produced no verdict at all is an error;
- failure is **typed** (:class:`shinken.errors.ScorerError` with ``kind`` =
  ``crash | timeout | garbage``) and feeds the trajectory-level
  ``exit_reason = "scorer_error"`` (``shinken.runtime.trajectory.EXIT_REASONS``).

Two parent entrypoints share those semantics:

- :func:`run_scorer` — spawn ``python -m shinken.scorer_proc``; the child imports a
  ``"module:callable"`` entrypoint and calls it with the task dict (scorers describable
  by module path, e.g. a downloadable task-bundle evaluator);
- :func:`run_scorer_callable` — ``os.fork`` for scorers that are live closures over
  un-serializable state (the OSWorld ``DesktopEnv.evaluate`` bound method holds the VM
  connection); the child inherits the object and reports through the same result-file
  protocol.

The in-process reference verifiers (``shinken.eval``'s tiny fixtures, which read the
live session) deliberately do NOT use this — isolation is for *external* evaluator code.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from typing import Any

from shinken.errors import ScorerError

_TAIL_CHARS = 2000  # how much child output to keep as ScorerError.detail


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write ``payload`` to ``path`` via tmp-file + fsync + rename, so a reader never
    sees a torn/partial verdict — either the file is absent or it is complete."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_result(path: str) -> dict | None:
    """Parse the result file; ``None`` if absent or unparseable (atomic write means a
    present-but-garbled file should not happen, but never let it crash the parent)."""
    try:
        with open(path) as fh:
            verdict = json.load(fh)
    except (OSError, ValueError):
        return None
    return verdict if isinstance(verdict, dict) else None


def _normalize_verdict(value: Any) -> dict:
    """Coerce a scorer's return into the verdict shape ``{"score": float, ...}``."""
    if isinstance(value, dict):
        if "score" not in value:
            raise ValueError(f"scorer dict verdict missing 'score': {value!r}")
        return {**value, "score": float(value["score"])}
    if isinstance(value, int | float):
        return {"score": float(value)}
    raise ValueError(f"scorer returned {type(value).__name__}, want a number or dict with 'score'")


def _tail(text: str | None) -> str | None:
    return text[-_TAIL_CHARS:] if text else None


def run_scorer_command(
    argv: list[str],
    task: dict,
    *,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
) -> dict:
    """Run ``argv`` as an isolated scorer process: the result-file path is appended as
    the last argument, ``task`` goes in as JSON on stdin, and the verdict comes back per
    the T-5 contract — the atomically-written result file is authoritative (even on a
    non-zero exit or a timeout *after* the write); a child that wrote no verdict raises a
    typed :class:`ScorerError` (``timeout`` / ``crash`` / ``garbage``)."""
    with tempfile.TemporaryDirectory(prefix="shinken-scorer-") as td:
        result_path = os.path.join(td, "result.json")
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        # Prepend LAST so the child always resolves the SAME shinken package as the
        # parent (src layouts, worktrees) even when the caller overrides PYTHONPATH to
        # make its entrypoint module importable.
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        child_env["PYTHONPATH"] = src + os.pathsep + child_env.get("PYTHONPATH", "")
        proc = subprocess.Popen(  # noqa: S603 — argv comes from the harness, not the model
            [*argv, result_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
        )
        timed_out = False
        try:
            out, err = proc.communicate(json.dumps(task), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            out, err = proc.communicate()
        verdict = _read_result(result_path)
        if verdict is not None:
            return verdict  # authoritative — even over a later non-zero exit or timeout
        if timed_out:
            raise ScorerError(
                f"scorer timed out after {timeout}s with no verdict written",
                kind="timeout",
                detail=_tail(err or out),
            )
        if proc.returncode != 0:
            raise ScorerError(
                f"scorer exited {proc.returncode} with no verdict written",
                kind="crash",
                exit_code=proc.returncode,
                detail=_tail(err or out),
            )
        raise ScorerError(
            "scorer exited 0 without writing a verdict",
            kind="garbage",
            exit_code=0,
            detail=_tail(out or err),
        )


def run_scorer(
    entrypoint: str,
    task: dict,
    *,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
) -> dict:
    """Score ``task`` in a fresh ``python -m shinken.scorer_proc`` child: the child
    imports ``entrypoint`` (``"module:callable"``), calls it with the task dict, and
    reports through the result-file protocol (see :func:`run_scorer_command`)."""
    payload = {"entrypoint": entrypoint, "task": task}
    return run_scorer_command(
        [sys.executable, "-m", "shinken.scorer_proc"], payload, timeout=timeout, env=env
    )


def run_scorer_callable(
    fn: Callable[[], Any],
    *,
    timeout: float = 300.0,
    poll_s: float = 0.05,
) -> dict:
    """Isolate a **live-object** scorer — a callable closing over un-serializable state,
    e.g. the OSWorld ``DesktopEnv.evaluate`` bound method — in an ``os.fork`` child with
    the same semantics as :func:`run_scorer_command`: the child inherits the object,
    redirects its stdout/stderr to a log (stray prints cannot pollute the parent), writes
    the verdict atomically, and the parent enforces the bounded timeout + typed errors.
    POSIX-only (the Linux/macOS lanes this runtime targets); on a platform without
    ``os.fork`` it degrades to a direct in-process call."""
    if not hasattr(os, "fork"):  # pragma: no cover — non-POSIX fallback
        return _normalize_verdict(fn())
    with tempfile.TemporaryDirectory(prefix="shinken-scorer-") as td:
        result_path = os.path.join(td, "result.json")
        log_path = os.path.join(td, "scorer.log")
        pid = os.fork()
        if pid == 0:  # ---- child: score, write verdict, _exit (never return) ----
            code = 1
            try:
                log = open(log_path, "w")  # noqa: SIM115 — child _exits; no context manager
                os.dup2(log.fileno(), 1)
                os.dup2(log.fileno(), 2)
                # A capture manager (pytest) may have rebound the *Python* stream objects
                # away from fds 1/2 — rebind those too, or stray scorer prints would leak
                # into the harness capture instead of the child log.
                sys.stdout = log
                sys.stderr = log
                _atomic_write_json(result_path, _normalize_verdict(fn()))
                code = 0
            except BaseException:  # noqa: BLE001 — child must report + die, never unwind
                import traceback

                with contextlib.suppress(Exception):
                    traceback.print_exc()
            finally:
                with contextlib.suppress(Exception):
                    sys.stdout.flush()
                    sys.stderr.flush()
                os._exit(code)
        # ---- parent: bounded wait, then result file is authoritative ----
        status: int | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wpid, st = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                status = st
                break
            time.sleep(poll_s)
        timed_out = status is None
        if timed_out:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        verdict = _read_result(result_path)
        if verdict is not None:
            return verdict  # authoritative — even over a timeout/non-zero exit after the write
        detail = None
        with contextlib.suppress(OSError):
            with open(log_path) as fh:
                detail = _tail(fh.read())
        if timed_out:
            raise ScorerError(
                f"scorer timed out after {timeout}s with no verdict written",
                kind="timeout",
                detail=detail,
            )
        code = os.waitstatus_to_exitcode(status)  # negative = killed by signal
        if code != 0:
            raise ScorerError(
                f"scorer exited {code} with no verdict written",
                kind="crash",
                exit_code=code,
                detail=detail,
            )
        raise ScorerError(
            "scorer exited 0 without writing a verdict",
            kind="garbage",
            exit_code=0,
            detail=detail,
        )


def main(argv: list[str] | None = None) -> int:
    """Child half of :func:`run_scorer`: read ``{"entrypoint": "module:callable",
    "task": {...}}`` as JSON on stdin, import + call the entrypoint with the task dict,
    atomically write the normalized verdict to the result file (the single positional
    argument) and mirror it on stdout. Any failure exits non-zero *without* writing, so
    the parent classifies it as ``crash``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m shinken.scorer_proc <result-file>", file=sys.stderr)
        return 2
    spec = json.load(sys.stdin)
    mod_name, _, fn_name = str(spec.get("entrypoint", "")).partition(":")
    if not mod_name or not fn_name:
        print(
            f"bad entrypoint {spec.get('entrypoint')!r} (want 'module:callable')",
            file=sys.stderr,
        )
        return 2
    fn = getattr(importlib.import_module(mod_name), fn_name)
    verdict = _normalize_verdict(fn(spec.get("task") or {}))
    _atomic_write_json(args[0], verdict)
    print(json.dumps(verdict))  # log mirror; the result file is the authoritative channel
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(main())
