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
env.close()
print("M1 smoke: move + click sent")
