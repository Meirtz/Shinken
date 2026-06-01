"""Trajectory — the universal record of an agent driving a session.

Consumer-neutral by construction: a trajectory is the same artifact whether it feeds
training (state-action data), evaluation (evidence behind a verdict), or audit/replay.
It carries NO verdict and NO reward — a consumer attaches those out-of-band, keyed by
step/episode, so the record never bakes in eval/train semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Step:
    """One turn: what the agent saw and the canonical ACI action(s) it took."""

    index: int
    observation: dict  # ACI observation (coordinate-space tagged); capture policy decides retention
    actions: list[dict] = field(default_factory=list)  # canonical ACI actions (post-adapter)
    note: str | None = None  # agent rationale / control note, if any
    info: dict = field(default_factory=dict)  # raw event passthrough (model output, decisions, …)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "observation": self.observation,
            "actions": self.actions,
            "note": self.note,
            "info": self.info,
        }


@dataclass
class Trajectory:
    """An ordered list of :class:`Step`\\ s plus a consumer-defined terminal sentinel and
    metadata. ``terminal`` is a sentinel (``"done"``/``"stuck"``/``"stopped"``/…), **not** a
    pass/fail verdict. ``metadata`` records provider/model/seed/checkpoints — never secrets."""

    steps: list[Step] = field(default_factory=list)
    terminal: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def num_actions(self) -> int:
        return sum(len(s.actions) for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "terminal": self.terminal,
            "num_actions": self.num_actions,
            "metadata": self.metadata,
        }
