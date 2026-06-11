"""S7 — head-to-head per-step loop cost: typed-WS ACI vs OSWorld's Flask/HTTP server.

OSWorld's guest server is the incumbent in-guest path most harnesses ship; this suite is
the direct measurement of what replacing it with the typed ACI buys. ONE sandbox (the
``shinken/sandbox-linux-osworld`` image, built by ``images/linux/Dockerfile.osworld``)
runs BOTH agent-facing servers against the SAME Xvfb display, same X session, same
frame content:

- ``shinkend`` — the typed-WS ACI on :8765, exactly as in every other suite, and
- OSWorld's ``desktop_env/server/main.py`` (Flask + pyautogui, fetched at image build
  time from the public OSWorld repo at a pinned commit) on :5000, launched the way the
  OSWorld VM's systemd unit launches it (``python main.py``).

Both are measured over loopback-published container ports, strictly sequentially
(never concurrently), interleaved within each rep so both systems sample the same
evolving desktop. The OSWorld endpoints are exercised EXACTLY the way OSWorld's own
``DesktopEnv`` client (``desktop_env/controllers/python.py``) calls them:

- observe: ``GET /screenshot`` → raw PNG body (server-side pyautogui/scrot capture +
  XFixes cursor overlay, written to disk, then ``send_file``);
- act: ``POST /execute`` with ``{"command": ["python", "-c", <pyautogui prefix +
  command>], "shell": false}`` — i.e. every OSWorld action spawns a fresh Python
  subprocess in the guest that re-imports pyautogui (and pays pyautogui's default
  0.1 s post-call PAUSE; the prefix sets FAILSAFE=False but not PAUSE=0). That is not
  a strawman: it is the documented, shipping action path of OSWorld's controller.

Byte accounting is dual and explicit, because the wire formats differ in opposite
directions: the ACI ships images base64-in-JSON (~33% wire overhead over the decoded
image), while OSWorld ships raw PNG over HTTP but spends ~0.7 KiB/action on the
pyautogui-prefix request body. Every datapoint records ``wire_bytes`` (HTTP bodies /
WS JSON frames; the ~200 B/call HTTP headers and ~8 B/frame WS framing are both
EXCLUDED, which slightly favors HTTP) and ``img_bytes`` (decoded image payload).

N=150 reps per cell × 10 cells = 1,500 datapoints. Emits
``benchmarks/results/osworld_loop.json`` and ``docs/assets/bench/osworld_loop.png``.

Run (build both images from this checkout first — see images/linux/Dockerfile.osworld):

    docker build -f images/linux/Dockerfile         -t shinken/sandbox-linux .
    docker build -f images/linux/Dockerfile.osworld -t shinken/sandbox-linux-osworld .
    python3 benchmarks/bench_osworld_loop.py
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

# This suite's sandbox is the dual-server image, not the plain one (its own knob:
# SHINKEN_BENCH_OSWORLD_IMAGE). Forcing SHINKEN_BENCH_IMAGE before importing _common
# makes env_meta() record the image actually measured, even when the caller exported
# SHINKEN_BENCH_IMAGE for the single-server suites.
os.environ["SHINKEN_BENCH_IMAGE"] = os.environ.get(
    "SHINKEN_BENCH_OSWORLD_IMAGE", "shinken/sandbox-linux-osworld"
)

from _common import (
    image_bytes,  # noqa: E402
    GEOMETRY,
    IMAGE,
    PALETTE,
    fill_xterm,
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)

REPS = int(os.environ.get("SHINKEN_BENCH_OSWORLD_REPS", "150"))
WARMUP = 5

# The OSWorld server revision baked into the image (images/linux/Dockerfile.osworld
# pins this hash; recorded into the result JSON so the baseline is auditable).
OSWORLD_REPO = "https://github.com/xlang-ai/OSWorld"
OSWORLD_COMMIT = "705623ca18e0055dd995fd5a350d6588cff2caf5"

# Verbatim from OSWorld desktop_env/controllers/python.py (PYAUTOGUI_PKGS_PREFIX,
# Apache-2.0) — the exact per-action preamble OSWorld's PythonController sends, so the
# /execute endpoint is exercised the way their own client drives it.
PYAUTOGUI_PKGS_PREFIX = (
    "import pyautogui; import time; import platform; "
    "pyautogui.FAILSAFE = False; "
    "_osworld_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\"<>?'; "
    "_osworld_linux_shift_chars = '~!@#$%^&*()_+' + chr(123) + chr(125) + '|:\">?'; "
    "pyautogui.isShiftCharacter = lambda character: character.isupper() or "
    "character in (_osworld_linux_shift_chars if platform.system() == 'Linux' else _osworld_shift_chars); "
    "{command}"
)


def _free_port(host: str = "127.0.0.1") -> int:
    sock = socket.socket()
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


def _compact_len(obj) -> int:
    """Wire size of a JSON frame: compact re-serialization of the parsed dict —
    byte-faithful to what shinkend/the SDK put on the WebSocket (both emit compact
    JSON), modulo key order."""
    return len(json.dumps(obj, separators=(",", ":")).encode())


# --- OSWorld side: stdlib urllib, one fresh TCP connection per call — matching the
# official client, which calls module-level requests.get/post (no Session, so no
# keep-alive either). ----------------------------------------------------------------


def osworld_screenshot(base: str, timeout: float = 10.0) -> tuple[float, bytes]:
    """GET /screenshot exactly like PythonController.get_screenshot (raw PNG body)."""
    t0 = now_ms()
    with urllib.request.urlopen(f"{base}/screenshot", timeout=timeout) as resp:
        body = resp.read()
    ms = now_ms() - t0
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(
            f"OSWorld /screenshot returned a non-PNG body ({body[:40]!r})"
        )
    return ms, body


def osworld_execute(
    base: str, command: str, timeout: float = 120.0
) -> tuple[float, int, int]:
    """POST /execute exactly like PythonController.execute_python_command: the pyautogui
    prefix + command as ``python -c``, JSON body, fresh connection. Returns
    (ms, request_bytes, response_bytes); raises if the guest subprocess failed."""
    payload = json.dumps(
        {
            "command": ["python", "-c", PYAUTOGUI_PKGS_PREFIX.format(command=command)],
            "shell": False,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/execute", data=payload, headers={"Content-Type": "application/json"}
    )
    t0 = now_ms()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    ms = now_ms() - t0
    reply = json.loads(body)
    if reply.get("status") != "success" or reply.get("returncode") != 0:
        raise RuntimeError(f"OSWorld /execute failed: {body[:200]!r}")
    return ms, len(payload), len(body)


# --- ACI side ------------------------------------------------------------------------


def aci_action_wire(action: dict, reply: dict) -> int:
    """Request frame + ack frame, compact JSON (the call_id placeholder is the same
    width as the SDK's real ids)."""
    req = {"type": "action", "call_id": "a000", "action": action}
    return _compact_len(req) + _compact_len(reply)


def aci_screenshot(env, fmt: str | None = None, quality: int | None = None):
    """One-shot ACI screenshot via act(), returning (ms, wire_bytes, img_bytes)."""
    t0 = now_ms()
    reply = env.act("screenshot", scope="screen", format=fmt, quality=quality)
    ms = now_ms() - t0
    img = reply.get("image") or {}
    decoded = len(image_bytes(img))
    action = {"verb": "screenshot", "scope": "screen"}
    if fmt:
        action.update(format=fmt, quality=quality)
    # reply side: the SDK records the exact on-the-wire size of the frame it
    # received (text JSON or binary) in `wire_len`; fall back to compact JSON with
    # raw-bytes fields counted at their length for pre-wire_len runtimes.
    reply_wire = reply.get("wire_len")
    if reply_wire is None:
        reply_wire = _compact_len(
            {
                k: (len(v) if isinstance(v, (bytes, bytearray)) else v)
                for k, v in reply.items()
            }
        )
    wire = _compact_len({"type": "action", "call_id": "a000", "action": action}) + int(
        reply_wire
    )
    return ms, wire, decoded


# --- sandbox lifecycle (direct `docker run`: the suite needs BOTH ports published,
# which DockerLocalProvider deliberately does not do) ---------------------------------


def boot_dual() -> tuple[str, str, str, "object"]:
    """Run one dual-server container; wait until BOTH servers observe the desktop.
    Returns (container_name, osworld_base_url, token, env)."""
    import shinken

    token = secrets.token_hex(16)
    aci_port, osw_port = _free_port(), _free_port()
    name = f"shinken-bench-osworld-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", f"SHINKEND_TOKEN={token}",
            "-e", f"SCREEN_GEOMETRY={GEOMETRY}",
            "-p", f"127.0.0.1:{aci_port}:8765",
            "-p", f"127.0.0.1:{osw_port}:5000",
            IMAGE,
        ],
        check=True,
        capture_output=True,
        text=True,
    )  # fmt: skip
    base = f"http://127.0.0.1:{osw_port}"
    env = None
    try:
        deadline = time.monotonic() + 120.0
        last: Exception | None = None
        while env is None and time.monotonic() < deadline:
            try:
                env = shinken.connect(f"127.0.0.1:{aci_port}", token=token)
            except Exception as exc:  # not up yet
                last = exc
                time.sleep(0.3)
        if env is None:
            raise RuntimeError(f"shinkend never became reachable: {last}")
        while time.monotonic() < deadline:  # desktop painted?
            if len(env.screenshot()["bytes"]) > 2048:
                break
            time.sleep(0.3)
        while time.monotonic() < deadline:  # OSWorld Flask up + capturing?
            try:
                osworld_screenshot(base, timeout=5.0)
                break
            except Exception as exc:
                last = exc
                time.sleep(0.5)
        else:
            raise RuntimeError(f"OSWorld server never became ready: {last}")
        return name, base, token, env
    except Exception:
        if env is not None:
            env.close()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        raise


