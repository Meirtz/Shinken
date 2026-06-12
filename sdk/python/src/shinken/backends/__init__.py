"""Operation-layer backends — drive the Shinken ACI over a *third-party* computer-control
system instead of (or alongside) the native ``shinkend`` guest runtime.

Shinken maintains its own backend (``shinkend`` behind :class:`~shinken.providers.docker.
DockerLocalProvider`), but the operation layer is a narrow waist: anything that presents the
verb surface a :class:`~shinken.client.Sandbox` exposes — ``screenshot``/``click``/
``type_text``/``key``/``scroll``/``exec``/``observe``/``clipboard_*``/``screen_size`` — can
sit underneath it. A backend adapter wraps an external driver (trycua/cua, e2b-desktop, an
AX MCP server, …) as a duck-typed Sandbox returned by a :class:`~shinken.providers.base.
SandboxProvider` subclass, so the *inherited* ``provider.session()`` lifecycle and every
Sandbox consumer (the operator loop, model adapters, the fork-native gym where the substrate
allows it) work unchanged.

Each backend advertises **honest capabilities**: a backend with no checkpoint/fork leaves
``supports_fork=False`` (and the inherited ``checkpoint``/``resume`` raise
:class:`~shinken.providers.base.UnsupportedProviderOperation`), and the Sandbox's
``capabilities`` envelope lists only the verbs/observation types it really serves — so a
consumer degrades loudly, never silently.

Registry::

    from shinken.backends import get_backend, register_backend, list_backends
    provider = get_backend("cua", interface_factory=...)   # a SandboxProvider
    with provider.session() as env:
        env.click(x=640, y=420); env.type_text("hello")

Adapters are lazy-imported on first use, so importing this package never requires the
third-party SDKs to be installed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shinken.providers.base import SandboxProvider

__all__ = ["register_backend", "get_backend", "list_backends"]

#: name -> zero-arg-or-kwargs factory returning a configured SandboxProvider.
_REGISTRY: dict[str, Callable[..., SandboxProvider]] = {}


def register_backend(name: str, factory: Callable[..., SandboxProvider]) -> None:
    """Register an operation-layer backend by name. ``factory(**kwargs)`` returns a
    :class:`~shinken.providers.base.SandboxProvider` whose ``connect()`` yields a
    duck-typed Sandbox over the third-party driver."""
    _REGISTRY[name] = factory


def list_backends() -> list[str]:
    """Names of registered backends (built-ins are registered on first import)."""
    _ensure_builtins()
    return sorted(_REGISTRY)


def get_backend(name: str, **kwargs: Any) -> SandboxProvider:
    """Instantiate a registered backend provider. Raises ``KeyError`` with the known
    names if ``name`` is unregistered."""
    _ensure_builtins()
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown backend {name!r}; registered: {sorted(_REGISTRY)} "
            "(register your own with shinken.backends.register_backend)"
        ) from None
    return factory(**kwargs)


_BUILTINS_LOADED = False


def _ensure_builtins() -> None:
    """Lazy-register the in-tree backends without importing their third-party SDKs."""
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    from .browser_runtime import BrowserRuntimeBackend
    from .cua import CuaBackendProvider
    from .mcp_computer import McpComputerBackend

    register_backend("cua", CuaBackendProvider)
    # iFurySt/open-codex-computer-use (and any codex-style 9-tool MCP computer-use server)
    register_backend("mcp-computer", McpComputerBackend)
    # the BU half: a CDP browser runtime (e.g. iFurySt/open-browser-use) — D13 §10
    register_backend("browser-runtime", BrowserRuntimeBackend)
