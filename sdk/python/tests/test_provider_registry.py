"""Provider registry: name resolution + out-of-tree plugin loading.

The repo seeds only OFFICIAL providers; a private provider loads from an out-of-tree module
named in $SHINKEN_PROVIDER_PLUGINS, so no private provider name appears in a tracked file."""

from __future__ import annotations

import textwrap

import pytest

from shinken import providers
from shinken.providers import (
    DockerLocalProvider,
    ExternalProvider,
    ProviderError,
    SandboxProvider,
)


@pytest.fixture
def clean_registry():
    """Snapshot/restore the global registry so a test's registrations don't leak."""
    snap = dict(providers._REGISTRY)
    loaded = providers._PLUGINS_LOADED
    try:
        yield
    finally:
        providers._REGISTRY.clear()
        providers._REGISTRY.update(snap)
        providers._PLUGINS_LOADED = loaded


def test_only_official_providers_registered_in_tree():
    names = set(providers.available())
    assert {"docker", "docker-criu", "external"} <= names
    # No private/non-official provider names ship in-tree.
    assert names == {"docker", "docker-criu", "external"}


def test_get_resolves_official_providers():
    assert isinstance(providers.get("docker"), DockerLocalProvider)
    ext = providers.get("external", addr="127.0.0.1:8765")
    assert isinstance(ext, ExternalProvider) and isinstance(ext, SandboxProvider)


def test_unknown_provider_raises_listing_available():
    with pytest.raises(ProviderError) as ei:
        providers.get("definitely-not-registered")
    msg = str(ei.value)
    assert "unknown provider" in msg and "docker" in msg and "external" in msg


def test_register_and_resolve_custom(clean_registry):
    class FakeProvider(ExternalProvider):
        pass

    providers.register("fake", FakeProvider)
    assert "fake" in providers.available()
    assert isinstance(providers.get("fake", addr="x"), FakeProvider)


def test_out_of_tree_plugin_loads_via_env(clean_registry, tmp_path, monkeypatch):
    # A private provider shipped out-of-tree = a module that registers itself on import.
    (tmp_path / "shk_test_plugin.py").write_text(
        textwrap.dedent(
            """
            from shinken.providers import register, ExternalProvider
            class PluginProvider(ExternalProvider):
                pass
            register("plugin", PluginProvider)
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("SHINKEN_PROVIDER_PLUGINS", "shk_test_plugin")
    providers._PLUGINS_LOADED = False  # allow a re-scan for the test

    assert "plugin" in providers.available()  # triggers load_plugins -> imports the module
    assert isinstance(providers.get("plugin", addr="x"), SandboxProvider)


def test_empty_plugin_env_is_a_noop(clean_registry, monkeypatch):
    monkeypatch.delenv("SHINKEN_PROVIDER_PLUGINS", raising=False)
    providers._PLUGINS_LOADED = False
    providers.load_plugins()
    assert set(providers.available()) == {"docker", "docker-criu", "external"}
