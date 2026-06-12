"""Fixture tests for the MCP computer-use backend (codex-style 9-tool server) — verb→MCP-tool
mapping, structured observe with element_ref, honest caps, loud fork degrade. No server."""

from __future__ import annotations

import base64

import pytest

from shinken.backends import get_backend, list_backends
from shinken.backends.mcp_computer import McpComputerBackend, McpComputerSandbox
from shinken.providers.base import SandboxSpec, UnsupportedProviderOperation

_PNG = base64.b64encode(b"\x89PNGfake").decode()
_TREE = '‹1› window "Main"\n‹7› text "Vendor"\n‹9› button "OK"'


def fake_mcp():
    """A faithful in-memory MCP computer-use server: records tools/call, returns codex-shape
    content blocks (text AX tree + image screenshot)."""
    calls: list[tuple] = []

    def mcp_call(tool: str, args: dict) -> dict:
        calls.append((tool, args))
        if tool == "get_app_state":
            return {
                "content": [
                    {"type": "text", "text": _TREE},
                    {"type": "image", "data": _PNG, "mimeType": "image/png"},
                ]
            }
        if tool == "list_apps":
            return {"content": [{"type": "text", "text": "TextEdit\nSafari"}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    mcp_call.calls = calls  # type: ignore[attr-defined]
    return mcp_call


def _provider(**kw):
    return McpComputerBackend(transport_factory=lambda spec: fake_mcp(), app="TextEdit", **kw)


def _session(prov):
    return prov.session(SandboxSpec(metadata={"app": "TextEdit"}))


def test_registry_has_mcp_computer():
    assert "mcp-computer" in list_backends()
    assert isinstance(
        get_backend("mcp-computer", transport_factory=lambda s: fake_mcp(), app="X"),
        McpComputerBackend,
    )


def test_caps_structured_and_no_fork():
    cap = _provider().capabilities
    assert cap.name == "mcp-computer" and not cap.supports_fork
    with _session(_provider()) as env:
        assert env.capabilities.structured_observation is True
        assert "element_ref" in env.capabilities.targets
        assert "exec" not in env.capabilities.verbs  # non-invasive AX: no shell


def test_app_scope_required():
    prov = McpComputerBackend(transport_factory=lambda s: fake_mcp())  # no app
    with pytest.raises(ValueError, match="app-scoped"):
        prov.create(SandboxSpec())


def test_structured_observe_parses_elements():
    with _session(_provider()) as env:
        obs = env.observe(structured=True)
        assert obs["available"] and 'button "OK"' in obs["tree_text"]
        refs = {e["ref"]: e for e in obs["elements"]}
        assert refs["e9"]["role"] == "button" and refs["e9"]["name"] == "OK"
        assert refs["e7"]["element_index"] == "7"


def test_click_type_key_scroll_map_to_tools():
    prov = _provider()
    with _session(prov) as env:
        mcp = prov._live[env._handle.sandbox_id][0]
        env.observe(structured=True)
        env.click(ref="e9")
        env.click(x=100, y=200, button="right")
        env.type_text("hi")
        env.key("super+c")
        env.scroll(dy=2)
        tools = [c[0] for c in mcp.calls]
        assert tools.count("get_app_state") >= 1
        click_args = [a for t, a in mcp.calls if t == "click"]
        assert click_args[0]["element_index"] == "9" and click_args[0]["app"] == "TextEdit"
        assert click_args[1]["x"] == 100 and click_args[1]["mouse_button"] == "right"
        assert ("type_text", {"app": "TextEdit", "text": "hi"}) in mcp.calls
        assert ("press_key", {"app": "TextEdit", "key": "super+c"}) in mcp.calls
        assert any(t == "scroll" and a["direction"] == "down" for t, a in mcp.calls)


def test_act_on_and_invoke_and_set_value():
    prov = _provider()
    with _session(prov) as env:
        mcp = prov._live[env._handle.sandbox_id][0]
        env.observe(structured=True)
        env.act_on("e9", "click")
        env.invoke_action("e9", "AXPress")
        env.set_value("e7", "ACME")
        assert any(
            t == "perform_secondary_action" and a["action"] == "AXPress" for t, a in mcp.calls
        )
        assert any(t == "set_value" and a["value"] == "ACME" for t, a in mcp.calls)


def test_unknown_ref_raises():
    with _session(_provider()) as env:
        with pytest.raises(KeyError, match="unknown element_ref"):
            env.click(ref="e999")


def test_screenshot_returns_png():
    with _session(_provider()) as env:
        shot = env.screenshot()
        assert shot["png"] == b"\x89PNGfake" and shot["format"] == "png"


def test_fork_family_degrades_loudly():
    with _session(_provider()) as env:
        with pytest.raises(UnsupportedProviderOperation):
            env.checkpoint("golden")


def test_mcp_error_surfaces():
    def erroring(spec):
        def call(tool, args):
            return {"isError": True, "content": [{"type": "text", "text": "boom"}]}

        return call

    prov = McpComputerBackend(transport_factory=erroring, app="X")
    with prov.session(SandboxSpec(metadata={"app": "X"})) as env:
        with pytest.raises(RuntimeError, match="boom"):
            env.observe(structured=True)


def test_is_duck_typed_sandbox():
    with _session(_provider()) as env:
        assert isinstance(env, McpComputerSandbox) and env.platform == "linux"
