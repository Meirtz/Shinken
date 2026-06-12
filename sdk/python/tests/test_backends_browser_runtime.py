"""Fixture tests for the Browser Runtime (BU) backend — the three tab surfaces (pixel /
semantic-node element_ref / locator-eval) over a faithful in-memory CDP client. No Chrome."""

from __future__ import annotations

import base64

import pytest

from shinken.backends import get_backend, list_backends
from shinken.backends.browser_runtime import BrowserRuntimeBackend, BrowserRuntimeSandbox
from shinken.providers.base import UnsupportedProviderOperation

_AX = [
    {
        "nodeId": "1",
        "role": {"value": "RootWebArea"},
        "name": {"value": "Demo"},
        "childIds": ["2", "3"],
        "backendDOMNodeId": 1,
    },
    {
        "nodeId": "2",
        "role": {"value": "textbox"},
        "name": {"value": "Vendor"},
        "childIds": [],
        "backendDOMNodeId": 2,
    },
    {
        "nodeId": "3",
        "role": {"value": "button"},
        "name": {"value": "Save"},
        "childIds": [],
        "backendDOMNodeId": 3,
    },
]
_SNAP = {
    "documents": [
        {
            "nodes": {"backendNodeId": [1, 2, 3]},
            "layout": {
                "nodeIndex": [0, 1, 2],
                "bounds": [[0, 0, 800, 600], [40, 80, 200, 30], [40, 130, 80, 30]],
            },
        }
    ]
}


class FakeBrowserClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._value = ""
        self._clip = ""

    def execute_cdp(self, tab_id, method, params):
        self.calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\x89PNGpng").decode()}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": _AX}
        if method == "DOMSnapshot.captureSnapshot":
            return _SNAP
        if method == "Runtime.evaluate":
            return {"result": {"value": self._value}}
        if method == "Input.insertText":
            self._value += params.get("text", "")
        return {}

    def read_clipboard_text(self, tab_id):
        return self._clip

    def write_clipboard_text(self, tab_id, text):
        self._clip = text


def _provider():
    return BrowserRuntimeBackend(client_factory=lambda spec: (FakeBrowserClient(), 1))


def test_registry_has_browser_runtime():
    assert "browser-runtime" in list_backends()
    assert isinstance(
        get_backend("browser-runtime", client_factory=lambda s: (FakeBrowserClient(), 1)),
        BrowserRuntimeBackend,
    )


def test_caps_browser_facade_no_fork_no_exec():
    cap = _provider().capabilities
    assert cap.name == "browser-runtime" and not cap.supports_fork
    with _provider().session() as env:
        assert env.capabilities.structured_observation is True
        assert "navigate" in env.capabilities.verbs and "eval" in env.capabilities.verbs
        assert "exec" not in env.capabilities.verbs  # a browser has no shell


def test_structured_observe_via_cdp_ax_tree():
    with _provider().session() as env:
        obs = env.observe(structured=True)
        assert obs["available"]
        roles = {e["role"]: e for e in obs["elements"]}
        assert "textbox" in roles and "button" in roles
        # bbox came from the DOMSnapshot bounds → element_ref clicks resolve to a center
        assert tuple(roles["button"]["bbox"]) == (40, 130, 80, 30)


def test_click_by_element_ref_dispatches_mouse_at_center():
    prov = _provider()
    with prov.session() as env:
        client = prov._live[env._handle.sandbox_id][0]
        obs = env.observe(structured=True)
        btn = next(e["ref"] for e in obs["elements"] if e["role"] == "button")
        env.click(ref=btn)
        # center of (40,130,80,30) = (80,145)
        press = [
            a
            for m, a in client.calls
            if m == "Input.dispatchMouseEvent" and a["type"] == "mousePressed"
        ]
        assert press and press[0]["x"] == 80 and press[0]["y"] == 145


def test_pixel_click_and_type_and_navigate_and_eval():
    prov = _provider()
    with prov.session() as env:
        client = prov._live[env._handle.sandbox_id][0]
        env.navigate("https://example.com")
        env.click(x=10, y=20)
        env.type_text("hi")
        methods = [m for m, _ in client.calls]
        assert "Page.navigate" in methods and "Input.insertText" in methods
        assert any(m == "Input.dispatchMouseEvent" and a["x"] == 10 for m, a in client.calls)
        assert env.eval("x") == "hi"  # insertText accumulated into the fake's value


def test_screenshot_and_clipboard():
    with _provider().session() as env:
        assert env.screenshot()["png"] == b"\x89PNGpng"
        env.clipboard_set("xyz")
        assert env.clipboard_get() == "xyz"


def test_unknown_ref_raises():
    with _provider().session() as env:
        with pytest.raises(KeyError, match="unknown element_ref"):
            env.click(ref="e999")


def test_click_needs_ref_or_pixels():
    with _provider().session() as env:
        with pytest.raises(ValueError, match="ref= .* or x,y"):
            env.click()


def test_fork_family_degrades_loudly():
    with _provider().session() as env:
        with pytest.raises(UnsupportedProviderOperation):
            env.checkpoint("golden")


def test_is_duck_typed_browser_sandbox():
    with _provider().session() as env:
        assert isinstance(env, BrowserRuntimeSandbox) and env.platform == "browser"
