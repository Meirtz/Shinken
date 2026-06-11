"""Live desktop-verb smoke (G2+G3): clipboard set→get + launch_app → list_windows →
activate_window → active-window confirmation.

Runs against a live shinkend on a real X display, in BOTH desktop shapes:
- CI's bare Xvfb (no WM): list_windows uses the query-tree fallback and
  activate_window uses the raise+set-input-focus fallback (the focused flag then
  comes from the input-focus fallback of active_window);
- the Docker sandbox image (openbox): the EWMH `_NET_ACTIVE_WINDOW` paths.

Env: ``SHK_TOKEN`` — bearer token (Docker image); ``SHK_LAUNCH_APP`` — the app to
launch (default ``xlogo``, present in both CI's x11-apps and the sandbox image).
Usage: python scripts/clipboard_app_smoke.py [addr]
"""

import os
import sys
import time
import uuid

import shinken

addr = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8765"
app = os.environ.get("SHK_LAUNCH_APP", "xlogo")
env = shinken.connect(addr, token=os.environ.get("SHK_TOKEN"))
caps = env.capabilities
for verb in ("clipboard_get", "clipboard_set", "launch_app", "activate_window"):
    assert verb in caps.verbs, f"runtime does not advertise {verb}"

# 1) clipboard roundtrip: shinkend owns the CLIPBOARD selection (no xclip), and the
#    get rides ConvertSelection back through the X server — twice, so a re-set
#    proves the owner updates rather than serving a stale buffer.
for token in (f"shinken-clip-{uuid.uuid4().hex[:12]}", "second ✂️ payload"):
    ack = env.clipboard_set(token)
    assert ack["ok"] is True, f"clipboard_set nacked: {ack}"
    got = env.clipboard_get()
    assert got == token, f"clipboard roundtrip mismatch: set {token!r}, got {got!r}"
print(f"clipboard set→get roundtrip OK ({len(token)} bytes, re-set verified)")

# 2) launch_app: spawn the app on the session display, then watch it appear in
#    list_windows (title match — the launch ack itself can't see the window map).
env.launch_app(app, ["-geometry", "180x140+420+260"])
win = None
deadline = time.time() + 15
while time.time() < deadline and win is None:
    win = next((w for w in env.list_windows() if app in w["title"].lower()), None)
    if win is None:
        time.sleep(0.25)
assert win is not None, f"launched {app!r} but no window with that title appeared"
assert win["w"] > 0 and win["h"] > 0, f"launched window has no geometry: {win}"
print(f"launch_app({app!r}) → window {win['id']:#x} {win['title']!r} {win['w']}x{win['h']}")

# 3) activate_window by id, then confirm the active window flipped to it (EWMH
#    _NET_ACTIVE_WINDOW under a WM; input-focus fallback on bare Xvfb).
env.activate_window(win["id"])
focused = False
deadline = time.time() + 10
while time.time() < deadline and not focused:
    entry = next((w for w in env.list_windows() if w["id"] == win["id"]), None)
    focused = bool(entry and entry["focused"])
    if not focused:
        time.sleep(0.25)
assert focused, f"window {win['id']:#x} never became the active window after activate_window"
print(f"activate_window({win['id']:#x}) → active_window confirms focus")

# 4) the app/title selector resolves to the same window (ack is enough — focus is
#    already proven above).
ack = env.activate_window(app=app)
assert ack["ok"] is True, f"activate_window(app=...) nacked: {ack}"
print(f"activate_window(app={app!r}) resolved by title")

env.close()
print("clipboard+app smoke: set→get roundtrip + launch→enumerate→activate OK")