def osworld_image_commit(image: str) -> str:
    """Read the pinned OSWorld commit back off the image label, so the JSON records
    what was actually measured even if the Dockerfile pin moves."""
    out = subprocess.run(
        [
            "docker", "image", "inspect", image,
            "--format", '{{index .Config.Labels "org.shinken.osworld_commit"}}',
        ],
        capture_output=True,
        text=True,
    )  # fmt: skip
    label = out.stdout.strip()
    return (
        label
        if out.returncode == 0 and label and label != "<no value>"
        else OSWORLD_COMMIT
    )


def frame_parity(aci_png: bytes, osw_png: bytes) -> dict:
    """Fairness evidence: decode both servers' PNGs of the same display and record the
    mean per-pixel delta — demonstrating both interfaces observed the same frame (the
    only expected difference is OSWorld's pasted cursor sprite; shinkend captures the
    framebuffer without compositing a cursor)."""
    import io

    from matplotlib import image as mpimg

    a = mpimg.imread(io.BytesIO(aci_png), format="png")
    b = mpimg.imread(io.BytesIO(osw_png), format="png")
    out = {"aci_shape": list(a.shape), "osworld_shape": list(b.shape)}
    if a.shape[:2] == b.shape[:2]:
        a3, b3 = a[:, :, :3], b[:, :, :3]
        diff = abs(a3 - b3)
        out["mean_abs_px_diff"] = round(float(diff.mean()), 6)
        out["pct_px_differing"] = round(float((diff.max(axis=2) > 1 / 255).mean()), 6)
        out["note"] = (
            "0..1 scale; expected nonzero only at the cursor sprite (OSWorld composites "
            "an XFixes cursor overlay, shinkend captures the raw framebuffer)"
        )
    return out


