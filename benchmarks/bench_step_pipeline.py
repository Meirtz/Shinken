"""S11 — pipelined agent step vs the sequential facade under emulated WAN RTT.

A k-action agent step plus one observation costs k+1 serial round-trips through the
plain sync facade (`act()` × k, then a screenshot) but ~1 round-trip through
`Sandbox.step()` — pipelined dispatch + fused observation
(docs/engineering/many-sandbox-concurrency.md §5). This suite measures both against
ONE local Docker sandbox reached through an asyncio TCP delay proxy
(`benchmarks/_wan_proxy.py` — macOS has no netem) at added RTT
∈ {0 (loopback), 50, 150, 300} ms, step shapes k ∈ {3, 5, 8} actions
(+observe JPEG q80 @1024), N=30 steps per cell.

The proxy is validated in-run: every tier records ACI `ping` RTT through the proxy
(nominal vs measured) before any step timing, so the emulation itself is auditable
in the results JSON.

Emits benchmarks/results/step_pipeline.json and docs/assets/bench/step_pipeline.png
(`benchmarks/replot.py` regenerates the figure from the tracked JSON without Docker).

Run:  python benchmarks/bench_step_pipeline.py
"""

from __future__ import annotations

import statistics
import sys

from _common import (
    boot,
    fill_xterm,
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)
from _wan_proxy import DelayProxy

RTT_TIERS_MS = [0.0, 50.0, 150.0, 300.0]  # added RTT; proxy one-way delay = RTT/2
STEP_SHAPES = [3, 5, 8]  # actions per step (observation rides on top)
STEPS_PER_CELL = 30
PING_REPS = 15
OBSERVE = {"format": "jpeg", "quality": 80, "max_long_edge": 1024}


def make_actions(k: int) -> list[dict]:
    """k input-plane actions: a focus click, then alternating key/move — the realistic
    multi-action step shape (position, interact, confirm) without changing screen
    content enough to skew the trailing JPEG observation between modes."""
    acts: list[dict] = [
        {"verb": "click", "target": {"kind": "point_px", "x": 120, "y": 120}}
    ]
    while len(acts) < k:
        acts.append({"verb": "key", "keys": "space"})
        if len(acts) < k:
            x = 200 + 7 * len(acts)
            acts.append(
                {"verb": "move", "target": {"kind": "point_px", "x": x, "y": 300}}
            )
    return acts[:k]


def _sequential_step(env, actions: list[dict]) -> None:
    """The k+1-round-trip baseline: each act() awaits its ack, then one observation."""
    for a in actions:
        rest = {k: v for k, v in a.items() if k not in ("verb", "target")}
        env.act(a["verb"], a.get("target"), **rest)
    reply = env.act("screenshot", **OBSERVE)
    assert reply.get("type") == "observation"


def _pipelined_step(env, actions: list[dict]) -> None:
    """The ~1-round-trip path: every action + the observation in one exchange."""
    res = env.step(actions, observe=OBSERVE)
    assert res["failure_kind"] is None and res["observation"] is not None


