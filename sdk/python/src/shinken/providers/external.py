"""Connect-only provider for externally managed Shinken/OSWorld sandboxes."""

from __future__ import annotations

import time

from .base import ProviderCapabilities, SandboxHandle, SandboxProvider, SandboxSpec


class ExternalProvider(SandboxProvider):
    """Provider for a sandbox that already exists outside this SDK process."""

    capabilities = ProviderCapabilities(
        name="external",
        supports_lifecycle=False,
        supports_gui=True,
        supports_snapshot=False,
        supports_fork=False,
        supports_gpu=False,
        supports_vsock=False,
        supports_egress_policy=False,
        supports_checkpoint=False,
        supports_resume=False,
        reset_strategy="provider_managed",
        transport="tcp_ws",
        snapshot_kind="none",
        tier="external",
        max_sessions=1,
        notes=("Lifecycle is owned by the external provider; Shinken only connects.",),
    )

    def __init__(self, addr: str = "127.0.0.1:8765", token: str | None = None) -> None:
        self.addr = addr
        self.token = token

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        metadata = dict((spec.metadata if spec is not None else {}) or {})
        metadata["lifecycle"] = "external"
        return SandboxHandle(
            provider=self.capabilities.name,
            sandbox_id=metadata.get("sandbox_id", self.addr),
            addr=self.addr,
            token=self.token,
            created_at=time.time(),
            metadata=metadata,
        )

    def reset(self, handle: SandboxHandle) -> SandboxHandle:
        handle.metadata["reset_strategy"] = self.capabilities.reset_strategy
        return handle

    def destroy(self, handle: SandboxHandle) -> None:
        handle.metadata["destroyed"] = True