# --- the measurement -----------------------------------------------------------------


def run() -> dict:
    name, base, _token, env = boot_dual()
    points: list[dict] = []

    def rec(op: str, rep: int, ms: float, wire: int | None, img: int | None) -> None:
        points.append(
            {
                "op": op,
                "rep": rep,
                "ms": round(ms, 3),
                "wire_bytes": wire,
                "img_bytes": img,
            }
        )

    parity: dict = {}
    try:
        fill_xterm(env)  # realistic text content; both servers observe this display
        # One back-to-back capture pair through both interfaces, decoded and diffed:
        # evidence that the two servers see the same frame (idle desktop, no input
        # between the two captures).
        aci_png = env.screenshot()["bytes"]
        _, osw_png = osworld_screenshot(base)
        parity = frame_parity(aci_png, osw_png)
        for rep in range(-WARMUP, REPS):
            keep = rep >= 0

            # (a) input plane — same click target / same single typed char both ways.
            t0 = now_ms()
            reply = env.click(x=120, y=120)
            ms = now_ms() - t0
            if keep:
                wire = aci_action_wire(
                    {"verb": "click", "target": {"x": 120, "y": 120}}, reply
                )
                rec("aci click", rep, ms, wire, None)

            ms, req_b, resp_b = osworld_execute(base, "pyautogui.click(120, 120)")
            if keep:
                rec("osworld /execute click", rep, ms, req_b + resp_b, None)

            t0 = now_ms()
            reply = env.type_text("x")
            ms = now_ms() - t0
            if keep:
                wire = aci_action_wire({"verb": "type_text", "text": "x"}, reply)
                rec("aci type_text", rep, ms, wire, None)

            ms, req_b, resp_b = osworld_execute(base, "pyautogui.write('x')")
            if keep:
                rec("osworld /execute type", rep, ms, req_b + resp_b, None)

            # (b) observation plane — same display, back-to-back samples.
            ms, wire, img = aci_screenshot(env)
            if keep:
                rec("aci screenshot png", rep, ms, wire, img)

            ms, wire, img = aci_screenshot(env, fmt="jpeg", quality=80)
            if keep:
                rec("aci screenshot jpeg q80", rep, ms, wire, img)

            ms, body = osworld_screenshot(base)
            if keep:
                rec("osworld GET /screenshot", rep, ms, len(body), len(body))

            # (c) the full agent step, act-then-observe, composed each way.
            t0 = now_ms()
            reply = env.click(x=120, y=120)
            _, s_wire, s_img = aci_screenshot(env)
            ms = now_ms() - t0
            if keep:
                wire = aci_action_wire(
                    {"verb": "click", "target": {"x": 120, "y": 120}}, reply
                )
                rec("aci step png", rep, ms, wire + s_wire, s_img)

            t0 = now_ms()
            reply = env.click(x=120, y=120)
            _, s_wire, s_img = aci_screenshot(env, fmt="jpeg", quality=80)
            ms = now_ms() - t0
            if keep:
                wire = aci_action_wire(
                    {"verb": "click", "target": {"x": 120, "y": 120}}, reply
                )
                rec("aci step jpeg q80", rep, ms, wire + s_wire, s_img)

            t0 = now_ms()
            _, req_b, resp_b = osworld_execute(base, "pyautogui.click(120, 120)")
            _, body = osworld_screenshot(base)
            ms = now_ms() - t0
            if keep:
                rec("osworld step", rep, ms, req_b + resp_b + len(body), len(body))

            if rep % 25 == 0:
                print(f"rep {rep}/{REPS}", flush=True)
        print(f"{len(points)} datapoints", flush=True)
    finally:
        env.close()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    return {
        "reps": REPS,
        "warmup_reps_discarded": WARMUP,
        "frame_parity": parity,
        "osworld": {
            "repo": OSWORLD_REPO,
            "commit": osworld_image_commit(IMAGE),
            "server": "desktop_env/server/main.py — Flask dev server, `python main.py` "
            "(debug on), launched as in the OSWorld VM's osworld_server.service",
            "client_parity": "endpoints driven exactly as desktop_env/controllers/python.py "
            "does: GET /screenshot (raw PNG body) and POST /execute with the "
            "PYAUTOGUI_PKGS_PREFIX `python -c` command list; one fresh TCP connection "
            "per call (the official client uses module-level requests.* — no Session, "
            "no keep-alive)",
            "byte_accounting": "wire_bytes = HTTP bodies vs WS JSON frames (compact "
            "re-serialization); HTTP headers (~200 B/call) and WS framing (~8 B/frame) "
            "both excluded; img_bytes = decoded image payload (OSWorld ships raw PNG; "
            "the ACI ships base64-in-JSON, so its wire ≈ 1.33× decoded)",
        },
        "datapoints": points,
    }