def run() -> dict:
    import shinken

    provider, handle, env0 = boot()
    points: list[dict] = []
    tiers: list[dict] = []
    try:
        fill_xterm(env0)  # realistic frame content for the JPEG observation
        env0.close()  # each tier owns its own session (direct or through the proxy)
        for rtt in RTT_TIERS_MS:
            proxy = None
            addr = handle.addr
            if rtt > 0:
                host, port = handle.addr.rsplit(":", 1)
                proxy = DelayProxy(host, int(port), delay_ms=rtt / 2.0)
                addr = proxy.start_in_thread()
            env = shinken.connect(addr, token=handle.token)
            try:
                # Validate the emulation: measured ping RTT vs the nominal added RTT.
                pings = [env.ping() * 1000.0 for _ in range(PING_REPS)]
                tiers.append({"rtt_nominal_ms": rtt, "ping_ms": summarize(pings)})
                print(
                    f"tier +{rtt:.0f} ms RTT: measured ping p50 "
                    f"{statistics.median(pings):.1f} ms",
                    flush=True,
                )
                for k in STEP_SHAPES:
                    actions = make_actions(k)
                    for mode, fn in (
                        ("sequential", _sequential_step),
                        ("pipelined", _pipelined_step),
                    ):
                        for rep in range(STEPS_PER_CELL):
                            t0 = now_ms()
                            fn(env, actions)
                            points.append(
                                {
                                    "rtt_ms": rtt,
                                    "k": k,
                                    "mode": mode,
                                    "rep": rep,
                                    "ms": round(now_ms() - t0, 3),
                                }
                            )
                    print(f"  k={k}: done ({2 * STEPS_PER_CELL} steps)", flush=True)
            finally:
                env.close()
                if proxy is not None:
                    proxy.stop()
    finally:
        provider.destroy(handle)
    return {
        "steps_per_cell": STEPS_PER_CELL,
        "rtt_tiers_ms": RTT_TIERS_MS,
        "step_shapes": STEP_SHAPES,
        "observe": OBSERVE,
        "tiers": tiers,
        "datapoints": points,
    }


def _cell(points: list[dict], rtt: float, k: int, mode: str) -> list[float]:
    return [
        p["ms"]
        for p in points
        if p["rtt_ms"] == rtt and p["k"] == k and p["mode"] == mode
    ]


def summarize_cells(payload: dict) -> list[dict]:
    out = []
    for rtt in payload["rtt_tiers_ms"]:
        for k in payload["step_shapes"]:
            for mode in ("sequential", "pipelined"):
                s = summarize(_cell(payload["datapoints"], rtt, k, mode))
                out.append(
                    {
                        "rtt_ms": rtt,
                        "k": k,
                        "mode": mode,
                        "ms": s,
                        "steps_per_s": round(1000.0 / s["p50"], 2) if s else None,
                    }
                )
    return out


def plot(payload: dict) -> None:
    points = payload["datapoints"]
    shapes = payload["step_shapes"]
    rtts = payload["rtt_tiers_ms"]
    fig, (ax1, ax2) = new_axes(2)
    colors = {3: "#2980b9", 5: "#27ae60", 8: "#8e44ad"}

    def p50(rtt, k, mode):
        vals = sorted(_cell(points, rtt, k, mode))
        return vals[len(vals) // 2] if vals else None

    for k in shapes:
        c = colors.get(k, "#555")
        ax1.plot(
            rtts,
            [p50(r, k, "sequential") for r in rtts],
            "--o",
            ms=4,
            color=c,
            label=f"sequential k={k} ({k + 1} RTT)",
        )
        ax1.plot(
            rtts,
            [p50(r, k, "pipelined") for r in rtts],
            "-o",
            ms=4,
            color=c,
            label=f"step() k={k} (~1 RTT)",
        )
    ax1.set_xlabel("added WAN RTT (ms, delay proxy)")
    ax1.set_ylabel("step wall-clock p50 (ms)")
    ax1.set_title(
        f"k actions + observe JPEG q80@1024 (N={payload['steps_per_cell']}/cell)"
    )
    ax1.legend(fontsize=7, loc="upper left")

    for k in shapes:
        c = colors.get(k, "#555")
        speedups = [
            (p50(r, k, "sequential") / p50(r, k, "pipelined"))
            if p50(r, k, "pipelined")
            else None
            for r in rtts
        ]
        ax2.plot(rtts, speedups, "-o", ms=4, color=c, label=f"k={k}")
        ax2.axhline(k + 1, ls=":", lw=0.9, color=c, alpha=0.7)
    ax2.set_xlabel("added WAN RTT (ms, delay proxy)")
    ax2.set_ylabel("speedup: sequential p50 / step() p50")
    ax2.set_title("Convergence to the (k+1)$\\times$ RTT bound (dotted)")
    ax2.legend(fontsize=8, loc="upper left")
    save_plot(fig, "step_pipeline")


def main() -> int:
    payload = run()
    payload["summary"] = summarize_cells(payload)
    write_result("step_pipeline", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
