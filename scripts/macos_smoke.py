"""macOS live smoke: connect to a locally-running shinkend with the native backend.

NON-DESTRUCTIVE by default — it observes (ready / screen_size / screenshot) and
moves the pointer to the screen center (a hover, never a click), because it runs
against YOUR live desktop, not a sandbox. Clicks/typing are gated behind
``--unsafe``.

Run shinkend first (the terminal needs the Screen Recording + Accessibility
grants — see docs/engineering/macos-engine.md):

    SHINKEND_TOKEN="$SHK_TOKEN" cargo run --manifest-path shinkend/Cargo.toml -- --backend macos
    python scripts/macos_smoke.py [addr] [--unsafe]

Exit codes: 0 = smoke passed; 2 = runtime reachable but TCC permissions pending
(the documented "grant and rerun" state); 1 = anything else.
"""

import os
import sys
from pathlib import Path

try:
    import shinken
except ImportError:  # repo checkout without `pip install -e sdk/python`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python" / "src"))
    import shinken

PERMISSION_GUIDANCE = (
    "TCC permissions pending: grant Screen Recording AND Accessibility to the app\n"
    "that launched shinkend (your terminal) in System Settings -> Privacy & Security,\n"
    "then restart that terminal and rerun this smoke."
)


def main() -> int:
    addr = next((a for a in sys.argv[1:] if not a.startswith("--")), "127.0.0.1:8765")
    unsafe = "--unsafe" in sys.argv[1:]

    token = os.environ.get("SHK_TOKEN")
    if not token:
        raise RuntimeError("SHK_TOKEN is required (use the same token passed to shinkend)")
    env = shinken.connect(addr, token=token)
    print(f"connected to {addr}: platform={env.platform}")
    assert env.platform == "macos", f"expected a macOS runtime, got {env.platform!r}"

    ready = env.query("ready")
    print(f"ready: {ready}")
    assert ready.get("display_up") is True, "no display reported up on a live Mac"

    size = env.screen_size()
    print(f"screen_size: {size['w']}x{size['h']} (capture pixels)")
    assert size["w"] >= 640 and size["h"] >= 480, f"implausible screen size {size}"

    if ready.get("permissions_pending"):
        # Still try the capture: the runtime must answer with the typed
        # permission_pending error, never crash or return a lying frame.
        try:
            env.screenshot()
        except Exception as e:
            print(f"screenshot while pending -> typed refusal: {e}")
            assert "permission_pending" in str(e), f"expected a permission_pending error, got: {e}"
        print(PERMISSION_GUIDANCE)
        env.close()
        return 2

    shot = env.screenshot()
    assert shot["png"][:8] == b"\x89PNG\r\n\x1a\n", "screenshot is not a PNG"
    assert len(shot["png"]) > 10_000, f"implausibly small screenshot ({len(shot['png'])} bytes)"
    assert (shot["w"], shot["h"]) == (size["w"], size["h"]), (
        f"screenshot {shot['w']}x{shot['h']} != screen_size {size['w']}x{size['h']} "
        "(the ACI coordinate space must be capture pixels)"
    )
    print(f"screenshot: {shot['w']}x{shot['h']}, {len(shot['png'])} bytes (PNG)")

    # Pointer: a hover at the screen center is harmless on any desktop.
    cx, cy = size["w"] // 2, size["h"] // 2
    env.move(x=cx, y=cy)
    print(f"moved pointer to {cx},{cy} (hover only)")

    if unsafe:
        # ACTUATES THE LIVE DESKTOP. A click at the center plus a few keystrokes —
        # only run with a throwaway window focused.
        env.click(x=cx, y=cy)
        env.type_text("shinken macos smoke")
        env.key("super+a")
        print("unsafe: click + type_text + key (super+a) executed")
    else:
        print("clicks/typing skipped (pass --unsafe to actuate the live desktop)")

    env.close()
    print("macOS smoke: ready + screen_size + screenshot + move OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
