"""S3 — ACI action/observation latency distributions on the local sandbox.

Per-operation round-trip latency through the synchronous SDK facade (so the
numbers are what a sync caller — an eval loop, a trainer step — actually pays:
guest capture/injection + shinkend encode + loopback WebSocket + SDK background-
loop hop). N repetitions per op, recorded individually, so the doc can show full
distributions (p50/p90/p99 + CDF), not just means.

Ops: ping, move, click, key, type_text (input plane); screenshot PNG native,
screenshot JPEG q80 native, screenshot JPEG q80 @1024 (observation plane).
Emits benchmarks/results/action_latency.json and
docs/engineering/assets/benchmarks/action_latency.png.

Run:  python benchmarks/bench_action_latency.py
"""

from __future__ import annotations

import random
import sys

from _common import boot, fill_xterm, new_axes, now_ms, save_plot, summarize, write_result

INPUT_REPS = 300
OBS_REPS = 150


def run() -> dict:
    provider, handle, env = boot()
    rng = random.Random(20260611)
    points: list[dict] = []

    def rec(op: str, rep: int, t0: float) -> None:
        points.append({"op": op, "rep": rep, "ms": round(now_ms() - t0, 3)})

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
            t0 = now_ms()
            shot = env.screenshot()
            rec("screenshot png", rep, t0)
            assert shot["format"] == "png"

            t0 = now_ms()
            shot = env.screenshot(format="jpeg", quality=80)
            rec("screenshot jpeg q80", rep, t0)
            assert shot["format"] == "jpeg"

            t0 = now_ms()
            env.act("screenshot", format="jpeg", quality=80, max_long_edge=1024)
            rec("screenshot jpeg q80 @1024", rep, t0)
        print(f"{len(points)} datapoints", flush=True)
    finally:
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
    points = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)
    input_ops = ["ping", "move", "click", "key", "type_text"]
    obs_ops = ["screenshot png", "screenshot jpeg q80", "screenshot jpeg q80 @1024"]

    for ax, ops, title in (
        (ax1, input_ops, f"Input-plane ops (N={payload['input_reps']} each)"),
        (ax2, obs_ops, f"Observation ops (N={payload['obs_reps']} each)"),
    ):
        for op in ops:
            xs = sorted(p["ms"] for p in points if p["op"] == op)
            if not xs:
                continue
            ys = [(i + 1) / len(xs) for i in range(len(xs))]
            ax.plot(xs, ys, linewidth=1.4, label=op)
        ax.set_xscale("log")
        ax.set_xlabel("round-trip ms (log)")
        ax.set_ylabel("fraction of calls ≤ x")
        ax.set_title(f"Latency CDF — {title}")
        ax.legend(fontsize=8, loc="lower right")
    save_plot(fig, "action_latency")


def main() -> int:
    payload = run()
    payload["summary"] = [
        {"op": op, "ms": summarize([p["ms"] for p in payload["datapoints"] if p["op"] == op])}
        for op in _ops(payload["datapoints"])
    ]
    write_result("action_latency", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
