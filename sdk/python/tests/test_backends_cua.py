"""Fixture tests for the cua operation-layer backend — the verb→cua mapping, honest
capabilities, and loud fork-native degradation. No cua install, no VM (a protocol-faithful
in-memory cua interface stands in)."""

from __future__ import annotations

import pytest

from shinken.backends import get_backend, list_backends, register_backend
from shinken.backends.cua import CuaBackendProvider, CuaSandbox
from shinken.providers.base import SandboxProvider, UnsupportedProviderOperation


class _FakeCuaNoA11y:
    """The async cua BaseComputerInterface surface WITHOUT get_accessibility_tree."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._clip = ""

    async def get_screen_size(self):
        return {"width": 1024, "height": 768}

    async def screenshot(self):
        self.calls.append(("screenshot",))
        return b"\x89PNGfake"

    async def left_click(self, x, y):
        self.calls.append(("left_click", x, y))

    async def right_click(self, x, y):
        self.calls.append(("right_click", x, y))

    async def double_click(self, x, y):
        self.calls.append(("double_click", x, y))

    async def type_text(self, text):
        self.calls.append(("type_text", text))

    async def press_key(self, key):
        self.calls.append(("press_key", key))

    async def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))

    async def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    async def run_command(self, cmd):
        self.calls.append(("run_command", cmd))
        return {"stdout": "out", "stderr": "", "returncode": 0}

    async def copy_to_clipboard(self):
        return self._clip

    async def set_clipboard(self, text):
        self._clip = text


class FakeCua(_FakeCuaNoA11y):
    """…plus the a11y tree, so structured observe is advertised + served."""

    async def get_accessibility_tree(self):
        return {"role": "window", "name": "W", "children": [{"role": "button", "name": "OK"}]}


def _provider(*, with_a11y: bool = True):
    cls = FakeCua if with_a11y else _FakeCuaNoA11y
    return CuaBackendProvider(interface_factory=lambda spec: cls())


def test_registry_lists_and_builds_cua():
    assert "cua" in list_backends()
    assert isinstance(get_backend("cua", interface_factory=lambda s: FakeCua()), CuaBackendProvider)
    with pytest.raises(KeyError, match="unknown backend"):
        get_backend("nope")


def test_registry_custom_backend():
    sentinel = object()
    register_backend("custom-x", lambda **k: sentinel)
    assert get_backend("custom-x") is sentinel
    assert "custom-x" in list_backends()


def test_provider_capabilities_no_fork():
    cap = _provider().capabilities
    assert cap.name == "cua" and cap.supports_gui and not cap.supports_fork
    assert not cap.supports_snapshot


def test_session_drives_verbs_to_cua():
    prov = _provider()
    with prov.session() as env:
        iface = prov._live[env._handle.sandbox_id][0]
        env.click(x=10, y=20)
        env.click(x=1, y=2, button="right")
        env.click(x=3, y=4, count=2)
        env.type_text("hi")
        env.key("ctrl+s")
        env.key("Return")
        env.scroll(dy=-3)
        assert ("left_click", 10, 20) in iface.calls
        assert ("right_click", 1, 2) in iface.calls
        assert ("double_click", 3, 4) in iface.calls
        assert ("type_text", "hi") in iface.calls
        assert ("hotkey", "ctrl", "s") in iface.calls
        assert ("press_key", "Return") in iface.calls
        assert ("scroll", 0, -3) in iface.calls


def test_screenshot_and_exec_and_clipboard_shapes():
    with _provider().session() as env:
        shot = env.screenshot()
        assert shot["png"].startswith(b"\x89PNG") and shot["w"] == 1024 and shot["h"] == 768
        out = env.exec(["echo", "hi there"])
        assert out["returncode"] == 0 and out["exit_code"] == 0 and out["stdout"] == "out"
        env.clipboard_set("xyz")
        assert env.clipboard_get() == "xyz"
        assert env.screen_size() == {"w": 1024, "h": 768}
        assert env.ping() >= 0.0


def test_structured_observe_when_a11y_present():
    with _provider(with_a11y=True).session() as env:
        assert env.capabilities.structured_observation is True
        assert "observe" in env.capabilities.verbs
        obs = env.observe(structured=True)
        assert obs["available"] and 'window "W"' in obs["tree_text"]
        assert '  button "OK"' in obs["tree_text"]


def test_structured_observe_absent_degrades_honestly():
    with _provider(with_a11y=False).session() as env:
        assert env.capabilities.structured_observation is False
        assert "observe" not in env.capabilities.verbs
        obs = env.observe(structured=True)
        assert obs["available"] is False


class _FakeCuaWrappedTree(FakeCua):
    """The REAL cua shape: get_accessibility_tree returns a command-result wrapper, not
    the bare tree."""

    async def get_accessibility_tree(self):
        return {
            "success": True,
            "tree": {"role": "window", "name": "W", "children": [{"role": "button", "name": "OK"}]},
        }


class _FakeCuaRaisingTree(FakeCua):
    """The REAL cua failure shape: the method exists on every interface and RAISES when the
    server can't serve the tree (hasattr can never select the degrade branch)."""

    async def get_accessibility_tree(self):
        raise RuntimeError("Failed to get accessibility tree: not supported")


