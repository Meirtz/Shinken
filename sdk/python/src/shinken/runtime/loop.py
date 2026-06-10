"""rollout — the narrow waist's observe→decide→act→record loop.

Semantic-free: no scorer, no reward, no task. It drives any ``operator.Agent`` against any
``Session`` and returns a :class:`Trajectory`. Every consumer (eval, train, interactive,
red-team, …) composes this; none of them are visible here. It is a *convenience* over
``session.observe()`` / ``session.act_batch()`` — a caller may always drive those directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from shinken.errors import SandboxDied, is_connection_loss

from .trajectory import Step, Trajectory

#: terminal sentinel -> trajectory ``exit_reason`` (#56). ``stuck`` is an agent fault
#: (the agent produced neither actions nor done); ``stopped`` is a consumer-requested
#: stop, recorded as ``task_complete`` (the loop reached *its* terminal condition).
_EXIT_REASON_FOR_TERMINAL = {
    "done": "task_complete",
    "stopped": "task_complete",
    "stuck": "agent_error",
    "max_steps": "max_steps",
}


def rollout(
    session: Any,
    agent: Any,
    *,
    max_steps: int = 20,
    observe: Callable[[Any], dict] | None = None,
    stop: Callable[[Any, list[Step]], bool] | None = None,
    on_step: Callable[[Step], None] | None = None,
    metadata: dict | None = None,
) -> Trajectory:
    """Drive ``agent`` (an ``operator.Agent`` with ``decide(obs) -> Decision``) against
    ``session`` for up to ``max_steps`` turns, recording each turn as a :class:`Step`.

    Terminal sentinels: ``"done"`` (agent reported completion), ``"stuck"`` (no actions and
    not done), ``"stopped"`` (``stop`` predicate fired), ``"max_steps"``, or ``"aborted"``
    (an exception mid-loop — recorded, never raised, so a failed rollout still yields a
    structurally valid record, #56). The trajectory's ``exit_reason`` is set in every case:
    an abort classifies as ``sandbox_died`` (infra death — the typed/connection-loss
    family) or ``agent_error`` (everything else), with the message kept in
    ``metadata["error"]``. No verdict is computed — that is a consumer's job, applied to
    the returned :class:`Trajectory`."""
    _observe = observe or (lambda s: s.observe())
    steps: list[Step] = []
    terminal = "max_steps"
    exit_reason: str | None = None
    error: str | None = None
    for i in range(max_steps):
        try:
            obs = _observe(session)
            decision = agent.decide(obs)
            actions = list(getattr(decision, "actions", None) or [])
            if actions:
                session.act_batch(actions)
        except Exception as exc:  # noqa: BLE001 — classify into exit_reason, never crash a batch
            terminal = "aborted"
            exit_reason = (
                "sandbox_died"
                if isinstance(exc, SandboxDied) or is_connection_loss(exc)
                else "agent_error"
            )
            error = f"{exit_reason}: {exc}"
            break
        step = Step(index=i, observation=obs, actions=actions, note=getattr(decision, "note", None))
        steps.append(step)
        if on_step is not None:
            on_step(step)
        if getattr(decision, "done", False):
            terminal = "done"
            break
        if not actions:
            terminal = "stuck"
            break
        if stop is not None and stop(session, steps):
            terminal = "stopped"
            break
    if exit_reason is None:
        exit_reason = _EXIT_REASON_FOR_TERMINAL[terminal]
    meta = dict(metadata or {})
    if error is not None:
        meta["error"] = error
    return Trajectory(steps=steps, terminal=terminal, exit_reason=exit_reason, metadata=meta)
