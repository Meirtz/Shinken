"""Docker sandbox smoke: connect to a containerized shinkend (token auth) and
capture a screenshot off the in-container Xvfb desktop. Used by the Docker CI job.

Env: SHK_TOKEN (bearer token), optional SHK_ADDR (default 127.0.0.1:8765).
"""

import os

import shinken

addr = os.environ.get("SHK_ADDR", "127.0.0.1:8765")
env = shinken.connect(addr, token=os.environ["SHK_TOKEN"])
print(f"connected: platform={env.platform}, verbs={env.capabilities.verbs[:4]}")
shot = env.screenshot()
assert shot["png"][:8] == b"\x89PNG\r\n\x1a\n", "screenshot is not a PNG"
print(f"docker screenshot: {shot['w']}x{shot['h']}, {len(shot['png'])} bytes")
env.close()
print("docker sandbox smoke OK")
