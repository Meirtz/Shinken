"""Live screencast smoke: start a server-pushed screencast against a real
shinkend (X11/Xvfb), receive frames off the wire, and validate them.

The first frame is the initial capture and is always present; further frames
are collected opportunistically (a bare desktop may be static, in which case
idle-frame suppression yields nothing more — that is correct behaviour). Run
against the Linux integration Xvfb or the Docker container.

Env: optional SHK_ADDR (default 127.0.0.1:8765), required SHK_TOKEN.
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

# Phase 1: a full-resolution frame over the wire.
with env.screencast(fps=10, timeout=3, limit=1) as stream:
    full = next(iter(stream))
assert full["png"][:8] == _PNG_SIG, "frame is not a PNG"
assert full["w"] == 1280 and full["h"] == 800, f"unexpected full-res size {full['w']}x{full['h']}"
print(f"  full-res frame: {full['w']}x{full['h']}, {len(full['png'])} bytes")

# Phase 2: a bandwidth-capped frame — the longer edge must be downscaled to 640.
with env.screencast(fps=10, timeout=3, limit=1, max_long_edge=640) as stream:
    small = next(iter(stream))
assert small["png"][:8] == _PNG_SIG, "capped frame is not a PNG"
long_edge = max(small["w"], small["h"])
assert long_edge == 640, f"downscale not applied (1280 long edge → expected 640, got {long_edge})"
assert len(small["png"]) < len(full["png"]), "downscaled frame should be smaller on the wire"
print(f"  capped frame:   {small['w']}x{small['h']}, {len(small['png'])} bytes")

env.close()
print("screencast smoke OK — full-res + bandwidth-capped frames verified over the wire")
