"""S1 — observation codec ladder, measured densely on the LOCAL Docker sandbox.

The remote-WAN B1 ladder (docs/engineering/streaming-bandwidth.md §1) sampled five
codec/scale points on one frame. This suite is the dense local counterpart: a full
``format × quality × max_long_edge`` sweep with repetitions, over THREE content
scenarios on the 1280x800 Xvfb desktop —

- ``desktop``    — the image's default desktop: one 80x24 xterm filled with text on
                   a flat background (~15% of pixels carry content; the
                   PNG-favorable case),
- ``dense-text`` — a near-fullscreen xterm filled with ANSI-colored text (~95%
                   pixel coverage; the heavy-UI case), and
- ``photo``      — a procedurally generated photographic frame painted across 100%
                   of the screen (deterministic seed, no binary asset — see
                   ``_common.synth_photo_ppm``; the JPEG-favorable,
                   content-rich-desktop case that B1 measured remotely).

Latency is loopback round-trip through the sync SDK (capture + encode +
base64/JSON framing + decode) — bytes are substrate-independent, latency is
local-only. Emits benchmarks/results/codec_ladder.json and
docs/assets/bench/codec_ladder.png.

Run:  python benchmarks/bench_codec_ladder.py
"""

from __future__ import annotations

import sys
import time

from _common import (
    image_bytes,
    PALETTE,
    boot,
    fill_xterm,
    new_axes,
    now_ms,
    save_plot,
    show_photo,
    summarize,
    write_result,
)

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


def _setup_photo(env) -> None:
    """Generate the deterministic photographic frame, push it into the guest, and
    paint it across the whole screen — verified via screenshot before measuring."""
    show_photo(env)


SCENARIOS = [
    ("desktop", _setup_desktop),
    ("dense-text", _setup_dense),
    ("photo", _setup_photo),
]


def _shot(
    env, *, format: str | None, quality: int | None, max_long_edge: int | None
) -> dict:
    """One screenshot through the raw act path (the SDK facade does not yet expose
    ``max_long_edge`` on screenshot; shinkend accepts it — same clamp as screencast)."""
    t0 = now_ms()
    reply = env.act(
        "screenshot",
        scope="screen",
        format=format,
        quality=quality,
        max_long_edge=max_long_edge,
    )
    ms = now_ms() - t0
    img = reply.get("image") or {}
    raw = image_bytes(img)
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
                                **_shot(
                                    env, format=fmt, quality=q, max_long_edge=scale
                                ),
                                "rep": rep,
                            }
                        )
                print(
                    f"{scenario} scale={scale or 'native'}: {len(points)} datapoints",
                    flush=True,
                )
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


def _cell(
    points: list[dict], scen: str, fmt: str, q: int | None, scale: int | None
) -> list[dict]:
    return [
        p
        for p in points
        if p["scenario"] == scen
        and p["format"] == fmt
        and p["quality"] == q
        and p["max_long_edge"] == scale
    ]


# JPEG = blue family (downscale by shade), PNG = red family — semantics from PALETTE.
_JPEG_SHADES = {None: "#1b4f72", 1024: PALETTE["jpeg"], 768: "#5dade2", 512: "#85c1e9"}
_PNG_SHADES = {None: "#78281f", 1024: PALETTE["png"], 768: "#e74c3c", 512: "#f1948a"}
_SCALE_MARKERS = {None: "o", 1024: "s", 768: "^", 512: "D"}
_SCEN_LS = {"dense-text": "-", "desktop": "--", "photo": ":"}
_SCEN_MARKERS = {"dense-text": "o", "desktop": "^", "photo": "s"}


