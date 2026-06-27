"""Live focused-window capture smoke: capture a specific window region (and the
active window) against a real shinkend, and verify the frame is a sub-region of the
full screen.

Env: optional SHK_ADDR (default 127.0.0.1:8765), required SHK_TOKEN, optional
SHK_WINDOW (an X11 window id, decimal) to capture by id.
"""

import os

import shinken

_PNG_SIG = b"\x89PNG\r\n\x1a\n"

addr = os.environ.get("SHK_ADDR", "127.0.0.1:8765")
token = os.environ.get("SHK_TOKEN")
wid = os.environ.get("SHK_WINDOW")

env = shinken.connect(addr, token=token)
print(f"connected: platform={env.platform}")

full = env.screenshot()
assert full["png"][:8] == _PNG_SIG
print(f"  screen: {full['w']}x{full['h']}")

# active_window: a valid PNG no larger than the screen (falls back to screen if the
# window manager publishes no active window — e.g. a bare desktop).
active = env.screenshot(scope="active_window")
assert active["png"][:8] == _PNG_SIG, "active_window frame is not a PNG"
assert active["w"] <= full["w"] and active["h"] <= full["h"]
print(f"  active_window: {active['w']}x{active['h']}")

if wid:
    shot = env.screenshot(scope=f"window:{wid}")
    assert shot["png"][:8] == _PNG_SIG, "window frame is not a PNG"
    assert 0 < shot["w"] <= full["w"] and 0 < shot["h"] <= full["h"], (
        f"window capture {shot['w']}x{shot['h']} is not within the screen"
    )
    assert shot["w"] < full["w"] or shot["h"] < full["h"], (
        f"window capture should be a sub-region, got the full screen {shot['w']}x{shot['h']}"
    )
    print(f"  window {wid}: {shot['w']}x{shot['h']}")

env.close()
print("window-capture smoke OK")
