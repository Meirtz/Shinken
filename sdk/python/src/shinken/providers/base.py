"""Sandbox provider contracts.

Providers own sandbox lifecycle. The ACI client owns the session once a provider
returns an address and token for a running ``shinkend``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shinken.client import Sandbox, connect


class ProviderError(RuntimeError):
    """Raised when a sandbox provider cannot complete a lifecycle operation."""


class UnsupportedProviderOperation(ProviderError):
    """Raised when a provider does not support snapshot/fork-style semantics."""


@dataclass(frozen=True)
class ProviderCapabilities:
    """Honest capabilities advertised by a sandbox provider."""

    name: str
    supports_lifecycle: bool
    supports_gui: bool
    supports_snapshot: bool = False
    supports_fork: bool = False
    supports_gpu: bool = False
    supports_vsock: bool = False
    supports_egress_policy: bool = False
    reset_strategy: str = "recreate"
    max_sessions: int | None = None
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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxHandle:
    """A running or externally managed sandbox endpoint."""

    provider: str
    sandbox_id: str
    addr: str
    token: str | None = None
    created_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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

    def connect(self, handle: SandboxHandle, *, record: bool = False) -> Sandbox:
        return connect(handle.addr, record=record, token=handle.token)

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

    def reset(self, handle: SandboxHandle) -> SandboxHandle:
        raise UnsupportedProviderOperation(
            f"{self.capabilities.name} does not support provider-level reset"
        )

    def destroy(self, handle: SandboxHandle) -> None:
        raise NotImplementedError

    def snapshot(self, handle: SandboxHandle, name: str | None = None) -> str:
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support snapshot")

    def restore(self, snapshot_id: str) -> SandboxHandle:
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support restore")

    def fork(self, handle: SandboxHandle) -> SandboxHandle:
        raise UnsupportedProviderOperation(f"{self.capabilities.name} does not support fork")