def _ops(points: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in points:
        if p["op"] not in seen:
            seen.append(p["op"])
    return seen


# Head-to-head palette is SYSTEM-keyed (Shinken green/blue family vs neutral-gray
# baseline), not codec-keyed like the single-system suites.
_STEP_SERIES = [
    (
        "osworld step",
        PALETTE["neutral"],
        "OSWorld HTTP step (/execute + /screenshot PNG)",
    ),
    ("aci step png", PALETTE["async"], "ACI step (click + screenshot PNG)"),
    ("aci step jpeg q80", PALETTE["delta"], "ACI step (click + screenshot JPEG q80)"),
]


def plot(payload: dict) -> None:
    points = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)

    # Panel 1 — per-step latency CDFs.
    for op, color, label in _STEP_SERIES:
        xs = sorted(p["ms"] for p in points if p["op"] == op)
        if not xs:
            continue
        ys = [(i + 1) / len(xs) for i in range(len(xs))]
        ax1.plot(xs, ys, linewidth=1.6, color=color, label=label)
        p50 = xs[len(xs) // 2]
        ax1.axvline(p50, color=color, linewidth=0.7, alpha=0.4, linestyle="--")
    ax1.set_xscale("log")
    ax1.set_xlabel("full agent step, act + observe (ms, log)")
    ax1.set_ylabel("fraction of steps $\\leq$ x")
    ax1.set_title(
        f"Per-step latency CDF — same guest, same display (N={payload['reps']})"
    )
    ax1.legend(loc="center left", fontsize=8)

    # Panel 2 — bytes per step: wire vs decoded image payload.
    labels, wire_kib, img_kib, colors = [], [], [], []
    for op, color, _label in _STEP_SERIES:
        rows = [p for p in points if p["op"] == op]
        if not rows:
            continue
        labels.append(
            {
                "osworld step": "OSWorld\nHTTP",
                "aci step png": "ACI\nPNG",
                "aci step jpeg q80": "ACI\nJPEG q80",
            }[op]
        )
        wire_kib.append(sum(p["wire_bytes"] for p in rows) / len(rows) / 1024)
        img_kib.append(sum(p["img_bytes"] for p in rows) / len(rows) / 1024)
        colors.append(color)
    xpos = range(len(labels))
    ax2.bar(
        [x - 0.2 for x in xpos],
        wire_kib,
        width=0.38,
        color=colors,
        label="wire bytes/step",
    )
    ax2.bar(
        [x + 0.2 for x in xpos],
        img_kib,
        width=0.38,
        color=colors,
        alpha=0.45,
        hatch="//",
        label="decoded image bytes/step",
    )
    for x, w, i in zip(xpos, wire_kib, img_kib):
        ax2.text(x - 0.2, w, f"{w:.0f}", ha="center", va="bottom", fontsize=8)
        ax2.text(x + 0.2, i, f"{i:.0f}", ha="center", va="bottom", fontsize=8)
    ax2.set_xticks(list(xpos))
    ax2.set_xticklabels(labels)
    ax2.set_ylabel("KiB per step (mean)")
    ax2.set_title("Bytes per step — wire vs decoded image payload")
    ax2.legend(fontsize=8)

    save_plot(fig, "osworld_loop")


def main() -> int:
    payload = run()
    payload["summary"] = [
        {
            "op": op,
            "ms": summarize([p["ms"] for p in payload["datapoints"] if p["op"] == op]),
            "wire_bytes": summarize(
                [p["wire_bytes"] for p in payload["datapoints"] if p["op"] == op]
            ),
            "img_bytes": summarize(
                [
                    p["img_bytes"]
                    for p in payload["datapoints"]
                    if p["op"] == op and p["img_bytes"] is not None
                ]
            ),
        }
        for op in _ops(payload["datapoints"])
    ]
    write_result("osworld_loop", payload)
    plot(payload)
    for row in payload["summary"]:
        if row["op"] in {op for op, _c, _l in _STEP_SERIES}:
            print(
                f"{row['op']}: p50 {row['ms']['p50']} ms, "
                f"wire {row['wire_bytes']['p50'] / 1024:.1f} KiB/step"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