def _log_ticks(
    ax, values: list[float], axis: str = "y", candidates: list[int] | None = None
) -> list[int]:
    """FixedLocator + ScalarFormatter on a log axis: plain-number ticks chosen to
    bracket the plotted data (no bare 4x10^-1-style mid-decade gaps), with the axis
    limits widened so the bracketing ticks are actually visible."""
    from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter

    candidates = candidates or [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
    lo, hi = min(values), max(values)
    below = [c for c in candidates if c <= lo]
    above = [c for c in candidates if c >= hi]
    ticks = [
        c
        for c in candidates
        if (below[-1] if below else candidates[0])
        <= c
        <= (above[0] if above else candidates[-1])
    ]
    axis_obj = ax.yaxis if axis == "y" else ax.xaxis
    axis_obj.set_major_locator(FixedLocator(ticks))
    fmt = ScalarFormatter()
    fmt.set_scientific(False)
    axis_obj.set_major_formatter(fmt)
    axis_obj.set_minor_locator(NullLocator())
    (ax.set_ylim if axis == "y" else ax.set_xlim)(ticks[0] * 0.9, ticks[-1] * 1.12)
    return ticks


def _band(points: list[dict], scen: str, fmt: str, q: int | None, scale: int | None):
    """(mean, min, max) of KiB across the reps of one cell, or None if empty."""
    vals = [p["bytes"] / 1024.0 for p in _cell(points, scen, fmt, q, scale)]
    if not vals:
        return None
    return sum(vals) / len(vals), min(vals), max(vals)


def plot(payload: dict) -> None:
    """2x2: one bytes-vs-quality panel per scenario (min-max band over reps, PNG
    baselines dashed), plus the downscale ladder. Per-rep latency stays in the JSON
    (the action-latency suite carries the latency story)."""
    from matplotlib.lines import Line2D

    points = payload["datapoints"]
    qualities = payload["qualities"]
    scenarios = payload["scenarios"]
    fig, axes = new_axes(2, nrows=2)
    flat = [ax for row in axes for ax in row]
    scen_axes = flat[: len(scenarios)]
    ax_d = flat[len(scenarios)] if len(flat) > len(scenarios) else flat[-1]

    for ax, scen in zip(scen_axes, scenarios):
        ymin, ymax = [], []
        for scale in payload["scales"]:
            stats = [_band(points, scen, "jpeg", q, scale) for q in qualities]
            means = [s[0] for s in stats]
            ax.plot(
                qualities,
                means,
                marker=_SCALE_MARKERS[scale],
                ms=4.5,
                lw=1.6,
                color=_JPEG_SHADES[scale],
                label=f"JPEG {scale or 1280} px",
            )
            ax.fill_between(
                qualities,
                [s[1] for s in stats],
                [s[2] for s in stats],
                color=_JPEG_SHADES[scale],
                alpha=0.18,
                lw=0,
            )
            png = _band(points, scen, "png", None, scale)
            ax.axhline(png[0], color=_PNG_SHADES[scale], ls="--", lw=1.3, alpha=0.85)
            ymin.append(min(means + [png[0]]))
            ymax.append(max(means + [png[0]]))
        ax.set_yscale("log")
        ticks = _log_ticks(ax, [min(ymin), max(ymax)])
        ax.set_ylim(
            top=ticks[-1] * 1.35
        )  # free a top band so the legend covers no data
        ax.set_xlabel("JPEG quality")
        ax.set_ylabel("KiB / frame (log)")
        ax.set_title(f"Bytes vs JPEG quality — {scen}")
        handles, labels = ax.get_legend_handles_labels()
        handles.append(Line2D([], [], color=PALETTE["png"], ls="--", lw=1.3))
        labels.append("PNG (same scale)")
        ax.legend(
            handles,
            labels,
            loc="upper left",
            ncols=3,
            columnspacing=1.0,
            handlelength=1.6,
        )

    # last panel — the downscale ladder at fixed JPEG q80 vs the PNG baseline.
    ladder = sorted((s or 1280, s) for s in payload["scales"])
    xs = [px for px, _ in ladder]
    dmin, dmax = [], []
    for fmt, q, color in (("png", None, PALETTE["png"]), ("jpeg", 80, PALETTE["jpeg"])):
        for scen in scenarios:
            stats = [_band(points, scen, fmt, q, scale) for _, scale in ladder]
            means = [s[0] for s in stats]
            ax_d.errorbar(
                xs,
                means,
                yerr=[
                    [m - s[1] for m, s in zip(means, stats)],
                    [s[2] - m for m, s in zip(means, stats)],
                ],
                marker=_SCEN_MARKERS[scen],
                ms=5,
                lw=1.6,
                ls=_SCEN_LS[scen],
                color=color,
                capsize=3,
                label=f"{fmt.upper()}{f' q{q}' if q else ''} — {scen}",
            )
            dmin.append(min(means))
            dmax.append(max(means))
    ax_d.set_yscale("log")
    _log_ticks(ax_d, [min(dmin), max(dmax)])
    ax_d.set_xticks(xs)
    ax_d.set_xlabel("max long edge (px)")
    ax_d.set_ylabel("KiB / frame (log)")
    ax_d.set_title("Downscale ladder — PNG vs JPEG q80")
    ax_d.legend(loc="upper left", fontsize=8, ncols=2)
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
