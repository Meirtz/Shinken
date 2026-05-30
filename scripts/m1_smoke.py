"""M1 live smoke: connect to a running shinkend and execute pointer actions.

Used by the Linux Xvfb integration CI job, which then verifies the cursor position
with `xdotool`. Usage: python scripts/m1_smoke.py [addr]
"""

import sys

import shinken

addr = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8765"
env = shinken.connect(addr)
print(f"connected to {addr}: platform={env.platform}, verbs={env.capabilities.verbs[:4]}")
env.move(x=300, y=200)
env.click(x=300, y=200)

shot = env.screenshot()
assert shot["png"][:8] == b"\x89PNG\r\n\x1a\n", "screenshot is not a PNG"
print(f"screenshot: {shot['w']}x{shot['h']}, {len(shot['png'])} bytes")
assert (shot["w"], shot["h"]) == (1280, 800), f"unexpected screen size {shot['w']}x{shot['h']}"

# keyboard: executes against the real X server without error (no focused app to read back)
env.type_text("hello shinken")
env.key("ctrl+a")
print("type_text + key executed")

env.close()
print("M1 smoke: move + click + screenshot + type + key OK")
