"""Workload — the unified, deliberately under-specified consumer surface.

Every way of *using* the runtime — eval, training, an interactive agent harness, red-team,
regression, replay, and uses we have not thought of yet — is a ``Workload``. The protocol
unifies **entry + discovery**, never **behaviour**: it is a single method, ``run(rt, **params)
-> Any``. It MUST NEVER grow ``score``/``reward``/``terminal``/``task`` — that is exactly what
would re-narrow the scope back to "an eval framework". ``run(rt) -> Any`` is as open as a
plain callable; the registry adds naming, discovery, a uniform CLI, and — like providers —
out-of-tree plugin loading so a private workload (e.g. an internal training pipeline) ships
without any reference in a tracked file.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

_PLUGIN_ENV = "SHINKEN_WORKLOAD_PLUGINS"
_REGISTRY: dict[str, Callable[..., Workload]] = {}
_PLUGINS_LOADED = False


@runtime_checkable
class Workload(Protocol):
    """Anything runnable against the runtime. Return type is the workload's own (eval → a
    summary, train → a dataset/handle, interactive → a session handle, …)."""

    name: str

    def run(self, rt: Any, **params: Any) -> Any: ...


class WorkloadError(ValueError):
    """Unknown workload name (lists what is available)."""


def register(name: str, factory: Callable[..., Workload]) -> None:
    """Map a workload ``name`` to a factory. Out-of-tree plugins call this at import time."""
    _REGISTRY[name] = factory


def load_plugins(env_var: str = _PLUGIN_ENV) -> None:
    """Import each ``:``-separated module named in ``$SHINKEN_WORKLOAD_PLUGINS`` (idempotent).
    Each module registers its workload as an import side effect. Empty by default."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    for mod in filter(None, (m.strip() for m in os.environ.get(env_var, "").split(":"))):
        importlib.import_module(mod)
    # Mark loaded only after all imports succeed, so a failed import is retryable on the
    # next call rather than permanently disabled by an early flag flip.
    _PLUGINS_LOADED = True


def get(name: str, **kwargs: Any) -> Workload:
    """Resolve a registered workload by ``name`` (loading out-of-tree plugins first)."""
    load_plugins()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise WorkloadError(f"unknown workload {name!r}; available: {available()}")
    return factory(**kwargs)


def available() -> list[str]:
    """Sorted names of all registered workloads (after loading out-of-tree plugins)."""
    load_plugins()
    return sorted(_REGISTRY)
