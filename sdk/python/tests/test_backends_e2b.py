"""Fixture tests for the e2b-desktop operation-layer backend — the verb→e2b mapping
(xdotool-style press/scroll/drag), the shell exec passthrough (incl. e2b's raise-on-nonzero
and timeout semantics), honest pixel-only capabilities, and loud fork-native degradation.
No e2b install, no E2B_API_KEY, no cloud (a protocol-faithful in-memory e2b-desktop Sandbox
stands in — same signatures, same raise behavior)."""

from __future__ import annotations

import pytest

from shinken.backends import dispatch_action, get_backend, list_backends
from shinken.backends.e2b_desktop import E2bDesktopBackend, E2bDesktopSandbox
from shinken.providers.base import SandboxProvider, UnsupportedProviderOperation


class _FakeCommandExit(Exception):
    """Shape of e2b's CommandExitException: carries the result of a non-zero exit."""

    def __init__(self, stdout="", stderr="boom", exit_code=1):
        super().__init__(stderr)
        self.stdout, self.stderr, self.exit_code = stdout, stderr, exit_code


class _FakeTimeout(TimeoutError):
    """Shape of e2b's TimeoutException (class name carries 'Timeout')."""


class _FakeCommands:
    """The real e2b Commands.run shape: accepts timeout/background, RAISES on non-zero exit."""

    def __init__(self, calls: list) -> None:
        self._calls = calls

    def run(self, cmd, background=None, timeout=60, **_kw):
        self._calls.append(("run", cmd, timeout))
        if "exit 7" in cmd:
            raise _FakeCommandExit(stdout="partial", stderr="boom", exit_code=7)
        if "sleep forever" in cmd:
            raise _FakeTimeout(cmd)
        return type("R", (), {"stdout": "out", "stderr": "", "exit_code": 0})()


class FakeE2bDesktop:
    """The e2b-desktop Sandbox surface, in-memory (xdotool-backed pixel driver + shell).
    Mirrors the real SDK's quirks: click(x, y) skips the mouse move unless BOTH coords are
    truthy (`if x and y:`), screenshot's kwarg is named `format`."""

    sandbox_id = "e2b-fake-001"

    def __init__(self) -> None:
        self.calls: list = []
        self.commands = _FakeCommands(self.calls)
        self.killed = False

    def get_screen_size(self):
        return (1280, 800)

    def screenshot(self, format="bytes"):  # noqa: A002 - the real SDK's param name
        self.calls.append(("screenshot", format))
        return bytearray(b"\x89PNGe2b")

    def move_mouse(self, x, y):
        self.calls.append(("move_mouse", x, y))

    def left_click(self, x=None, y=None):
        if x and y:
            self.calls.append(("move_mouse", x, y))
        self.calls.append(("left_click",))

    def right_click(self, x=None, y=None):
        if x and y:
            self.calls.append(("move_mouse", x, y))
        self.calls.append(("right_click",))

    def double_click(self, x=None, y=None):
        if x and y:
            self.calls.append(("move_mouse", x, y))
        self.calls.append(("double_click",))

    def middle_click(self, x=None, y=None):
        if x and y:
            self.calls.append(("move_mouse", x, y))
        self.calls.append(("middle_click",))

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
        self.calls.append(("launch", application, uri))

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
        # every implemented verb is advertised — incl. drag and (pixel) observe,
        # so they are reachable through dispatch_action/RoutedSession
        assert set(cap.verbs) == {
            "click",
            "drag",
            "type_text",
            "key",
            "scroll",
            "screenshot",
            "observe",
            "exec",
            "launch_app",
        }


def test_clicks_move_explicitly_then_click():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.click(x=10, y=20)
        env.click(x=1, y=2, button="right")
        env.click(x=3, y=4, button="middle")
        env.click(x=5, y=6, count=2)
        assert ("move_mouse", 10, 20) in sb.calls and ("left_click",) in sb.calls
        assert ("move_mouse", 1, 2) in sb.calls and ("right_click",) in sb.calls
        assert ("move_mouse", 3, 4) in sb.calls and ("middle_click",) in sb.calls
        assert ("move_mouse", 5, 6) in sb.calls and ("double_click",) in sb.calls


def test_click_at_zero_coordinate_still_moves():
    # the real SDK's own click guards its move with `if x and y:` — the adapter must not
    # inherit that truthiness bug (a click at x==0 landing wherever the cursor last was)
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.click(x=0, y=300)
        assert ("move_mouse", 0, 300) in sb.calls and ("left_click",) in sb.calls


def test_right_double_click_never_uses_left_double():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.click(x=9, y=9, button="right", count=2)
        assert sb.calls.count(("right_click",)) == 2
        assert ("double_click",) not in sb.calls  # SDK double_click is xdotool button 1


