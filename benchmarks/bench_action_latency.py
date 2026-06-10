"""S3 — ACI action/observation latency distributions on the local sandbox.

Per-operation round-trip latency through the synchronous SDK facade (so the
numbers are what a sync caller — an eval loop, a trainer step — actually pays:
guest capture/injection + shinkend encode + wire framing + loopback WebSocket +
SDK background-loop hop). N repetitions per op, recorded individually, so the doc
can show full distributions (p50/p90/p99 + CDF), not just means.

Ops: ping, move, click, key, type_text (input plane); screenshot PNG native,
screenshot JPEG q80 native, screenshot JPEG q80 @1024 (observation plane), each
measured over BOTH wire formats — `binary` (binary WS media frames, the
negotiated default) and `text` (base64-in-JSON, the pre-binary format, pinned
via ``connect(..., binary_frames=False)``) — against the SAME sandbox, so the
text-vs-binary delta is same-host, same-desktop. Screenshot rows also record
decoded payload bytes and on-the-wire bytes.

Emits benchmarks/results/action_latency.json and
docs/assets/bench/action_latency.png.

Run:  python benchmarks/bench_action_latency.py
"""

from __future__ import annotations

import random
import sys

from _common import PALETTE, boot, fill_xterm, new_axes, now_ms, save_plot, summarize, write_result

INPUT_REPS = 300
OBS_REPS = 150


def run() -> dict:
    import shinken

    provider, handle, env = boot()
    # A second session against the SAME sandbox, pinned to the legacy text wire
    # format — the A/B half of the text-vs-binary observation measurements.
    text_env = shinken.connect(handle.addr, token=handle.token, binary_frames=False)
    rng = random.Random(20260611)
    points: list[dict] = []

    def rec(op: str, rep: int, t0: float, shot: dict | None = None) -> None:
        row = {"op": op, "rep": rep, "ms": round(now_ms() - t0, 3)}
        if shot is not None:
            row["payload_bytes"] = len(shot["bytes"])
            row["wire_bytes"] = shot.get("wire_len")
        points.append(row)

    try:
        fill_xterm(env)  # realistic frame for the observation ops
        for rep in range(INPUT_REPS):
            t0 = now_ms()
            env.ping()
            rec("ping", rep, t0)

            x, y = rng.randint(50, 1230), rng.randint(50, 750)
            t0 = now_ms()
            env.move(x=x, y=y)
            rec("move", rep, t0)

            t0 = now_ms()
            env.click(x=120, y=120)  # keep focus in the xterm
            rec("click", rep, t0)

            t0 = now_ms()
            env.key("space")
            rec("key", rep, t0)

            t0 = now_ms()
            env.type_text("x")
            rec("type_text", rep, t0)
        for rep in range(OBS_REPS):
            # Interleave the two wire formats so drift (desktop content, host
            # load) hits both equally.
            for label, e in (("binary", env), ("text", text_env)):
                t0 = now_ms()
                shot = e.screenshot()
                rec(f"screenshot png ({label})", rep, t0, shot)
                assert shot["format"] == "png"

                t0 = now_ms()
                shot = e.screenshot(format="jpeg", quality=80)
                rec(f"screenshot jpeg q80 ({label})", rep, t0, shot)
                assert shot["format"] == "jpeg"

                t0 = now_ms()
                e.act("screenshot", format="jpeg", quality=80, max_long_edge=1024)
                rec(f"screenshot jpeg q80 @1024 ({label})", rep, t0)
        print(f"{len(points)} datapoints", flush=True)
    finally:
        text_env.close()
        env.close()
        provider.destroy(handle)
    return {"input_reps": INPUT_REPS, "obs_reps": OBS_REPS, "datapoints": points}


def _ops(points: list[dict]) -> list[str]:
    seen: list[str] = []
    for p in points:
        if p["op"] not in seen:
            seen.append(p["op"])
    return seen


def plot(payload: dict) -> None:
    from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter

    points = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)
    input_ops = ["ping", "move", "click", "key", "type_text"]
    # observation ops keep the repo-wide codec colors (png red, jpeg blue);
    # binary wire = solid, text wire = dashed, same color per screenshot kind.
    obs_kinds = [
        ("screenshot png", "PNG native", PALETTE["png"]),
        ("screenshot jpeg q80", "JPEG q80 native", PALETTE["jpeg"]),
        ("screenshot jpeg q80 @1024", "JPEG q80 @1024", "#5dade2"),
    ]
    obs_series = [
        (f"{kind} ({mode})", f"{label} ({mode})", color, "-" if mode == "binary" else "--")
        for kind, label, color in obs_kinds
        for mode in ("binary", "text")
    ]

    def cdf(ax, op: str, label: str, color: str | None, ls: str = "-") -> None:
        xs = sorted(p["ms"] for p in points if p["op"] == op)
        if not xs:
            return
        ys = [(i + 1) / len(xs) for i in range(len(xs))]
        ax.plot(xs, ys, linewidth=1.4, label=label, color=color, linestyle=ls)

    # Left: input-plane ops — log x, pinned to plain-number ticks that span
    # the data so the curves sit under labeled ticks.
    for op in input_ops:
        cdf(ax1, op, op, None)
    ax1.set_xscale("log")
    ax1.xaxis.set_major_locator(FixedLocator([0.4, 0.6, 1, 2, 3]))
    ax1.xaxis.set_minor_locator(NullLocator())
    fmt = ScalarFormatter()
    fmt.set_scientific(False)
    ax1.xaxis.set_major_formatter(fmt)
    ax1.set_xlim(0.35, 3.3)
    ax1.set_xlabel("round-trip ms (log)")
    ax1.set_title(f"Latency CDF — input ops (N={payload['input_reps']}/op)")

    # Right: observation ops, binary vs text wire — narrow range, linear x.
    for op, label, color, ls in obs_series:
        cdf(ax2, op, label, color, ls)
    ax2.set_xticks([8, 10, 12, 14, 16])
    ax2.set_xlim(7.5, 16.5)  # a rare daemon-stall outlier sits beyond; the JSON keeps it
    ax2.set_xlabel("round-trip ms")
    ax2.set_title(f"Latency CDF — observation ops, binary vs text (N={payload['obs_reps']}/op)")

    for ax in (ax1, ax2):
        ax.set_ylabel("fraction of calls ≤ x")
        ax.set_ylim(0, 1.02)
        ax.legend(fontsize=8, loc="lower right")
    save_plot(fig, "action_latency")


def main() -> int:
    payload = run()
    summary = []
    for op in _ops(payload["datapoints"]):
        rows = [p for p in payload["datapoints"] if p["op"] == op]
        entry: dict = {"op": op, "ms": summarize([p["ms"] for p in rows])}
        payloads = [float(p["payload_bytes"]) for p in rows if p.get("payload_bytes")]
        wires = [float(p["wire_bytes"]) for p in rows if p.get("wire_bytes")]
        if payloads:
            entry["payload_bytes"] = summarize(payloads)
        if wires:
            entry["wire_bytes"] = summarize(wires)
        summary.append(entry)
    payload["summary"] = summary
    write_result("action_latency", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
