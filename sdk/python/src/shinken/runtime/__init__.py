"""Shinken agent runtime — the narrow waist.

Consumer-neutral primitives that eval/training/interactive agents (and unforeseen
consumers) all compose: a provider-backed :class:`Session` (observe/act + runtime-state),
:func:`~shinken.runtime.loop.rollout` producing a :class:`~shinken.runtime.trajectory.Trajectory`,
and a :class:`~shinken.runtime.workloads.Workload` registry. There is **no** Scorer, Reward,
or Task here — that is the whole point (see ``docs/design/agent-runtime.md``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from shinken import providers
from shinken._lifecycle import connect_owned_handle

from . import workloads
from .loop import rollout
from .trajectory import EXIT_REASONS, Step, Trajectory, resolve_exit_reason
from .workloads import Workload, WorkloadError


@runtime_checkable
class Session(Protocol):
    """A connected, provider-backed sandbox: the unit a rollout drives. The sync
    ``shinken.Sandbox`` satisfies this structurally."""

    def observe(self, structured: bool = False) -> dict: ...
    def act_batch(self, actions: list[dict], **kwargs: Any) -> dict: ...
    def checkpoint(self, name: str | None = None) -> str: ...
    def fork(self) -> Any: ...
    def resume(self, handle_or_checkpoint: Any) -> Any: ...
    def close(self) -> None: ...


class Runtime:
    """Plumbing handed to a :class:`Workload`: open provider-backed sessions and run
    rollouts. Pure mechanism — it knows nothing about tasks, scores, or rewards.

    The provider is resolved **by name** through the provider registry, so any official or
    out-of-tree (private) provider works; the default is the connect-only ``external``
    provider (attach to an already-running ``shinkend`` at ``addr``)."""

    def __init__(self, provider: str = "external", **provider_kwargs: Any):
        self.provider_name = provider
        self._provider_kwargs = provider_kwargs
        self._provider: Any = None

    @property
    def provider(self) -> Any:
        if self._provider is None:
            self._provider = providers.get(self.provider_name, **self._provider_kwargs)
        return self._provider

    def open(self, spec: Any = None) -> Session:
        """Create + connect a sandbox via the resolved provider; returns a live Session."""
        provider = self.provider
        handle = provider.create(spec)
        return connect_owned_handle(provider, handle)

    def rollout(self, session: Any, agent: Any, **kwargs: Any) -> Trajectory:
        """Convenience: run a semantic-free rollout (see :func:`shinken.runtime.loop.rollout`)."""
        return rollout(session, agent, **kwargs)


__all__ = [
    "EXIT_REASONS",
    "Runtime",
    "Session",
    "Step",
    "Trajectory",
    "Workload",
    "WorkloadError",
    "resolve_exit_reason",
    "rollout",
    "workloads",
]
