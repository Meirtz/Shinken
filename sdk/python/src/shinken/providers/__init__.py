"""Sandbox provider implementations + a name-keyed registry.

Providers are resolved by name through :func:`get`, so a caller (CLI, benchmark/eval
workload, runtime) never hard-codes a concrete class. Only **official** providers are
registered in-tree (``docker``, ``external``). A **private** provider (e.g. an internal
cloud-sandbox substrate) ships out-of-tree and registers itself when imported; point
``$SHINKEN_PROVIDER_PLUGINS`` at its module(s) and :func:`load_plugins` will import them.
``$SHINKEN_PROVIDER_PLUGINS`` defaults empty, so a fresh public clone behaves identically
whether or not a private plugin is present, and no private provider name appears in any
tracked file.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable

from .base import (
    GcReport,
    ProviderCapabilities,
    ProviderError,
    SandboxHandle,
    SandboxHealth,
    SandboxProvider,
    SandboxSpec,
    UnsupportedProviderOperation,
)
from .criu import CriuDockerProvider
from .docker import DockerLocalProvider
from .external import ExternalProvider

_PLUGIN_ENV = "SHINKEN_PROVIDER_PLUGINS"
_REGISTRY: dict[str, Callable[..., SandboxProvider]] = {}
_PLUGINS_LOADED = False


def register(name: str, factory: Callable[..., SandboxProvider]) -> None:
    """Map a provider ``name`` to a zero/kw-arg ``factory``. Out-of-tree plugins call this
    at import time, so a private provider is reachable by name with no in-tree reference."""
    _REGISTRY[name] = factory


def load_plugins(env_var: str = _PLUGIN_ENV) -> None:
    """Import each ``:``-separated module named in ``$SHINKEN_PROVIDER_PLUGINS`` (idempotent).
    Each module registers its provider as an import side effect. Empty by default."""
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    for mod in filter(None, (m.strip() for m in os.environ.get(env_var, "").split(":"))):
        importlib.import_module(mod)
    # Mark loaded only after all imports succeed, so a failed import is retryable.
    _PLUGINS_LOADED = True


def get(name: str, **kwargs: object) -> SandboxProvider:
    """Resolve a registered provider by ``name`` (loading out-of-tree plugins first)."""
    load_plugins()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ProviderError(f"unknown provider {name!r}; available: {available()}")
    return factory(**kwargs)


def available() -> list[str]:
    """Sorted names of all registered providers (after loading out-of-tree plugins)."""
    load_plugins()
    return sorted(_REGISTRY)


# Official providers — the only provider names that ever appear in a tracked file.
register("docker", DockerLocalProvider)
register("docker-criu", CriuDockerProvider)
register("external", ExternalProvider)

__all__ = [
    "CriuDockerProvider",
    "DockerLocalProvider",
    "ExternalProvider",
    "GcReport",
    "ProviderCapabilities",
    "ProviderError",
    "SandboxHandle",
    "SandboxHealth",
    "SandboxProvider",
    "SandboxSpec",
    "UnsupportedProviderOperation",
    "available",
    "get",
    "load_plugins",
    "register",
]
