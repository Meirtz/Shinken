"""Live action-surface smoke: drag + mouse_down/up + observe-after-act + list_windows.

Run by the Linux Xvfb integration CI job AFTER the xclock window is up. The job
verifies the drag's end position with `xdotool` afterwards, so the drag to
(640, 360) must be the LAST pointer action here.

Env: ``SHK_EXPECT_WINDOW`` — an X window id (decimal) that must appear in
list_windows (CI passes the xclock id it found via xdotool); ``SHK_TOKEN`` — bearer
token for the mandatory authenticated handshake.
Usage: python scripts/action_surface_smoke.py [addr]
"""

import os
import sys

import shinken

addr = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8765"
env = shinken.connect(addr, token=os.environ.get("SHK_TOKEN"))
caps = env.capabilities
for verb in ("drag", "mouse_down", "mouse_up"):
    assert verb in caps.verbs, f"runtime does not advertise {verb}"
assert caps.observe_after_act, "runtime does not advertise observe_after_act"

# 1) decomposed gesture: button down at (200,150) -> move -> release in place
env.mouse_down(x=200, y=150)
env.move(x=240, y=180)
env.mouse_up()
print("mouse_down / move / mouse_up executed")

# 2) act-returns-observation: one round trip = click + fresh frame, correlated by
#    the same call_id; the frame must match the live screen geometry.
size = env.screen_size()
obs = env.click(x=5, y=5, observe={"format": "jpeg", "quality": 70})
assert obs["format"] == "jpeg" and obs["bytes"][:2] == b"\xff\xd8", "observe frame is not a JPEG"
assert (obs["w"], obs["h"]) == (size["w"], size["h"]), (
    f"observe frame {obs['w']}x{obs['h']} != screen {size['w']}x{size['h']}"
)
print(f"click+observe: {obs['w']}x{obs['h']} jpeg, {len(obs['bytes'])} bytes")

# 3) list_windows enumerates the xclock CI launched (the WM-less fallback path —
#    bare Xvfb publishes no EWMH client list).
windows = env.list_windows()
print("windows:", [(w["id"], w["title"], w["w"], w["h"], w["focused"]) for w in windows])
expect = os.environ.get("SHK_EXPECT_WINDOW")
if expect:
    win = next((w for w in windows if w["id"] == int(expect)), None)
    assert win is not None, f"expected window {expect} in {[w['id'] for w in windows]}"
    assert win["w"] > 0 and win["h"] > 0, f"window {expect} has no usable geometry: {win}"
    print(f"window {expect} enumerated: {win['title']!r} {win['w']}x{win['h']}")

# 4) drag LAST: CI then asserts the pointer parked at (640, 360) via xdotool.
env.drag(x=100, y=100, to_x=640, to_y=360, duration_ms=200)
print("dragged 100,100 -> 640,360 over an interpolated 200 ms path")

env.close()
print("action-surface smoke: gestures + observe + list_windows OK")
