"""Provider runtime-state descriptor contract tests (#164).

Every provider advertises a `ProviderCapabilities` descriptor declaring which runtime-state
operations (snapshot / restore / fork / reset) it supports. These tests pin the contract
that the descriptor is **complete and honest**: a provider that declares an operation
unsupported MUST raise `UnsupportedProviderOperation` rather than silently no-op or pretend,
so the Control Plane can route on the descriptor. No live runtime is needed — the
unsupported-op methods raise before touching any backend."""

from __future__ import annotations

import pytest

from shinken.providers import CriuDockerProvider, DockerLocalProvider, ExternalProvider
from shinken.providers.base import (
    ProviderCapabilities,
    SandboxHandle,
    SandboxProvider,
    UnsupportedProviderOperation,
)

# All concrete providers, constructed without connecting to any runtime.
PROVIDERS: list[SandboxProvider] = [
    DockerLocalProvider(),
    CriuDockerProvider(),
    ExternalProvider(),
]
_KNOWN_RESET_STRATEGIES = {"recreate", "provider_managed", "snapshot_restore", "fork_from_snapshot"}
_KNOWN_SNAPSHOT_KINDS = {"none", "disk", "memory", "process", "provider_managed"}


def _dummy_handle(provider: SandboxProvider) -> SandboxHandle:
    return SandboxHandle(
        provider=provider.capabilities.name, sandbox_id="dummy", addr="127.0.0.1:1"
    )


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.capabilities.name)
def test_descriptor_is_complete(provider: SandboxProvider) -> None:
    caps = provider.capabilities
    assert isinstance(caps, ProviderCapabilities)
    # every runtime-state field is present and typed as declared
    for field in (
        "supports_lifecycle",
        "supports_gui",
        "supports_snapshot",
        "supports_fork",
        "supports_gpu",
        "supports_vsock",
        "supports_egress_policy",
        "supports_checkpoint",
        "supports_resume",
        "requires_privileged",
    ):
        assert isinstance(getattr(caps, field), bool), f"{caps.name}.{field} must be bool"
    assert caps.reset_strategy in _KNOWN_RESET_STRATEGIES, (
        f"{caps.name} reset_strategy={caps.reset_strategy!r}"
    )
    assert caps.snapshot_kind in _KNOWN_SNAPSHOT_KINDS, (
        f"{caps.name} snapshot_kind={caps.snapshot_kind!r}"
    )
    # structured routing fields are present non-empty strings (machine-routable, #206)
    for sfield in ("isolation", "transport", "display", "tier"):
        value = getattr(caps, sfield)
        assert isinstance(value, str) and value, f"{caps.name}.{sfield}"


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.capabilities.name)
def test_unsupported_runtime_state_ops_raise(provider: SandboxProvider) -> None:
    caps = provider.capabilities
    handle = _dummy_handle(provider)
    # The descriptor is the source of truth: declared-unsupported ops must fail loudly.
    if not caps.supports_snapshot:
        with pytest.raises(UnsupportedProviderOperation):
            provider.snapshot(handle)
        with pytest.raises(UnsupportedProviderOperation):
            provider.restore("snap-id")  # restore depends on snapshot support
    if not caps.supports_fork:
        with pytest.raises(UnsupportedProviderOperation):
            provider.fork(handle)
    if not caps.supports_checkpoint:
        with pytest.raises(UnsupportedProviderOperation):
            provider.checkpoint(handle)
    if not caps.supports_resume:
        with pytest.raises(UnsupportedProviderOperation):
            provider.resume(handle)


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.capabilities.name)
def test_claimed_support_is_actually_wired(provider: SandboxProvider) -> None:
    # A provider that CLAIMS a runtime-state op must override the base method (not the
    # raising stub) — guard against flipping a flag without implementing it (#206).
    caps = provider.capabilities
    claims = {
        "snapshot": caps.supports_snapshot,
        "fork": caps.supports_fork,
        "checkpoint": caps.supports_checkpoint,
        "resume": caps.supports_resume,
    }
    for op, claimed in claims.items():
        overridden = getattr(type(provider), op) is not getattr(SandboxProvider, op)
        if claimed:
            assert overridden, f"{caps.name} advertises {op} but does not override the stub"


def test_external_provider_is_not_a_lifecycle_owner() -> None:
    # ExternalProvider attaches to an already-running runtime; it must not claim to own
    # lifecycle, and its reset is provider-managed (not a recreate it performs).
    caps = ExternalProvider().capabilities
    assert caps.supports_lifecycle is False
    assert caps.reset_strategy == "provider_managed"


def test_docker_provider_owns_lifecycle_with_recreate_reset() -> None:
    caps = DockerLocalProvider().capabilities
    assert caps.supports_lifecycle is True
    assert caps.reset_strategy == "recreate"
