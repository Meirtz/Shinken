"""rollout — the narrow waist's observe→decide→act→record loop.

Semantic-free: no scorer, no reward, no task. It drives any ``operator.Agent`` against any
``Session`` and returns a :class:`Trajectory`. Every consumer (eval, train, interactive,
red-team, …) composes this; none of them are visible here. It is a *convenience* over
``session.observe()`` / ``session.act_batch()`` — a caller may always drive those directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .trajectory import Step, Trajectory


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
    not done), ``"stopped"`` (``stop`` predicate fired), or ``"max_steps"``. No verdict is
    computed — that is a consumer's job, applied to the returned :class:`Trajectory`."""
    _observe = observe or (lambda s: s.observe())
    steps: list[Step] = []
    terminal = "max_steps"
    for i in range(max_steps):
        obs = _observe(session)
        decision = agent.decide(obs)
        actions = list(getattr(decision, "actions", None) or [])
        if actions:
            session.act_batch(actions)
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
    return Trajectory(steps=steps, terminal=terminal, metadata=dict(metadata or {}))
