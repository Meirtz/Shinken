"""Run ONE agent loop across a desktop (CU) backend and a browser (BU) backend — the
CU↔BU composition a host like Codex.app does, in Shinken. A RoutedSession holds both
surfaces, routes each ACI action to the right one, and tags every action + observation with
`source` provenance. It quacks like a Sandbox, so shinken.operator.drive runs over it.

Scripted, no installs: the CU surface is the codex-style MCP computer-use backend and the BU
surface is the browser-runtime backend, each over an in-memory faithful peer.

Run:  python examples/backends_routed_cu_bu.py
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.backends import RoutedSession, get_backend, route_for_target  # noqa: E402
from shinken.backends.mcp_computer import McpComputerBackend  # noqa: E402
from shinken.operator import ScriptedAgent, drive  # noqa: E402
from shinken.providers.base import SandboxSpec  # noqa: E402


def fake_mcp(_spec):
    def call(tool, args):
        if tool == "get_app_state":
            return {
                "content": [
                    {"type": "text", "text": '‹1› window "Notes"\n‹4› button "New"'},
                    {"type": "image", "data": base64.b64encode(b"\x89PNG").decode()},
                ]
            }
        return {"content": [{"type": "text", "text": "ok"}]}

    return call


def fake_browser(_spec):
    state = {"url": "about:blank"}

    def execute_cdp(tab, method, params):
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\x89PNGbu").decode()}
        if method == "Accessibility.getFullAXTree":
            return {
                "nodes": [
                    {
                        "nodeId": "1",
                        "role": {"value": "link"},
                        "name": {"value": "Docs"},
                        "childIds": [],
                        "backendDOMNodeId": 1,
                    }
                ]
            }
        if method == "DOMSnapshot.captureSnapshot":
            return {
                "documents": [
                    {
                        "nodes": {"backendNodeId": [1]},
                        "layout": {"nodeIndex": [0], "bounds": [[5, 6, 50, 20]]},
                    }
                ]
            }
        if method == "Runtime.evaluate":
            return {"result": {"value": state["url"]}}
        if method == "Page.navigate":
            state["url"] = params.get("url")
        return {}

    return (
        type(
            "C",
            (),
            {
                "execute_cdp": staticmethod(execute_cdp),
                "read_clipboard_text": lambda self, t: "",
                "write_clipboard_text": lambda self, t, x: None,
            },
        )(),
        1,
    )


def main() -> int:
    # CU surface (desktop, app-scoped) + BU surface (browser)
    cu_prov = McpComputerBackend(transport_factory=fake_mcp, app="Notes")
    bu_prov = get_backend("browser-runtime", client_factory=fake_browser)
    cu_h, bu_h = (
        cu_prov.create(SandboxSpec(metadata={"app": "Notes"})),
        bu_prov.create(),
    )
    cu, bu = cu_prov.connect(cu_h), bu_prov.connect(bu_h)
    ws = RoutedSession({"cu": cu, "bu": bu}, default="cu")

    try:
        caps = ws.capabilities
        print("routed capabilities (union):", caps.verbs)
        print("per-source:", caps.per_source)
        print(
            "route_for_target('https://docs') ->",
            route_for_target("https://docs"),
            "| route_for_target('Notes') ->",
            route_for_target("Notes"),
        )

        # one batch spanning both surfaces; each action says which surface (or implies it)
        batch = [
            {"verb": "observe", "structured": True, "surface": "cu"},
            {
                "verb": "click",
                "target": {"kind": "element_ref", "ref": "e4"},
                "surface": "cu",
            },
            {"verb": "navigate", "url": "https://example.com/docs"},  # implies BU
            {"verb": "observe", "structured": True, "surface": "bu"},
            {
                "verb": "click",
                "target": {"kind": "point_px", "x": 12, "y": 34},
                "surface": "bu",
            },
        ]
        res = ws.act_batch(batch, batch_id="demo")
        print("\nrouted batch results (source-tagged):")
        for r in res["results"]:
            print(f"    [{r['source']}] {r['verb']:9} ok={r['ok']}")

        print("\nprovenance log (what a trajectory records):")
        for e in ws.events:
            print(f"    #{e['i']} source={e['source']} verb={e['verb']} ok={e['ok']}")

        # and the Operator loop drives the routed session unchanged
        agent = ScriptedAgent(
            [
                [{"verb": "navigate", "url": "https://example.com"}],
                [{"verb": "observe", "structured": True, "surface": "bu"}],
            ]
        )
        out = drive(ws, agent, max_steps=3)
        print(f"\ndrive() over the routed session: {out.to_dict()}")
    finally:
        ws.close()
        cu_prov.destroy(cu_h)
        bu_prov.destroy(bu_h)

    print("\nOK — one operator loop spanned CU + BU with per-action source provenance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
