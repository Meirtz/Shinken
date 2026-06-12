"""Drive the Shinken operation layer over a trycua/cua computer interface — the
multi-backend wedge: anything that speaks cua's verb surface can sit UNDER the Shinken ACI,
while fork-native consumers degrade loudly where the backend has no snapshot tier.

Scripted, no model API and no cua install: a protocol-faithful in-memory cua interface
(`FakeCuaInterface`, the same async surface `BaseComputerInterface` exposes) stands in for a
real cua sandbox, exactly like the other examples use synthetic peers. Swap it for a real
cua `Computer().interface` (interface_factory=...) to run against an actual cua VM.

Run:  python examples/backends_cua_shinken.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.backends import get_backend, list_backends  # noqa: E402
from shinken.providers.base import UnsupportedProviderOperation  # noqa: E402


class FakeCuaInterface:
    """A protocol-faithful, in-memory cua ``BaseComputerInterface`` (async). Records calls
    and returns plausible data so the adapter wiring runs with no cua VM."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._clip = ""
        self._typed = ""

    async def get_screen_size(self):
        return {"width": 1280, "height": 800}

    async def screenshot(self):
        self.calls.append(("screenshot",))
        return b"\x89PNG\r\n\x1a\n" + b"fake-pixels" + self._typed.encode()

    async def left_click(self, x, y):
        self.calls.append(("left_click", x, y))

    async def right_click(self, x, y):
        self.calls.append(("right_click", x, y))

    async def double_click(self, x, y):
        self.calls.append(("double_click", x, y))

    async def type_text(self, text):
        self.calls.append(("type_text", text))
        self._typed += text

    async def press_key(self, key):
        self.calls.append(("press_key", key))

    async def hotkey(self, *keys):
        self.calls.append(("hotkey", *keys))

    async def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    async def run_command(self, cmd):
        self.calls.append(("run_command", cmd))
        return {"stdout": f"ran: {cmd}\n", "stderr": "", "returncode": 0}

    async def get_accessibility_tree(self):
        return {
            "role": "window",
            "name": "Demo",
            "children": [
                {"role": "button", "name": "OK"},
                {"role": "text", "name": "Vendor", "value": self._typed},
            ],
        }

    async def copy_to_clipboard(self):
        return self._clip

    async def set_clipboard(self, text):
        self._clip = text


def main() -> int:
    print("registered backends:", list_backends())
    provider = get_backend("cua", interface_factory=lambda spec: FakeCuaInterface())
    print(
        "provider capabilities:",
        provider.capabilities.name,
        "| supports_fork =",
        provider.capabilities.supports_fork,
    )

    with provider.session() as env:
        print("\nsandbox capabilities (honest — only what cua serves):")
        print("  verbs:", env.capabilities.verbs)
        print("  structured_observation:", env.capabilities.structured_observation)

        env.click(x=640, y=420)
        env.type_text("Imagine Diffusion KK")
        env.key("ctrl+s")
        shot = env.screenshot()
        print(f"\nscreenshot: {len(shot['png'])} bytes, {shot['w']}x{shot['h']}")

        out = env.exec(["echo", "hello from cua backend"])
        print("exec:", out["stdout"].strip(), "| rc", out["returncode"])

        obs = env.observe(structured=True)
        print("structured observe (cua a11y tree):")
        for line in obs["tree_text"].splitlines():
            print("   ", line)

        # honest degradation: cua has no fork tier
        try:
            env.checkpoint("golden")
            print("\nUNEXPECTED: checkpoint succeeded")
        except UnsupportedProviderOperation as exc:
            print(f"\nfork-native degrade (expected): {exc}")

    print("\nOK — Shinken operation layer drove a cua backend end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
