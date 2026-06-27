"""Trajectory — the universal record of an agent driving a session.

Consumer-neutral by construction: a trajectory is the same artifact whether it feeds
training (state-action data), evaluation (evidence behind a verdict), or audit/replay.
It carries NO verdict and NO reward — a consumer attaches those out-of-band, keyed by
step/episode, so the record never bakes in eval/train semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Trajectory-level exit reasons (#56), **highest precedence first**. ``exit_reason``
#: answers "why did this trajectory stop" with ONE value even when several causes
#: coincide: the earliest entry below wins (see :func:`resolve_exit_reason`).
#: Infrastructure death outranks everything (a retry signal, never a verdict);
#: environment-setup failure outranks an agent fault (the agent never had a fair run);
#: an agent fault outranks a scorer fault (a broken rollout has nothing to score); a
#: scorer fault outranks the step budget (the rollout finished, but its verdict cannot
#: be trusted); only a trajectory with none of those causes is ``max_steps`` (budget
#: exhausted) or ``task_complete`` (reached its terminal condition). Eval's coarser
#: ``RunResult.kind`` maps onto this field (see ``shinken.eval``), and RL trainers
#: consume it as verl's ``extra_fields.traj_exit_reason``
#: (https://github.com/verl-project/uni-agent).
EXIT_REASONS = (
    "sandbox_died",  # the substrate died under the run — infra failure, retry-eligible
    "setup_error",  # env/setup readiness failed before the agent had a fair run
    "agent_error",  # the agent/adapter faulted mid-loop (incl. emitting no action)
    "scorer_error",  # the (isolated) scorer crashed / timed out / wrote no verdict
    "max_steps",  # step budget exhausted without a terminal
    "task_complete",  # the loop reached its terminal condition (verdict attachable)
)


def resolve_exit_reason(*candidates: str | None) -> str | None:
    """Collapse the candidate causes of a stop into the single highest-precedence
    :data:`EXIT_REASONS` value. ``None`` entries are ignored (no such cause); an unknown
    value raises so a typo never silently becomes a low-precedence reason."""
    found = [c for c in candidates if c is not None]
    for c in found:
        if c not in EXIT_REASONS:
            raise ValueError(f"unknown exit_reason {c!r}; expected one of {EXIT_REASONS}")
    if not found:
        return None
    return min(found, key=EXIT_REASONS.index)


@dataclass
class Step:
    """One turn: what the agent saw and the canonical ACI action(s) it took.

    ``observation`` is the policy input *before* ``actions`` (``s_t``).  Producers
    that also observe the result retain it in ``next_observation`` (``s_{t+1}``).
    The latter is optional so existing producers and positional ``Step(...)`` calls
    remain compatible.
    """

    index: int
    observation: dict  # ACI observation (coordinate-space tagged); capture policy decides retention
    actions: list[dict] = field(default_factory=list)  # canonical ACI actions (post-adapter)
    note: str | None = None  # agent rationale / control note, if any
    info: dict = field(default_factory=dict)  # raw event passthrough (model output, decisions, …)
    # --- token-fidelity fields (A-2) — RESERVED for the train Workload (#223) ---------
    # Optional and never populated by current code paths. They exist so a token-level
    # adapter can record what an RL trainer needs for LOSSLESS conversion to verl's
    # AgentLoopOutput shape (prompt_ids / response_ids / response_mask with 1=model,
    # 0=tool tokens / finish reason — https://github.com/verl-project/uni-agent):
    # messages-only records are lossy for RL (retokenization mismatch). When collecting
    # against a token-level inference server the adapter fills these; everyone else
    # leaves them None.
    prompt_token_ids: list[int] | None = None  # token ids of the prompt fed this turn
    response_token_ids: list[int] | None = None  # token ids of the model response this turn
    response_mask: list[int] | None = None  # per response token: 1=model-generated, 0=tool-injected
    finish_reason: str | None = None  # provider finish reason (stop | length | tool_calls | …)
    # Kept last to preserve the historical positional constructor layout. Legacy
    # producers that only retain the policy input leave this as None.
    next_observation: dict | None = None  # post-action observation (s_{t+1}), when captured

    def to_dict(self) -> dict:
        out = {
            "index": self.index,
            "observation": self.observation,
            "actions": self.actions,
            "note": self.note,
            "info": self.info,
            "prompt_token_ids": self.prompt_token_ids,
            "response_token_ids": self.response_token_ids,
            "response_mask": self.response_mask,
            "finish_reason": self.finish_reason,
        }
        # Do not change the serialized shape of legacy/runtime-loop Steps that do not
        # capture a post-action observation. Gym transitions include the field.
        if self.next_observation is not None:
            out["next_observation"] = self.next_observation
        return out


@dataclass
class Trajectory:
    """An ordered list of :class:`Step`\\ s plus a consumer-defined terminal sentinel and
    metadata. ``terminal`` is a sentinel (``"done"``/``"stuck"``/``"stopped"``/…), **not** a
    pass/fail verdict. ``exit_reason`` is the typed, trajectory-level *why-it-stopped*
    field (#56): one of :data:`EXIT_REASONS`, resolved by precedence when several causes
    coincide (:func:`resolve_exit_reason`) — RL trainers read it as verl's
    ``extra_fields.traj_exit_reason``. ``metadata`` records provider/model/seed/checkpoints
    — never secrets."""

    steps: list[Step] = field(default_factory=list)
    terminal: str | None = None
    exit_reason: str | None = None  # one of EXIT_REASONS (precedence-resolved), or None
    metadata: dict = field(default_factory=dict)

    @property
    def num_actions(self) -> int:
        return sum(len(s.actions) for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "terminal": self.terminal,
            "exit_reason": self.exit_reason,
            "num_actions": self.num_actions,
            "metadata": self.metadata,
        }
