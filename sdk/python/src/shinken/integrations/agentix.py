"""Agentix interop — a Shinken provider behind Agentix's ``SandboxProvider`` shape.

Agentix-Project/Agentix (<https://github.com/Agentix-Project/Agentix>, surveyed at commit
``41d2ac9c``) orchestrates eval/rollout collection over sandbox backends through one
``runtime_checkable`` Protocol (``agentix/provider/base.py``):

- ``async create(config) -> Sandbox`` / ``async delete(sandbox_id)`` /
  ``async get(sandbox_id) -> SandboxInfo``, plus an inherited ``session(config, *,
  call_deadline=None)`` async context manager (create → yield → close client → delete);
- ``SandboxConfig`` carries ``image`` (task base image), ``bundle`` (their ``/nix`` runtime
  overlay), ``platform``, ``env``, and a ``resource`` request (``cpu``/``memory``/``gpu``);
- the live ``Sandbox`` handle is ``(sandbox_id, runtime_url, status, call_deadline)`` with a
  lazily-connected RPC client (``await sandbox.remote(fn, ...)`` — pickled Python over
  Socket.IO to a runtime server their bundle boots in-container);
- backends register under the ``agentix.provider`` entry-point group, or in-process via
  ``register_provider(name, cls)`` for tests/dynamic use.

:class:`ShinkenAgentixProvider` satisfies that Protocol **structurally** (no agentix
import; ``isinstance(provider, SandboxProvider)`` passes when agentix is installed, since
``runtime_checkable`` checks method presence). Differences, stated rather than papered over:

- **Runtime**: ``config.bundle`` (their runtime overlay) is accepted and recorded but not
  mounted — the guest runtime here is ``shinkend``, already baked into the Shinken image,
  and ``runtime_url`` names its typed-ACI WebSocket (``ws://host:port``), not an Agentix
  runtime server. The handle therefore exposes :meth:`ShinkenAgentixSandbox.aci` (a
  connected Shinken session: screenshot/click/type/checkpoint/...) instead of pickle-RPC;
  ``remote()`` raises ``NotImplementedError`` pointing there, never silently no-ops.
- **Runtime state**: construct with ``golden=<checkpoint id>`` and every ``create()``
  materializes the sandbox **from that checkpoint** instead of cold-booting — the
  fork-native rollout lifecycle Agentix roadmaps ("checkpoint … then fork") but does not
  ship; Shinken's Docker disk tier ships it today.

No Agentix code is imported or copied; the shape is fixture-tested against a frozen mirror
of the surveyed Protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from shinken.errors import SandboxDied, ShinkenError
from shinken.providers.base import SandboxSpec


class AgentixInteropError(ShinkenError):
    """The Agentix-shaped provider could not complete an operation."""


@dataclass
class ShinkenSandboxInfo:
    """Their ``SandboxInfo`` shape: ``(sandbox_id, runtime_url, status)``."""

    sandbox_id: str
    runtime_url: str
    status: str = "running"


@dataclass
class ShinkenAgentixSandbox:
    """Their live ``Sandbox`` handle shape, carrying the typed ACI instead of pickle-RPC.

    ``sandbox_id``/``runtime_url``/``status``/``call_deadline`` mirror Agentix's handle;
    :meth:`aci` lazily connects a Shinken session (their handle connects its RPC client
    lazily the same way), and ``aclose()``/``async with`` close it without deleting the
    container — deletion belongs to the provider, exactly as in their contract."""

    sandbox_id: str
    runtime_url: str
    status: str = "running"
    call_deadline: float | None = None
    _provider: Any = field(default=None, repr=False, compare=False)
    _handle: Any = field(default=None, repr=False, compare=False)
    _session: Any = field(default=None, init=False, repr=False, compare=False)

    def aci(self) -> Any:
        """The typed-ACI session for this sandbox (connected lazily, cached). This is the
        Shinken counterpart of their ``sandbox.remote(...)`` surface: GUI observation and
        actions are typed verbs, not pickled functions."""
        if self._session is None:
            if self._provider is None or self._handle is None:
                raise AgentixInteropError("sandbox handle has no provider context")
            self._session = self._provider.connect(self._handle)
        return self._session

    async def remote(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "Shinken sandboxes speak the typed ACI, not pickle-RPC — use sandbox.aci() "
            "(screenshot/click/type_text/checkpoint/...) instead of remote(fn, ...)"
        )

    async def health(self) -> dict:
        """Provider health probe → a plain dict (``status``/``detail``/``rtt_ms``)."""
        if self._provider is None or self._handle is None:
            raise AgentixInteropError("sandbox handle has no provider context")
        h = await asyncio.to_thread(self._provider.health, self._handle)
        return {
            "status": "ok" if getattr(h, "ok", False) else "error",
            "detail": getattr(h, "detail", ""),
            "rtt_ms": getattr(h, "rtt_ms", None),
        }

    async def aclose(self) -> None:
        """Close the ACI session (idempotent). Does not delete the container."""
        if self._session is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._session.close)
            self._session = None

    async def __aenter__(self) -> ShinkenAgentixSandbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def _spec_from_config(config: Any) -> tuple[SandboxSpec, dict]:
    """Map an Agentix ``SandboxConfig``-shaped object (or dict, or None) onto a Shinken
    :class:`SandboxSpec`. Duck-typed: only field names are read, nothing is imported.

    Honesty notes: ``bundle``/``platform``/``env`` are recorded in spec metadata but not
    applied by the local Docker provider (no runtime overlay; host platform; no env
    injection in ``SandboxSpec`` yet); a ``resource.gpu`` request raises rather than being
    silently dropped."""

    def read(name: str) -> Any:
        if config is None:
            return None
        if isinstance(config, dict):
            return config.get(name)
        return getattr(config, name, None)

    resource = read("resource")

    def res(name: str) -> Any:
        if resource is None:
            return None
        if isinstance(resource, dict):
            return resource.get(name)
        return getattr(resource, name, None)

    if res("gpu"):
        raise AgentixInteropError(
            "resource.gpu requested but the local Docker provider does not support GPU "
            "(ProviderCapabilities.supports_gpu=False)"
        )
    memory = res("memory")
    if isinstance(memory, int):
        memory = f"{memory}b"  # their int form is bytes; docker takes the unit-suffixed string
    cpu = res("cpu")
    ignored = {k: v for k, v in (("bundle", read("bundle")), ("platform", read("platform"))) if v}
    requested_env = read("env") or None
    metadata: dict = {}
    if ignored:
        metadata["agentix_ignored"] = ignored  # accepted, recorded, not applied
    if requested_env:
        metadata["agentix_requested_env"] = dict(requested_env)  # not injected (documented)
    spec = SandboxSpec(
        image=read("image") or None,
        memory=str(memory) if memory is not None else None,
        cpus=float(cpu) if cpu is not None else None,
        metadata=metadata,
    )
    return spec, metadata


class ShinkenAgentixProvider:
    """Agentix-``SandboxProvider``-shaped lifecycle over a Shinken provider.

    ``provider`` is any Shinken :class:`~shinken.providers.base.SandboxProvider` (default:
    ``DockerLocalProvider``). With ``golden=<checkpoint id>``, ``create()`` resumes every
    sandbox from that single golden checkpoint (fork-native rollouts) instead of
    cold-creating — note the resumed sandbox then boots from the checkpointed image and
    the per-call ``config.image`` is not consulted."""

    def __init__(self, provider: Any = None, *, golden: str | None = None) -> None:
        if provider is None:
            from shinken.providers.docker import DockerLocalProvider

            provider = DockerLocalProvider()
        self.provider = provider
        self.golden = golden
        self._handles: dict[str, Any] = {}

    # --- the Protocol's three lifecycle methods -------------------------------------------

    async def create(self, config: Any = None) -> ShinkenAgentixSandbox:
        spec, _meta = _spec_from_config(config)
        if self.golden is not None:
            handle = await asyncio.to_thread(self.provider.resume, self.golden)
        else:
            handle = await asyncio.to_thread(self.provider.create, spec)
        sandbox_id = str(getattr(handle, "sandbox_id", None) or f"shinken-{uuid.uuid4().hex[:8]}")
        self._handles[sandbox_id] = handle
        return ShinkenAgentixSandbox(
            sandbox_id=sandbox_id,
            runtime_url=f"ws://{getattr(handle, 'addr', '')}",
            status="running",
            _provider=self.provider,
            _handle=handle,
        )

    async def delete(self, sandbox_id: str) -> None:
        """Destroy the sandbox. Tolerant of unknown/already-deleted ids (their docker
        backend's ``rm -f`` semantics)."""
        handle = self._handles.pop(str(sandbox_id), None)
        if handle is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.provider.destroy, handle)

    async def get(self, sandbox_id: str) -> ShinkenSandboxInfo:
        """Inspect a sandbox → ``SandboxInfo`` shape. ``KeyError`` for an unknown id
        (their docker backend's contract)."""
        handle = self._handles.get(str(sandbox_id))
        if handle is None:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        status = "running"
        check = getattr(self.provider, "check_alive", None)
        if check is not None:
            try:
                await asyncio.to_thread(check, handle)
            except SandboxDied as died:
                status = f"exited({died.exit_code})" if died.exit_code is not None else "exited"
            except Exception:
                status = "unknown"
        return ShinkenSandboxInfo(
            sandbox_id=str(sandbox_id),
            runtime_url=f"ws://{getattr(handle, 'addr', '')}",
            status=status,
        )

    # --- their inherited session() helper, same semantics ---------------------------------

    @contextlib.asynccontextmanager
    async def session(
        self, config: Any = None, *, call_deadline: float | None = None
    ) -> AsyncIterator[ShinkenAgentixSandbox]:
        """Scoped sandbox: created on entry; its session closed and the sandbox deleted on
        exit (mirrors Agentix's ``SandboxProvider.session``)."""
        sandbox = await self.create(config)
        sandbox.call_deadline = call_deadline
        try:
            yield sandbox
        finally:
            await sandbox.aclose()
            await self.delete(sandbox.sandbox_id)

    # --- runtime-state extras (beyond their Protocol; the Shinken differentiator) ---------

    async def checkpoint(self, sandbox_id: str, *, name: str | None = None) -> str:
        """Checkpoint a live sandbox → a checkpoint id usable as ``golden=``. This is the
        primitive their rollout lifecycle lacks: golden-checkpoint once, fork per rollout."""
        handle = self._handles.get(str(sandbox_id))
        if handle is None:
            raise KeyError(f"Sandbox not found: {sandbox_id}")
        return await asyncio.to_thread(self.provider.checkpoint, handle, name=name)


def register_with_agentix(name: str = "shinken") -> None:
    """Register :class:`ShinkenAgentixProvider` in an *installed* Agentix's in-process
    provider registry (their test/dynamic path; packaged installs would use the
    ``agentix.provider`` entry-point group). Soft import — raises a clear error when
    Agentix is not installed; this module never hard-depends on it."""
    try:
        from agentix.provider.base import register_provider  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only with agentix installed
        raise AgentixInteropError(
            "agentix is not installed; `pip install agentixx` to register the provider"
        ) from exc
    register_provider(name, ShinkenAgentixProvider)  # pragma: no cover
