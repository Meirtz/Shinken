"""S6 — client-plane ceiling: one SDK event loop demuxing N sandboxes, text vs binary.

The fan-out suites (S5) measure real sandboxes; this one isolates the CLIENT plane —
how many observation bytes/frames one Python event loop (≈ one client core) can absorb
— by replacing shinkend with in-process mock servers that answer `screenshot` RPCs
with a fixed, realistic payload (PAYLOAD_BYTES ≈ the measured 1280×800 q80-JPEG p50
from S1). Both wire formats are exercised through the REAL SDK demux path:

- ``text``   — base64-in-JSON observation (the pre-binary wire format): the client
  pays `json.loads` over a megabyte-class string + `b64decode` per frame.
- ``binary`` — binary WS media frame (`u32 LE header_len | JSON header | raw
  payload`, negotiated via `hello.accept.binary_frames`): the client pays one small
  JSON parse + one slice per frame.

For each mode × N (concurrent connections) every connection runs back-to-back
`screenshot()` RPCs for DURATION_S on ONE client event loop; we record aggregate
frames/s, payload+wire Mbps, and per-call RTT distributions. The mock servers run as
SEPARATE PROCESSES (``python bench_wire_ceiling.py --serve <port>``) so the client
process owns its GIL/core outright and the measured ceiling is the client plane's,
not the mock's. The host is still shared (`meta.contended_host`); run when quiet.

Emits benchmarks/results/wire_ceiling.json and
docs/assets/bench/wire_ceiling.png.

Run:  python benchmarks/bench_wire_ceiling.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import socket
import subprocess
import sys
import time

from _common import new_axes, save_plot, summarize, write_result

# ≈ the S1-measured desktop 1280x800 q80 JPEG p50 (87.9 KB) — see codec_ladder.json.
PAYLOAD_BYTES = 88_000
NS = [1, 64, 256, 1024]
DURATION_S = 4.0
SERVER_PROCS = 4
RTT_SAMPLE_CAP = 500  # per cell, for the CDF plot (full counts still recorded)

_PAYLOAD = bytes(random.Random(20260611).getrandbits(8) for _ in range(PAYLOAD_BYTES))
_PAYLOAD_B64 = base64.b64encode(_PAYLOAD).decode()


def _text_reply(call_id: str) -> str:
    """A base64-in-JSON observation answering `call_id` (prebuilt around the id)."""
    return (
        '{"type":"observation","obs_id":"o","cause":"' + call_id + '",'
        '"image":{"ref":"' + _PAYLOAD_B64 + '","w":1280,"h":800,'
        '"scope":"screen","format":"jpeg"}}'
    )


def _binary_reply(call_id: str) -> bytes:
    """A binary media frame answering `call_id` (small header + raw payload)."""
    header = json.dumps(
        {
            "type": "observation",
            "obs_id": "o",
            "cause": call_id,
            "image": {
                "off": 0,
                "len": len(_PAYLOAD),
                "w": 1280,
                "h": 800,
                "scope": "screen",
                "format": "jpeg",
            },
        }
    ).encode()
    return len(header).to_bytes(4, "little") + header + _PAYLOAD


async def _handler(ws):
    """Minimal mock shinkend: hello→welcome (advertising binary_frames) and
    screenshot→prebuilt observation, honoring the session's accept.binary_frames."""
    binary = False
    async for raw in ws:
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "hello":
            binary = bool((msg.get("accept") or {}).get("binary_frames"))
            await ws.send(
                json.dumps(
                    {
                        "type": "welcome",
                        "v": 0,
                        "server": {"name": "mock", "version": "0", "platform": "linux"},
                        "capabilities": {
                            "schema_version": 0,
                            "verbs": ["screenshot"],
                            "targets": ["point_px"],
                            "observation_types": ["screenshot"],
                            "max_long_edge": 2576,
                            "image_formats": ["png", "jpeg"],
                            "binary_frames": True,
                        },
                    }
                )
            )
        elif kind == "action":
            cid = msg.get("call_id")
            if (msg.get("action") or {}).get("verb") == "screenshot":
                await ws.send(_binary_reply(cid) if binary else _text_reply(cid))
            else:
                await ws.send(json.dumps({"type": "ack", "call_id": cid, "ok": True}))


async def _serve_forever(port: int) -> None:
    """One mock-server process: serve until killed. compression=None matches the
    real runtime (tungstenite never negotiates permessage-deflate), so the python
    mock doesn't add a per-frame deflate cost production never pays."""
    from websockets.asyncio.server import serve

    await serve(_handler, "127.0.0.1", port, max_size=16 * 2**20, compression=None)
    print("READY", flush=True)
    await asyncio.Event().wait()


def _start_servers(n_procs: int) -> tuple[list[str], list[subprocess.Popen]]:
    """`n_procs` mock-server PROCESSES (own GIL each); returns (addrs, procs)."""
    addrs: list[str] = []
    procs: list[subprocess.Popen] = []
    for _ in range(n_procs):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", str(port)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None and proc.stdout.readline().strip() == "READY", (
            "mock server process failed to start"
        )
        addrs.append(f"127.0.0.1:{port}")
        procs.append(proc)
    return addrs, procs


