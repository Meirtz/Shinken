"""S1 — observation codec ladder, measured densely on the LOCAL Docker sandbox.

The remote-WAN B1 ladder (docs/engineering/streaming-bandwidth.md §1) sampled five
codec/scale points on one frame. This suite is the dense local counterpart: a full
``format × quality × max_long_edge`` sweep with repetitions, over TWO content
scenarios on the 1280x800 Xvfb desktop —

- ``desktop``    — the image's default desktop: one 80x24 xterm filled with text on
                   a flat background (~15% of pixels carry content; the
                   PNG-favorable case), and
- ``dense-text`` — a near-fullscreen xterm filled with ANSI-colored text (~95%
                   pixel coverage; the heavy-UI case).

Latency is loopback round-trip through the sync SDK (capture + encode +
base64/JSON framing + decode) — bytes are substrate-independent, latency is
local-only. Emits benchmarks/results/codec_ladder.json and
docs/engineering/assets/benchmarks/codec_ladder.png.

Run:  python benchmarks/bench_codec_ladder.py
"""

from __future__ import annotations

import base64
import sys
import time

from _common import boot, fill_xterm, new_axes, now_ms, save_plot, summarize, write_result

QUALITIES = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95]
SCALES = [None, 1024, 768, 512]  # max_long_edge; None = native 1280
REPS = 5

_DENSE_CMD = (
    "xterm -geometry 209x61+0+0 -e bash -c "
    "'for i in $(seq 1 8000); do "
    'printf "\\e[3%dm%05d lorem ipsum dolor sit amet consectetur %05d \\e[0m" '
    "$((i%8)) $i $RANDOM; done; exec sleep 600' &"
)


def _setup_desktop(env) -> None:
    fill_xterm(env)


def _setup_dense(env) -> None:
    """Spawn a near-fullscreen xterm (209x61 chars ~ 1254x793 px) filled with
    ANSI-colored text — the dense-content operating point."""
    env.click(x=120, y=120)
    env.type_text(_DENSE_CMD)
    env.key("Return")
    time.sleep(4.0)  # let 8000 colored words paint


SCENARIOS = [("desktop", _setup_desktop), ("dense-text", _setup_dense)]


def _shot(env, *, format: str | None, quality: int | None, max_long_edge: int | None) -> dict:
    """One screenshot through the raw act path (the SDK facade does not yet expose
    ``max_long_edge`` on screenshot; shinkend accepts it — same clamp as screencast)."""
    t0 = now_ms()
    reply = env.act(
        "screenshot", scope="screen", format=format, quality=quality, max_long_edge=max_long_edge
    )
    ms = now_ms() - t0
    img = reply.get("image") or {}
    raw = base64.b64decode(img.get("ref", ""))
    return {
        "format": img.get("format", "png"),
        "quality": quality,
        "max_long_edge": max_long_edge,
        "bytes": len(raw),
        "ms": round(ms, 3),
        "w": img.get("w"),
        "h": img.get("h"),
    }


def run() -> dict:
    points: list[dict] = []
    for scenario, setup in SCENARIOS:
        provider, handle, env = boot()
        try:
            setup(env)
            for scale in SCALES:
                for fmt, q in [(None, None)] + [("jpeg", q) for q in QUALITIES]:
                    for rep in range(REPS):
                        points.append(
                            {
                                "scenario": scenario,
                                **_shot(env, format=fmt, quality=q, max_long_edge=scale),
                                "rep": rep,
                            }
                        )
                print(f"{scenario} scale={scale or 'native'}: {len(points)} datapoints", flush=True)
        finally:
            env.close()
            provider.destroy(handle)
    return {
        "scenarios": [s for s, _ in SCENARIOS],
        "qualities": QUALITIES,
        "scales": SCALES,
        "reps": REPS,
        "datapoints": points,
    }


def _cell(points: list[dict], scen: str, fmt: str, q: int | None, scale: int | None) -> list[dict]:
    return [
        p
        for p in points
        if p["scenario"] == scen
        and p["format"] == fmt
        and p["quality"] == q
        and p["max_long_edge"] == scale
    ]


def plot(payload: dict) -> None:
    points = payload["datapoints"]
    fig, (ax1, ax2, ax3) = new_axes(3, width=5.4)
    colors = {None: "C0", 1024: "C1", 768: "C2", 512: "C3"}

    for ax, scen in ((ax1, "dense-text"), (ax2, "desktop")):
        for scale in payload["scales"]:
            label = f"long edge {scale or 1280}"
            means = []
            for q in payload["qualities"]:
                cell = _cell(points, scen, "jpeg", q, scale)
                means.append(sum(p["bytes"] for p in cell) / max(1, len(cell)) / 1024.0)
            ax.plot(payload["qualities"], means, "o-", ms=3.5, color=colors[scale], label=label)
            png = [p["bytes"] / 1024.0 for p in _cell(points, scen, "png", None, scale)]
            if png:
                ax.axhline(
                    sum(png) / len(png),
                    color=colors[scale],
                    linestyle="--",
                    linewidth=0.9,
                    alpha=0.6,
                )
        ax.set_yscale("log")
        ax.set_xlabel("JPEG quality")
        ax.set_ylabel("KiB / frame (log)")
        ax.set_title(f"bytes vs quality — {scen}\n(dashed = PNG baseline at same scale)")
        ax.legend(fontsize=7)

    markers = {"dense-text": "o", "desktop": "^"}
    for scen in payload["scenarios"]:
        xs = [p["bytes"] / 1024.0 for p in points if p["scenario"] == scen]
        ys = [p["ms"] for p in points if p["scenario"] == scen]
        ax3.scatter(xs, ys, s=8, alpha=0.45, marker=markers[scen], label=scen)
    ax3.set_xscale("log")
    ax3.set_yscale("log")
    ax3.set_xlabel("KiB / frame (log)")
    ax3.set_ylabel("round-trip ms (log, loopback)")
    ax3.set_title("Latency vs payload size (all cells)")
    ax3.legend(fontsize=7)
    save_plot(fig, "codec_ladder")


def main() -> int:
    payload = run()
    cells = []
    for scen in payload["scenarios"]:
        for scale in payload["scales"]:
            for fmt, q in [("png", None)] + [("jpeg", q) for q in payload["qualities"]]:
                cell = _cell(payload["datapoints"], scen, fmt, q, scale)
                if cell:
                    cells.append(
                        {
                            "scenario": scen,
                            "format": fmt,
                            "quality": q,
                            "max_long_edge": scale,
                            "bytes": summarize([p["bytes"] for p in cell]),
                            "ms": summarize([p["ms"] for p in cell]),
                        }
                    )
    payload["cells"] = cells
    write_result("codec_ladder", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