def test_type_key_scroll_drag():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.type_text("hi there")
        env.key("ctrl+s")  # combo -> list
        env.key("F5")  # single -> str
        env.scroll(dy=-3)  # negative -> up
        env.scroll(dy=2)  # positive -> down
        env.drag(target={"x": 1, "y": 2}, to={"x": 3, "y": 4})
        assert ("write", "hi there") in sb.calls
        assert ("press", ["ctrl", "s"]) in sb.calls
        assert ("press", "F5") in sb.calls
        assert ("scroll", "up", 3) in sb.calls
        assert ("scroll", "down", 2) in sb.calls
        assert ("drag", (1, 2), (3, 4)) in sb.calls


def test_key_normalizes_shinken_spellings():
    # spellings e2b's map_key/xdotool would silently no-op get mapped before press()
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.key("pageup")
        env.key("Return")
        env.key("ctrl+pgdn")
        assert ("press", "page_up") in sb.calls
        assert ("press", "enter") in sb.calls
        assert ("press", ["ctrl", "page_down"]) in sb.calls


def test_horizontal_scroll_degrades_loudly():
    with _provider().session() as env:
        with pytest.raises(UnsupportedProviderOperation, match="vertical-only"):
            env.scroll(dx=3)  # e2b cannot scroll horizontally — never actuate 'down' instead


def test_exec_uses_real_shell():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        out = env.exec(["echo", "hi there"])
        assert out["exit_code"] == 0 and out["returncode"] == 0
        assert out["stdout"] == "out" and out["timed_out"] is False
        out2 = env.exec(shell="ls -la /tmp")
        assert out2["exit_code"] == 0
        # argv leg shlex-joins; shell leg passes through unsplit; None timeout -> e2b's
        # documented "no limit" (0), not its silent 60 s default
        assert ("run", "echo 'hi there'", 0) in sb.calls
        assert ("run", "ls -la /tmp", 0) in sb.calls


def test_exec_nonzero_exit_returns_typed_result():
    # the real e2b RAISES CommandExitException on non-zero exit; the Shinken exec
    # contract returns a typed result instead of leaking the third-party exception
    with _provider().session() as env:
        out = env.exec(shell="exit 7")
        assert out["exit_code"] == 7 and out["returncode"] == 7
        assert out["stdout"] == "partial" and out["stderr"] == "boom"
        assert out["timed_out"] is False


def test_exec_timeout_forwarded_and_typed():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        out = env.exec(shell="sleep forever", timeout=5)
        assert out["timed_out"] is True and out["exit_code"] is None
        assert ("run", "sleep forever", 5) in sb.calls  # the timeout reaches commands.run


def test_screenshot_and_screen_size():
    with _provider().session() as env:
        shot = env.screenshot()
        assert shot["png"].startswith(b"\x89PNG") and shot["w"] == 1280 and shot["h"] == 800
        assert shot["scope"] == "screen"
        assert env.screen_size() == {"w": 1280, "h": 800}
        assert isinstance(env.ping(), float)


def test_screenshot_unsupported_scope_degrades_loudly():
    with _provider().session() as env:
        with pytest.raises(UnsupportedProviderOperation, match="scope"):
            env.screenshot(scope="window:42")  # full-screen scrot only — never mislabel


def test_observe_is_pixel_only():
    with _provider().session() as env:
        # pixel observe works (returns a screenshot-backed observation)
        obs = env.observe(structured=False)
        assert obs["png"].startswith(b"\x89PNG")
        # structured observe degrades honestly — no faked tree
        s = env.observe(structured=True)
        assert s["available"] is False and s["tree"] == "none"


def test_drag_and_observe_dispatchable():
    # implemented AND advertised -> reachable through dispatch_action/RoutedSession
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        dispatch_action(
            env,
            {
                "verb": "drag",
                "target": {"kind": "point_px", "x": 1, "y": 2},
                "to": {"kind": "point_px", "x": 3, "y": 4},
            },
        )
        assert ("drag", (1, 2), (3, 4)) in sb.calls
        obs = dispatch_action(env, {"verb": "observe", "structured": False})
        assert obs["png"].startswith(b"\x89PNG")


def test_launch_app_honors_args():
    prov = _provider()
    with prov.session() as env:
        sb = prov._live[env._handle.sandbox_id]
        env.launch_app("xterm")
        env.launch_app("libreoffice", args=["/tmp/doc.odt"])  # gtk-launch's one URI slot
        env.launch_app("myapp", args=["--flag", "value"])  # >1 arg -> via the shell
        assert ("launch", "xterm", None) in sb.calls
        assert ("launch", "libreoffice", "/tmp/doc.odt") in sb.calls
        assert ("run", "myapp --flag value", 0) in sb.calls


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


def test_closed_sandbox_guards_every_verb():
    prov = _provider()
    env = prov.connect(prov.create())
    env.close()
    for call in (
        lambda: env.observe(structured=True),
        lambda: env.screenshot(),
        lambda: env.click(x=1, y=2),
        lambda: env.exec(["true"]),
    ):
        with pytest.raises(RuntimeError, match="closed"):
            call()


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
