"""Drive the Shinken operation layer over a browser (CDP) — the BU half, alongside the CU
backends (cua, mcp-computer). This realizes Shinken's designed Browser Runtime (D13) as a
backend, wrapping iFurySt/open-browser-use's tab-scoped CDP client.

Three tab surfaces, all here: pixels (screenshot + click x,y), semantic node-ids
(observe(structured) → element_ref via the SAME parse_ax_tree→a11y path the desktop guest
engine uses), and locator/script (navigate / eval).

Scripted, no Chrome and no open-browser-use install: a protocol-faithful in-memory CDP
client stands in. Point client_factory at a real OpenBrowserUseClient to drive Chrome.

Run:  python examples/backends_browser_runtime_shinken.py
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.backends import get_backend  # noqa: E402
from shinken.providers.base import UnsupportedProviderOperation  # noqa: E402


class FakeBrowserClient:
    """In-memory open-browser-use client: answers CDP calls (screenshot / a11y tree+bounds /
    eval / input) so the backend wiring runs with no Chrome. `value` tracks across insertText."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._value = ""
        self._url = "about:blank"
        self._clip = ""

    def execute_cdp(self, tab_id: int, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if method == "Page.captureScreenshot":
            return {
                "data": base64.b64encode(b"\x89PNG" + self._value.encode()).decode()
            }
        if method == "Accessibility.getFullAXTree":
            return {
                "nodes": [
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
            }
        if method == "DOMSnapshot.captureSnapshot":
            return {
                "documents": [
                    {
                        "nodes": {"backendNodeId": [1, 2, 3]},
                        "layout": {
                            "nodeIndex": [0, 1, 2],
                            "bounds": [
                                [0, 0, 800, 600],
                                [40, 80, 200, 30],
                                [40, 130, 80, 30],
                            ],
                        },
                    }
                ]
            }
        if method == "Runtime.evaluate":
            expr = params.get("expression", "")
            return {
                "result": {"value": self._url if "location" in expr else self._value}
            }
        if method == "Input.insertText":
            self._value += params.get("text", "")
        if method == "Page.navigate":
            self._url = params.get("url", self._url)
        return {}

    def read_clipboard_text(self, tab_id):
        return self._clip

    def write_clipboard_text(self, tab_id, text):
        self._clip = text


def main() -> int:
    provider = get_backend(
        "browser-runtime", client_factory=lambda spec: (FakeBrowserClient(), 1)
    )
    print(
        "provider:",
        provider.capabilities.name,
        "| display tier:",
        provider.capabilities.tier,
        "| supports_fork =",
        provider.capabilities.supports_fork,
    )

    with provider.session() as env:
        print("\nsandbox capabilities (browser facade):")
        print("  verbs:", env.capabilities.verbs)
        print(
            "  structured_observation:",
            env.capabilities.structured_observation,
            "| exec advertised:",
            "exec" in env.capabilities.verbs,
        )

        env.navigate("https://example.com/expense")  # surface 3: locator/script
        print("\nnavigated; url via eval:", env.eval("window.location.href"))

        obs = env.observe(structured=True)  # surface 2: semantic node-ids
        print("structured observe (CDP a11y tree → element_refs):")
        for e in obs["elements"]:
            print(f"    {e['ref']} {e['role']} {e['name']!r} bbox={e.get('bbox')}")

        # act by element_ref (node center → CDP mouse), then pixel screenshot (surface 1)
        env.click(ref=next(e["ref"] for e in obs["elements"] if e["role"] == "textbox"))
        env.type_text("ACME GmbH")
        shot = env.screenshot()
        print(f"\nclicked textbox + typed; screenshot {len(shot['png'])} bytes")
        print("eval value reflects typed text:", "ACME GmbH" in (env.eval("x") or ""))

        try:
            env.checkpoint("golden")
        except UnsupportedProviderOperation as exc:
            print(f"\nfork-native degrade (expected — tabs are ephemeral): {exc}")

    print(
        "\nOK — Shinken drove a browser runtime (BU) backend across all three tab surfaces."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
