"""Provider-agnostic Operator loop — drive an agent through a task (#6 / M3).

The Operator loop is the *primary* path for a computer-use agent (low-level `env.click`
calls are the debug/control path): **observe → decide → act → repeat**, until the agent
reports the task done. It is provider-agnostic — any object with
``decide(observation) -> Decision`` works, including an off-the-shelf model wrapped by a
CU adapter (#75/#76), whose tool-calls translate to ACI actions. Each turn's actions are
dispatched as an ordered batch (#73), and the whole run is recorded into the session's
`.skn` bundle, so an agentic run is exactly one replay.

A model-backed agent is a thin wrapper, e.g.::

    class ModelAgent:
        def __init__(self, model, adapter): ...
        def decide(self, obs):
            tool_result = self.adapter.to_tool_result(obs)       # screenshot → provider shape
            tool_calls = self.model(tool_result)                 # one model turn (live API)
            actions = [self.adapter.to_aci_action(tc) for tc in tool_calls]
            return Decision(actions=actions, done=not tool_calls)

The :class:`ScriptedAgent` below is the deterministic reference driver used in tests and
fixtures (no model required).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Decision:
    """An agent's response to one observation: the ACI actions to take this turn, and
    whether the task is complete. ``done=True`` ends the loop after the actions run; a
    turn with no actions and ``done=False`` also stops the loop (the agent is stuck)."""

    actions: list[dict] = field(default_factory=list)
    done: bool = False
    note: str | None = None


class Agent(Protocol):
    """Anything that maps an observation to a :class:`Decision`."""

    def decide(self, observation: dict) -> Decision: ...


@dataclass
class DriveResult:
    steps: int  # turns taken
    actions: int  # total ACI actions dispatched
    done: bool  # the agent reported task completion
    stopped: str  # why the loop ended: "done" | "max_steps" | "stuck"

    def to_dict(self) -> dict:
        return {
            "steps": self.steps,
            "actions": self.actions,
            "done": self.done,
            "stopped": self.stopped,
        }


def drive(
    env: Any,
    agent: Agent,
    *,
    max_steps: int = 20,
    structured: bool = False,
    observe: Callable[[Any], dict] | None = None,
) -> DriveResult:
    """Run the Operator loop against ``env`` (connect with ``record=True`` to capture the
    whole run as one `.skn`).

    Each turn observes (a screenshot by default, or a structured a11y capture with
    ``structured=True``, or a custom ``observe(env)``), asks ``agent`` for a
    :class:`Decision`, executes its actions as an ordered batch (#73), and repeats until
    the agent reports ``done`` or ``max_steps`` is reached. Returns a :class:`DriveResult`.
    """
    _observe = observe or (lambda e: e.observe(structured=structured))
    total = 0
    stopped = "max_steps"
    step = 0
    while step < max_steps:
        step += 1
        obs = _observe(env)
        decision = agent.decide(obs)
        if decision.actions:
            env.act_batch(decision.actions, batch_id=f"turn-{step}")
            total += len(decision.actions)
        if decision.done:
            stopped = "done"
            break
        if not decision.actions:
            stopped = "stuck"
            break
    return DriveResult(steps=step, actions=total, done=(stopped == "done"), stopped=stopped)


class ScriptedAgent:
    """A deterministic agent that emits a fixed plan of per-turn action batches — the
    reference Operator-loop driver for tests and fixtures (no model required).

    ``plan`` is a list of turns; each turn is a list of ACI action dicts. The agent
    reports ``done`` once the plan is exhausted."""

    def __init__(self, plan: list[list[dict]]):
        self._plan = [list(turn) for turn in plan]
        self._i = 0

    def decide(self, observation: dict) -> Decision:
        if self._i >= len(self._plan):
            return Decision(actions=[], done=True)
        actions = self._plan[self._i]
        self._i += 1
        return Decision(actions=actions, done=(self._i >= len(self._plan)))


def agent_task(
    name: str,
    agent_factory: Callable[[], Agent],
    verify: Callable[[Any], Any],
    *,
    max_steps: int = 20,
    setup: Callable[[Any], None] | None = None,
):
    """Wrap an agent + verifier into an eval :class:`~shinken.eval.Task`, so the Operator
    loop composes with ``run_eval`` (N replicas → pass-rate). ``agent_factory`` is called
    per replica so each run gets a fresh (possibly stateful) agent; ``verify`` judges the
    run from its `.skn` replay."""
    from .eval import Task

    def run(env: Any) -> None:
        drive(env, agent_factory(), max_steps=max_steps)

    return Task(name=name, run=run, verify=verify, setup=setup)
