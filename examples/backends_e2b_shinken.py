"""Drive the Shinken operation layer over an e2b-desktop backend — a cloud Linux desktop as
one more pluggable execution substrate under the typed ACI. e2b-desktop is pixel-only with a
real shell, so the backend advertises pointer/keyboard/scroll/screenshot/exec/launch_app and,
honestly, NO structured-observe and NO Shinken fork tier.

Scripted, no installs / no E2B_API_KEY / no cloud: a protocol-faithful in-memory e2b-desktop
Sandbox stands in (same method names the real SDK exposes — left_click/write/press/scroll/
drag/commands.run/launch/get_screen_size). Swap `fake_e2b` for the default factory and a real
key to run it for real.

Run:  python examples/backends_e2b_shinken.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))

from shinken.backends import RoutedSession, get_backend  # noqa: E402
from shinken.operator import ScriptedAgent, drive  # noqa: E402
from shinken.providers.base import UnsupportedProviderOperation  # noqa: E402


class _Commands:
    def run(self, cmd):
        return type(
            "R", (), {"stdout": f"$ {cmd}\n(ok)", "stderr": "", "exit_code": 0}
        )()


class FakeE2bDesktop:
    """In-memory stand-in for e2b_desktop.Sandbox (the real one is a cloud VM)."""

    sandbox_id = "e2b-demo-001"

    def __init__(self) -> None:
        self.commands = _Commands()
        self.log: list[str] = []

    def get_screen_size(self):
        return (1280, 800)

    def screenshot(self, fmt="bytes"):
        return bytearray(b"\x89PNG" + b"e2b-cloud-frame")

    def left_click(self, x=None, y=None):
        self.log.append(f"left_click({x},{y})")

    def right_click(self, x=None, y=None):
        self.log.append(f"right_click({x},{y})")

    def double_click(self, x=None, y=None):
        self.log.append(f"double_click({x},{y})")

    def middle_click(self, x=None, y=None):
        self.log.append(f"middle_click({x},{y})")

    def move_mouse(self, x, y):
        self.log.append(f"move_mouse({x},{y})")

    def mouse_press(self, button="left"):
        self.log.append(f"mouse_press({button})")

    def mouse_release(self, button="left"):
        self.log.append(f"mouse_release({button})")

    def scroll(self, direction="down", amount=1):
        self.log.append(f"scroll({direction},{amount})")

    def write(self, text, **_kw):
        self.log.append(f"write({text!r})")

    def press(self, key):
        self.log.append(f"press({key!r})")

    def drag(self, fr, to):
        self.log.append(f"drag({fr}->{to})")

    def launch(self, application, uri=None):
        self.log.append(f"launch({application})")

    def kill(self):
        self.log.append("kill")


def main() -> int:
    provider = get_backend("e2b", sandbox_factory=lambda spec: FakeE2bDesktop())
    print(
        "provider capabilities:",
        provider.capabilities.name,
        "| fork:",
        provider.capabilities.supports_fork,
    )

    with provider.session() as env:
        caps = env.capabilities
        print("sandbox verbs:", caps.verbs)
        print(
            "structured observation:",
            caps.structured_observation,
            "| targets:",
            caps.targets,
        )

        # an Operator loop drives this backend unchanged — wrap the single surface in a
        # RoutedSession so dispatch_action turns ACI action dicts into e2b verb calls
        ws = RoutedSession({"e2b": env}, default="e2b")
        agent = ScriptedAgent(
            [
                [{"verb": "launch_app", "app": "xterm"}],
                [{"verb": "click", "target": {"kind": "point_px", "x": 640, "y": 400}}],
                [{"verb": "type_text", "text": "hello from shinken"}],
                [{"verb": "key", "keys": "ctrl+s"}],
                [{"verb": "scroll", "dy": -3}],
            ]
        )
        out = drive(ws, agent, max_steps=6)
        print("\ndrive() over e2b-desktop:", out.to_dict())

        # e2b ships a shell, so exec is advertised + served
        print("exec:", env.exec(["echo", "from the cloud desktop"])["stdout"])

        # honest degradation — no a11y tree, no Shinken fork
        s = env.observe(structured=True)
        print("\nstructured observe (honest):", s["available"], "-", s["detail"])
        try:
            env.checkpoint("golden")
        except UnsupportedProviderOperation as exc:
            print("checkpoint (honest):", type(exc).__name__, "-", exc)

        print("\nwhat the backend actually called on the cloud desktop:")
        for line in env._sb.log:
            print("   ", line)

    print(
        "\nOK — the operator loop ran on e2b-desktop; fork/structured-observe degraded loudly."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
