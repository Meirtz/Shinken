"""Sandbox provider contracts.

Providers own sandbox lifecycle. The ACI client owns the session once a provider
returns an address and token for a running ``shinkend``.

**Runtime-state canon (the one set of verbs — D5):**

- ``snapshot(handle)`` — capture substrate state → a snapshot id (Docker disk tier:
  ``docker commit`` → an image tag).
- ``checkpoint(handle)`` — a NAMED restore point binding a snapshot to optional agent
  state (the node in the checkpoint DAG) → a checkpoint id. A checkpoint *contains* a
  snapshot; both ids are accepted wherever state is restored.
- ``restore(id)`` — **the real verb**: materialize a NEW live sandbox from a
  snapshot/checkpoint id. Restoring never mutates the source: restoring while the
  source sandbox is alive mints a SIBLING.
- ``fork(handle)`` — snapshot + restore of a LIVE sandbox in one call.
- ``resume(id)`` — **deprecated alias of** ``restore`` (kept for back-compat). It has
  RESTORE semantics, not pause/unpause semantics.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from shinken.client import Sandbox, connect

# Structured routing semantics for providers (#206 / D1/D5). Enums keep the descriptor
# honest and machine-routable for future Firecracker/QEMU/gVisor/Kata tiers.
ResetStrategy = Literal["recreate", "provider_managed", "snapshot_restore", "fork_from_snapshot"]
TransportKind = Literal["tcp_ws", "vsock", "provider_managed"]
IsolationKind = Literal["container", "gvisor", "kata", "microvm", "vm", "provider_managed"]
DisplayKind = Literal["none", "x11", "wayland", "browser", "provider_managed"]
# What a provider's snapshot captures: nothing, filesystem only, full memory, process
# tree (CRIU), or an opaque provider-managed snapshot.
SnapshotKind = Literal["none", "disk", "memory", "process", "provider_managed"]


class ProviderError(RuntimeError):
    """Raised when a sandbox provider cannot complete a lifecycle operation."""


class UnsupportedProviderOperation(ProviderError):
    """Raised when a provider does not support snapshot/fork-style semantics."""


@dataclass(frozen=True)
class ProviderCapabilities:
    """Honest capabilities advertised by a sandbox provider.

    Boolean fields are convenience flags; the structured fields carry the routing
    semantics the Control Plane needs to pick a provider for a runtime-state operation."""

    name: str
    supports_lifecycle: bool
    supports_gui: bool
    supports_snapshot: bool = False
    supports_fork: bool = False
    supports_gpu: bool = False
    supports_vsock: bool = False
    supports_egress_policy: bool = False
    supports_checkpoint: bool = False
    supports_resume: bool = False
    reset_strategy: ResetStrategy = "recreate"
    isolation: IsolationKind = "provider_managed"
    transport: TransportKind = "provider_managed"
    display: DisplayKind = "provider_managed"
    snapshot_kind: SnapshotKind = "none"
    tier: str = "unspecified"
    max_sessions: int | None = None
    # Loud privilege posture: True when this provider's containers/VMs must run
    # PRIVILEGED (e.g. in-container CRIU needs CAP_SYS_ADMIN) — a routing signal that
    # the tier trades isolation for latency/state fidelity, never a security posture.
    requires_privileged: bool = False
    notes: tuple[str, ...] = ()


@dataclass
class SandboxSpec:
    """Provider-agnostic request for a sandbox."""

    image: str | None = None
    os: str = "linux"
    needs_gui: bool = True
    needs_gpu: bool = False
    fast_reset: bool = False
    memory: str | None = None
    cpus: float | None = None
    pids_limit: int | None = None
    shm_size: str | None = None
    screen_geometry: str = "1280x800x24"
    # Extra guest environment variables (e.g. ``SHINKEND_DAMAGE=off`` to A/B the
    # damage-driven capture path). Provider-reserved names (``SHINKEND_TOKEN``,
    # ``SCREEN_GEOMETRY``) cannot be overridden through this.
    extra_env: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(repr=False)
class SandboxHandle:
    """A running or externally managed sandbox endpoint.

    ``repr()`` REDACTS the bearer token (first four characters + ``…``), so handles
    can be logged/printed without leaking the session credential."""

    provider: str
    sandbox_id: str
    addr: str
    token: str | None = None
    created_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        token = f"{self.token[:4]}…" if self.token else self.token
        return (
            f"SandboxHandle(provider={self.provider!r}, sandbox_id={self.sandbox_id!r}, "
            f"addr={self.addr!r}, token={token!r}, created_at={self.created_at!r}, "
            f"metadata={self.metadata!r})"
        )


@dataclass
class GcReport:
    """What :meth:`SandboxProvider.gc` reclaimed: labeled ``containers`` removed,
    snapshot ``images`` removed, and how many live-owner resources were ``skipped``
    (left alone because their owning process is still running — pass ``force=True``
    to reclaim those too)."""

    containers: int = 0
    images: int = 0
    skipped: int = 0

    def to_dict(self) -> dict:
        return {"containers": self.containers, "images": self.images, "skipped": self.skipped}


@dataclass
class SandboxHealth:
    """Result of a provider readiness check."""

    ok: bool
    ready: bool
    detail: str
    rtt_ms: float | None = None
    screenshot_ms: float | None = None
    screenshot_bytes: int | None = None
    rss_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SandboxProvider:
    """Base class for sandbox lifecycle providers."""

    capabilities: ProviderCapabilities

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        raise NotImplementedError

    def connect(self, handle: SandboxHandle, **connect_kwargs: Any) -> Sandbox:
        """Open a session to the sandbox. Extra keyword arguments are passed through
        to :func:`shinken.connect` (e.g. ``frame_cache=`` to share one
        :class:`~shinken.FrameCache` across a forked fleet, ``screenshot_dedup=``)."""
        env = connect(handle.addr, token=handle.token, **connect_kwargs)
        # Attach the provider context so sandbox.checkpoint()/spawn()/destroy() can use
        # the substrate lifecycle operations (#206).
        env._set_provider_context(self, handle)
        return env

    @contextlib.contextmanager
    def session(self, spec: SandboxSpec | None = None, **connect_kwargs: Any) -> Iterator[Sandbox]:
        """The full sandbox lifecycle as ONE context manager: ``create`` + ``connect``
        on enter, session close + ``destroy`` on exit — **even when the body raises**,
        so a crashed run never leaks a substrate::

            with provider.session() as env:
                env.click(x=100, y=200)

        ``spec`` and ``connect_kwargs`` are forwarded to :meth:`create` /
        :meth:`connect`. Inherited by every provider from this base class."""
        handle = self.create(spec)
        try:
            env = self.connect(handle, **connect_kwargs)
        except BaseException:
            with contextlib.suppress(Exception):
                self.destroy(handle)
            raise
        try:
            yield env
        finally:
            with contextlib.suppress(Exception):
                env.close()
            with contextlib.suppress(Exception):
                self.destroy(handle)

    def health(self, handle: SandboxHandle) -> SandboxHealth:
        try:
            env = self.connect(handle)
            try:
                rtt_ms = env.ping() * 1000.0
                shot = env.screenshot()
            finally:
                env.close()
        except Exception as exc:
            return SandboxHealth(ok=False, ready=False, detail=str(exc))
        return SandboxHealth(
            ok=True,
            ready=True,
            detail="ready",
            rtt_ms=rtt_ms,
            screenshot_bytes=len(shot["png"]),
        )

    def check_alive(self, handle: SandboxHandle) -> None:
        """Probe whether the sandbox substrate is still alive. Raise
        :class:`~shinken.errors.SandboxDied` (with substrate exit/signal detail when the
        provider can recover it) if it has died; return normally if alive or if the provider
        cannot introspect. Lets a consumer that caught a dropped connection confirm whether
        it was infrastructure death (retry on a fresh sandbox) vs a transient/agent error.
        Default: no-op (a provider that cannot introspect never asserts death)."""
        return None

    def reset(self, handle: SandboxHandle) -> SandboxHandle:
        raise UnsupportedProviderOperation(
            f"{self.capabilities.name} does not support provider-level reset"
        )

    def destroy(self, handle: SandboxHandle) -> None:
        raise NotImplementedError

    def list(self) -> list[SandboxHandle]:
        """Enumerate the live sandboxes this provider (class) manages, as rebuilt
        :class:`SandboxHandle`\\ s. Unsupported by default — providers that stamp
        recoverable identity onto their substrate (labels) override it."""
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support list")

    def gc(self, snapshots: bool = False, force: bool = False) -> GcReport:
        """Garbage-collect leaked provider resources: containers (and, with
        ``snapshots=True``, snapshot images) whose OWNING PROCESS is dead. Live-owner
        resources are skipped (counted in ``GcReport.skipped``) unless ``force=True``.
        Unsupported by default."""
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support gc")

    def snapshot(self, handle: SandboxHandle, name: str | None = None) -> str:
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support snapshot")

    def restore(self, snapshot_id: str) -> SandboxHandle:
        """**The real restore verb**: materialize a NEW live sandbox from a
        snapshot/checkpoint id and return its handle. Restoring never mutates the
        source — restoring while the source sandbox is alive mints a SIBLING.
        Unsupported by default."""
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support restore")

    def fork(self, handle: SandboxHandle) -> SandboxHandle:
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support fork")

    def checkpoint(
        self,
        handle: SandboxHandle,
        *,
        name: str | None = None,
        event_seq: int | None = None,
        agent_state_ref: str | None = None,
    ) -> str:
        """Create a named Shinken restore point binding a substrate snapshot to optional
        agent state — the node in the checkpoint DAG (D5). Returns a checkpoint id.
        ``name`` is an optional human label for the underlying snapshot. Unsupported by
        default."""
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support checkpoint")

    def resume(self, handle_or_checkpoint: SandboxHandle | str) -> SandboxHandle:
        """**Deprecated alias of** :meth:`restore` — kept for back-compat. RESTORE
        semantics, not pause/unpause: it materializes a NEW live sandbox from a
        snapshot/checkpoint id, and calling it while the source sandbox is alive mints
        a SIBLING, not the same sandbox. Unsupported by default."""
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support resume")
