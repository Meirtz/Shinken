#!/usr/bin/env python3
"""Spike #3 (CRIU memory tier) — in-container usability probe.

Polls shinkend's WebSocket endpoint until the handshake succeeds, then takes one
screenshot, and prints a one-line JSON with the elapsed milliseconds for each stage:

    {"ws_ready_ms": ..., "screenshot_ms": ..., "png_bytes": ..., "ok": true}

`ws_ready_ms` is measured from probe start to the first successful `shinken.connect`
(handshake + hello), so run this the instant `criu restore` returns to capture the
restore→usable gap. Uses the repo SDK (bind-mounted at /opt/shinken/src, PYTHONPATH set
by the runner) so the verification path is the same one agents use.
"""

import json
import os
import sys
import time

import shinken

ADDR = os.environ.get("SHINKEND_PROBE_ADDR", "127.0.0.1:8765")
TOKEN = os.environ.get("SHINKEND_TOKEN", "")
DEADLINE_S = float(os.environ.get("SHINKEND_PROBE_DEADLINE_S", "30"))

t0 = time.monotonic()
sb = None
last_err = "deadline"
while time.monotonic() - t0 < DEADLINE_S:
    try:
        sb = shinken.connect(ADDR, token=TOKEN)
        break
    except Exception as exc:  # connection refused until the restored tree is live
        last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(0.02)

if sb is None:
    print(json.dumps({"ok": False, "error": last_err}))
    sys.exit(1)

t1 = time.monotonic()
obs = sb.observe()
t2 = time.monotonic()
png = obs.get("png") or b""
if isinstance(png, str):
    import base64

    png = base64.b64decode(png)
sb.close()

print(
    json.dumps(
        {
            "ws_ready_ms": round((t1 - t0) * 1000, 1),
            "screenshot_ms": round((t2 - t1) * 1000, 1),
            "png_bytes": len(png),
            "ok": len(png) > 8,
        }
    )
)