async def _run_cell(server_addrs: list[str], n: int, binary: bool) -> dict:
    """N concurrent screenshot loops on THIS event loop for DURATION_S."""
    from shinken import aconnect

    sandboxes = await asyncio.gather(
        *(
            aconnect(server_addrs[i % len(server_addrs)], binary_frames=binary)
            for i in range(n)
        )
    )
    counts = [0] * n
    payload_bytes = [0] * n
    wire_bytes = [0] * n
    rtts: list[float] = []
    deadline = time.perf_counter() + DURATION_S

    async def worker(i: int) -> None:
        sb = sandboxes[i]
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            shot = await sb.screenshot()
            rtts.append((time.perf_counter() - t0) * 1000.0)
            counts[i] += 1
            payload_bytes[i] += len(shot["bytes"])
            wire_bytes[i] += shot["wire_len"] or 0

    t_start = time.perf_counter()
    await asyncio.gather(*(worker(i) for i in range(n)))
    elapsed = time.perf_counter() - t_start
    await asyncio.gather(*(sb.close() for sb in sandboxes))

    frames = sum(counts)
    sample = rtts if len(rtts) <= RTT_SAMPLE_CAP else random.Random(7).sample(rtts, RTT_SAMPLE_CAP)
    return {
        "mode": "binary" if binary else "text",
        "n": n,
        "duration_s": round(elapsed, 3),
        "frames": frames,
        "frames_per_s": round(frames / elapsed, 1),
        "payload_bytes": sum(payload_bytes),
        "wire_bytes": sum(wire_bytes),
        "payload_mbps": round(sum(payload_bytes) * 8 / elapsed / 1e6, 1),
        "wire_mbps": round(sum(wire_bytes) * 8 / elapsed / 1e6, 1),
        "rtt_ms": summarize(rtts),
        "rtt_sample_ms": [round(x, 3) for x in sample],
    }


def run() -> dict:
    addrs, procs = _start_servers(SERVER_PROCS)
    cells: list[dict] = []
    try:
        for binary in (False, True):
            for n in NS:
                cell = asyncio.run(_run_cell(addrs, n, binary))
                cells.append(cell)
                print(
                    f"{cell['mode']:>6} N={n:4d}: {cell['frames_per_s']:8.1f} frames/s, "
                    f"wire {cell['wire_mbps']:7.1f} Mbps, payload {cell['payload_mbps']:7.1f} Mbps, "
                    f"rtt p50 {cell['rtt_ms']['p50']:.2f} ms",
                    flush=True,
                )
    finally:
        for proc in procs:
            proc.terminate()
    return {
        "payload_bytes_per_frame": PAYLOAD_BYTES,
        "ns": NS,
        "duration_s_per_cell": DURATION_S,
        "server_procs": SERVER_PROCS,
        "cells": cells,
    }


def plot(payload: dict) -> None:
    cells = payload["cells"]
    fig, (ax1, ax2) = new_axes(2)
    styles = {"text": ("C0", "o"), "binary": ("C2", "s")}
    for mode in ("text", "binary"):
        rows = [c for c in cells if c["mode"] == mode]
        color, marker = styles[mode]
        ax1.plot(
            [c["n"] for c in rows],
            [c["frames_per_s"] for c in rows],
            marker=marker,
            color=color,
            label=mode,
        )
        ax2.plot(
            [c["n"] for c in rows],
            [c["wire_mbps"] for c in rows],
            marker=marker,
            color=color,
            label=f"{mode} (wire)",
        )
        ax2.plot(
            [c["n"] for c in rows],
            [c["payload_mbps"] for c in rows],
            marker=marker,
            color=color,
            linestyle="--",
            alpha=0.6,
            label=f"{mode} (payload)",
        )
    for ax, ylab, title in (
        (ax1, "frames/s (aggregate)", "Observation RPCs one client loop sustains"),
        (ax2, "Mbps (aggregate)", "Observation throughput at the client plane"),
    ):
        ax.set_xscale("log", base=2)
        ax.set_xticks(payload["ns"])
        ax.set_xticklabels([str(n) for n in payload["ns"]])
        ax.set_xlabel("concurrent sandboxes N (one client event loop)")
        ax.set_ylabel(ylab)
        ax.set_title(f"{title}\n({payload['payload_bytes_per_frame'] // 1000} KB synthetic frames, loopback peers)")
        ax.legend(fontsize=8)
    save_plot(fig, "wire_ceiling")


def main() -> int:
    payload = run()
    payload["meta_extra"] = {
        "contended_host": True,  # servers + client share this machine/process group
        "loadavg_1m": round(os.getloadavg()[0], 2),
    }
    write_result("wire_ceiling", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--serve":
        asyncio.run(_serve_forever(int(sys.argv[2])))
        sys.exit(0)
    sys.exit(main())