def test_structured_observe_unwraps_real_cua_result_wrapper():
    prov = CuaBackendProvider(interface_factory=lambda spec: _FakeCuaWrappedTree())
    with prov.session() as env:
        obs = env.observe(structured=True)
        assert obs["available"] and 'window "W"' in obs["tree_text"]
        assert '  button "OK"' in obs["tree_text"]  # serialized the payload, not the wrapper


def test_structured_observe_real_raise_becomes_typed_unavailable():
    prov = CuaBackendProvider(interface_factory=lambda spec: _FakeCuaRaisingTree())
    with prov.session() as env:
        obs = env.observe(structured=True)
        assert obs["available"] is False and "not supported" in obs["detail"]
        assert obs["error"] == "RuntimeError"


def test_computer_kwargs_defaults_linux_to_docker(monkeypatch):
    from shinken.backends.cua import _computer_kwargs
    from shinken.providers.base import SandboxSpec

    for var in (
        "SHINKEN_CUA_PROVIDER",
        "SHINKEN_CUA_NAME",
        "SHINKEN_CUA_IMAGE",
        "CUA_API_KEY",
        "SHINKEN_CUA_HOST_SERVER",
    ):
        monkeypatch.delenv(var, raising=False)
    # the cua ctor default (lume) can never serve a linux sandbox — docker is our default
    assert _computer_kwargs(None) == {"os_type": "linux", "provider_type": "docker"}
    assert _computer_kwargs(SandboxSpec(os="macos"))["provider_type"] == "lume"
    # metadata wins, env fills, host-server short-circuits
    spec = SandboxSpec(os="linux", metadata={"provider_type": "cloud", "name": "vm-1"})
    kw = _computer_kwargs(spec)
    assert kw["provider_type"] == "cloud" and kw["name"] == "vm-1"
    monkeypatch.setenv("SHINKEN_CUA_IMAGE", "trycua/cua-ubuntu:latest")
    assert _computer_kwargs(None)["image"] == "trycua/cua-ubuntu:latest"
    monkeypatch.setenv("SHINKEN_CUA_HOST_SERVER", "1")
    assert _computer_kwargs(None) == {"os_type": "linux", "use_host_computer_server": True}


def test_fork_family_degrades_loudly():
    with _provider().session() as env:
        with pytest.raises(UnsupportedProviderOperation, match="checkpoint"):
            env.checkpoint("golden")
        with pytest.raises(UnsupportedProviderOperation):
            env.spawn()


def test_click_requires_pixels():
    with _provider().session() as env:
        with pytest.raises(ValueError, match="pixel x,y"):
            env.click()


def test_is_a_sandbox_provider():
    assert isinstance(_provider(), SandboxProvider)
    # the duck-typed sandbox is NOT a shinkend Sandbox but quacks like one
    with _provider().session() as env:
        assert isinstance(env, CuaSandbox)
        assert env.platform == "linux"
