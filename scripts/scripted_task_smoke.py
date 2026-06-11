"""E1 proof: an agent completes a scripted >=5-step task in a real Linux GUI app entirely
through the ACI (no direct host access), verified against real guest state.

Drives the live `xterm` in the reference Docker sandbox via the Operator loop + a
ScriptedAgent: focus the terminal, type a shell command that writes a file, run it. The
verifier reads the REAL guest file back over the file-transfer channel (`get_file`) and
checks its content — so a broken actuation fails the check, not a self-fulfilling assert.

Run (Docker required):  PYTHONPATH=sdk/python/src python scripts/scripted_task_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from shinken.operator import ScriptedAgent, drive
from shinken.providers.base import SandboxSpec
from shinken.providers.docker import DockerLocalProvider

MARKER = "shinken-e1-ok"
GUEST_FILE = "/tmp/shinken_e1.txt"


def _plan() -> list[list[dict]]:
    # >=5 ACI actions: focus xterm, type the command, run it, then a settle wait. Coordinates
    # land inside the xterm window start.sh launches at 80x24+20+20.
    px = {"kind": "point_px", "x": 120, "y": 120}
    return [
        [{"verb": "click", "target": px}],
        [{"verb": "type_text", "text": f"echo {MARKER} > {GUEST_FILE}"}],
        [{"verb": "key", "keys": "Return"}],
        [{"verb": "type_text", "text": "sync"}],
        [{"verb": "key", "keys": "Return"}],
        [{"verb": "wait", "ms": 800}],
    ]


def _wait_for_focused_window(env, timeout_s: float = 20.0) -> None:
    """Readiness fires when the desktop paints/maps — keyboard focus converges a
    beat later (openbox's click-to-focus grabs + start.sh's activate loop). Typing
    before that lands in openbox's no-focus fallback and silently vanishes, which
    is exactly what slow CI runners hit. Poll for a focused window, re-clicking
    the xterm as a nudge."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            wins = env.list_windows()
        except Exception:
            return  # pre-list_windows runtime: best effort
        if any(w.get("focused") for w in wins):
            return
        if wins:
            env.click(x=120, y=120)
        time.sleep(0.25)
    print(
        "warning: no focused window within "
        f"{timeout_s:.0f}s; typed input may be dropped",
        flush=True,
    )


def main() -> int:
    prov = DockerLocalProvider(image="shinken/sandbox-linux")
    handle = prov.create(SandboxSpec(screen_geometry="1280x800x24"))
    ok = False
    try:
        env = prov.connect(handle)
        try:
            _wait_for_focused_window(env)
            res = drive(env, ScriptedAgent(_plan()), max_steps=10)
            print(
                f"drive: steps={res.steps} actions={res.actions} stopped={res.stopped}"
            )
            time.sleep(1.0)  # let the shell flush the redirect before we read it back
            out = Path(tempfile.mkdtemp()) / "e1.txt"
            ref = env.get_file(GUEST_FILE, str(out))
            content = out.read_text().strip()
            print(
                f"guest file {GUEST_FILE!r} -> {content!r} (sha {ref['sha256'][:12]})"
            )
            ok = content == MARKER
        finally:
            env.close()
    finally:
        with __import__("contextlib").suppress(Exception):
            prov.destroy(handle)
    print(
        "E1 PASS: real-app scripted task verified via guest state" if ok else "E1 FAIL"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
