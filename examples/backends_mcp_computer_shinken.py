"""Drive the Shinken operation layer over an MCP desktop computer-use server — modeled on
iFurySt/open-codex-computer-use (non-invasive Accessibility; 9 MCP tools). Because that
server observes via ``get_app_state`` (numbered AX tree) and clicks by ``element_index``,
this backend serves **structured observe + element_ref** — the same shape as Shinken's own
guest engine, which is exactly what fills the macOS-AX gap.

Scripted, no server install: a faithful in-memory MCP server (codex-shape content blocks —
a text AX tree + an image screenshot) stands in. Point ``transport_factory`` at a real
``open-computer-use mcp`` process to drive an actual desktop.

Run:  python examples/backends_mcp_computer_shinken.py
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.backends import get_backend  # noqa: E402
from shinken.providers.base import SandboxSpec, UnsupportedProviderOperation  # noqa: E402


def fake_mcp_server(_spec):
    """In-memory MCP computer-use server: a typed value tracks across set_value so observe
    reflects edits, like a real AX tree would."""
    state = {"vendor": ""}

    def mcp_call(tool: str, args: dict) -> dict:
        if tool == "get_app_state":
            tree = (
                f'‹1› window "Expense report"\n'
                f'‹7› text "Vendor" value="{state["vendor"]}"\n'
                f'‹9› button "Save"'
            )
            return {
                "content": [
                    {"type": "text", "text": tree},
                    {
                        "type": "image",
                        "data": base64.b64encode(b"\x89PNG" + b"px").decode(),
                        "mimeType": "image/png",
                    },
                ]
            }
        if tool == "set_value":
            state["vendor"] = args.get("value", "")
        if tool == "list_apps":
            return {"content": [{"type": "text", "text": "TextEdit\nSafari"}]}
        return {"content": [{"type": "text", "text": "ok"}]}

    return mcp_call


def main() -> int:
    provider = get_backend(
        "mcp-computer", transport_factory=fake_mcp_server, app="TextEdit"
    )
    print(
        "provider:",
        provider.capabilities.name,
        "| supports_fork =",
        provider.capabilities.supports_fork,
    )

    with provider.session(SandboxSpec(metadata={"app": "TextEdit"})) as env:
        print("\nsandbox capabilities (honest):")
        print("  verbs:", env.capabilities.verbs)
        print(
            "  targets:", env.capabilities.targets, "(element_ref served via AX tree)"
        )
        print(
            "  exec advertised:",
            "exec" in env.capabilities.verbs,
            "(non-invasive AX → no shell)",
        )

        obs = env.observe(structured=True)
        print("\nobserve(structured) — numbered AX tree:")
        for line in obs["tree_text"].splitlines():
            print("   ", line)

        # act by element_ref (the index from the tree) — the structured path
        env.act_on("e7", "set_value", text="ACME GmbH")
        env.click(ref="e9")  # Save
        diff = env.observe(structured=True)
        vendor = next(e for e in diff["elements"] if e["ref"] == "e7")
        print(
            f"\nafter set_value+click: e7 now '{vendor['name']}' / tree shows ACME:",
            "ACME GmbH" in diff["tree_text"],
        )

        try:
            env.checkpoint("golden")
        except UnsupportedProviderOperation as exc:
            print(f"\nfork-native degrade (expected): {exc}")

    print(
        "\nOK — Shinken drove a codex-style MCP computer-use backend (structured/element_ref)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
