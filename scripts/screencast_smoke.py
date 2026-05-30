"""Live screencast smoke: start a server-pushed screencast against a real
shinkend (X11/Xvfb), receive frames off the wire, and validate them.

The first frame is the initial capture and is always present; further frames
are collected opportunistically (a bare desktop may be static, in which case
idle-frame suppression yields nothing more — that is correct behaviour). Run
against the Linux integration Xvfb or the Docker container.

Env: optional SHK_ADDR (default 127.0.0.1:8765), optional SHK_TOKEN.
"""

import os

import shinken

_PNG_SIG = b"\x89PNG\r\n\x1a\n"

addr = os.environ.get("SHK_ADDR", "127.0.0.1:8765")
token = os.environ.get("SHK_TOKEN")
env = shinken.connect(addr, token=token)
print(f"connected: platform={env.platform}, observation_types={env.capabilities.observation_types}")
assert "start_screencast" in env.capabilities.verbs, "runtime does not advertise screencast"
assert "screencast" in env.capabilities.observation_types

frames = []
with env.screencast(fps=10, timeout=3, limit=8) as stream:
    it = iter(stream)
    frames.append(next(it))  # initial capture — always delivered
    for i in range(5):
        # nudge the desktop, then opportunistically grab any changed frame
        env.move(x=200 + i * 150, y=150 + i * 90)
        env.click(x=200 + i * 150, y=150 + i * 90)
        try:
            frames.append(next(it))
        except StopIteration:
            break

assert len(frames) >= 1, "no screencast frame received"
first = frames[0]
assert first["png"][:8] == _PNG_SIG, "frame is not a PNG"
assert first["w"] == 1280 and first["h"] == 800, f"unexpected frame size {first['w']}x{first['h']}"
seqs = [f["seq"] for f in frames]
assert seqs == sorted(seqs), f"frame seq not monotonic: {seqs}"
env.close()
print(f"screencast smoke OK — {len(frames)} live frame(s) over the wire, seqs={seqs}")
