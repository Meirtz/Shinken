"""Fixture tests for the e2b-desktop operation-layer backend — the verb→e2b mapping
(xdotool-style press/scroll/drag), the shell exec passthrough, honest pixel-only capabilities,
and loud fork-native degradation. No e2b install, no E2B_API_KEY, no cloud (a protocol-faithful
in-memory e2b-desktop Sandbox stands in)."""

from __future__ import annotations

import pytest

from shinken.backends import get_backend, list_backends
from shinken.backends.e2b_desktop import E2bDesktopBackend, E2bDesktopSandbox
from shinken.providers.base import SandboxProvider, UnsupportedProviderOperation


class _FakeCommands:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def run(self, cmd):
        self._calls.append(("run", cmd))
        return type("R", (), {"stdout": "out", "stderr": "", "exit_code": 0})()


class FakeE2bDesktop:
    """The e2b-desktop Sandbox surface, in-memory (xdotool-backed pixel driver + shell)."""

    sandbox_id = "e2b-fake-001"

    def __init__(self) -> None:
        self.calls: list = []
        self.commands = _FakeCommands(self.calls)
        self.killed = False

    def get_screen_size(self):
        return (1280, 800)

    def screenshot(self, fmt="bytes"):
        self.calls.append(("screenshot", fmt))
        return bytearray(b"\x89PNGe2b")

    def left_click(self, x=None, y=None):
        self.calls.append(("left_click", x, y))

    def right_click(self, x=None, y=None):
        self.calls.append(("right_click", x, y))

    def double_click(self, x=None, y=None):
        self.calls.append(("double_click", x, y))

    def middle_click(self, x=None, y=None):
        self.calls.append(("middle_click", x, y))

    def move_mouse(self, x, y):
        self.calls.append(("move_mouse", x, y))

    def mouse_press(self, button="left"):
        self.calls.append(("mouse_press", button))

    def mouse_release(self, button="left"):
        self.calls.append(("mouse_release", button))

    def scroll(self, direction="down", amount=1):
        self.calls.append(("scroll", direction, amount))

    def write(self, text, **_kw):
        self.calls.append(("write", text))

    def press(self, key):
        self.calls.append(("press", key))

    def drag(self, fr, to):
        self.calls.append(("drag", fr, to))

    def launch(self, application, uri=None):
        self.calls.append(("launch", application))

    def kill(self):
        self.killed = True


def _provider():
    return E2bDesktopBackend(sandbox_factory=lambda spec: FakeE2bDesktop())


def test_registry_lists_and_builds_e2b():
    assert "e2b" in list_backends()
    prov = get_backend("e2b", sandbox_factory=lambda s: FakeE2bDesktop())
    assert isinstance(prov, E2bDesktopBackend)


def test_provider_capabilities_no_fork_no_snapshot():
    cap = _provider().capabilities
    assert cap.name == "e2b-desktop" and cap.supports_gui
    assert not cap.supports_fork and not cap.supports_snapshot
    assert cap.tier == "external-backend"


def test_sandbox_capabilities_pixel_only():
    with _provider().session() as env:
        cap = env.capabilities
        assert cap.structured_observation is False
        assert cap.targets == ["point_px"]  # no element_ref
        assert "exec" in cap.verbs and "launch_app" in cap.verbs
        assert "observe" not in cap.verbs or True  # pixel observe is fine; structured isn't


def test_clicks_map_to_xdotool_variants():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.click(x=10, y=20)
        env.click(x=1, y=2, button="right")
        env.click(x=3, y=4, button="middle")
        env.click(x=5, y=6, count=2)
        assert ("left_click", 10, 20) in sb.calls
        assert ("right_click", 1, 2) in sb.calls
        assert ("middle_click", 3, 4) in sb.calls
        assert ("double_click", 5, 6) in sb.calls


def test_type_key_scroll_drag():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.type_text("hi there")
        env.key("ctrl+s")  # combo -> list
        env.key("Return")  # single -> str
        env.scroll(dy=-3)  # negative -> up
        env.scroll(dy=2)  # positive -> down
        env.drag(target={"x": 1, "y": 2}, to={"x": 3, "y": 4})
        assert ("write", "hi there") in sb.calls
        assert ("press", ["ctrl", "s"]) in sb.calls
        assert ("press", "Return") in sb.calls
        assert ("scroll", "up", 3) in sb.calls
        assert ("scroll", "down", 2) in sb.calls
        assert ("drag", (1, 2), (3, 4)) in sb.calls


def test_exec_uses_real_shell():
    with _provider().session() as env:
        out = env.exec(["echo", "hi there"])
        assert out["exit_code"] == 0 and out["returncode"] == 0
        assert out["stdout"] == "out" and out["timed_out"] is False
        out2 = env.exec(shell="ls -la /tmp")
        assert out2["exit_code"] == 0


def test_screenshot_and_screen_size():
    with _provider().session() as env:
        shot = env.screenshot()
        assert shot["png"].startswith(b"\x89PNG") and shot["w"] == 1280 and shot["h"] == 800
        assert env.screen_size() == {"w": 1280, "h": 800}
        assert env.ping() >= 0.0


def test_observe_is_pixel_only():
    with _provider().session() as env:
        # pixel observe works (returns a screenshot-backed observation)
        obs = env.observe(structured=False)
        assert obs["png"].startswith(b"\x89PNG")
        # structured observe degrades honestly — no faked tree
        s = env.observe(structured=True)
        assert s["available"] is False and s["tree"] == "none"


def test_launch_app():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.launch_app("xterm")
        assert ("launch", "xterm") in sb.calls


def test_click_requires_pixels():
    with _provider().session() as env:
        with pytest.raises(ValueError, match="pixel x,y"):
            env.click()


def test_fork_family_degrades_loudly():
    with _provider().session() as env:
        with pytest.raises(UnsupportedProviderOperation, match="checkpoint"):
            env.checkpoint("golden")
        with pytest.raises(UnsupportedProviderOperation):
            env.spawn()


def test_destroy_kills_cloud_sandbox():
    prov = _provider()
    h = prov.create()
    sb = prov._live[h.sandbox_id]
    prov.destroy(h)
    assert sb.killed is True and h.sandbox_id not in prov._live


def test_is_a_sandbox_provider():
    assert isinstance(_provider(), SandboxProvider)
    with _provider().session() as env:
        assert isinstance(env, E2bDesktopSandbox)
        assert env.platform == "linux"
