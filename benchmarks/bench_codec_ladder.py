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


# Scene = hue (Okabe–Ito, colorblind-safe; no red/green comparison pair); codec is
# carried by linestyle everywhere (JPEG solid, PNG dashed + open markers).
_SCEN_HUES = {"desktop": "#0072B2", "dense-text": "#E69F00", "photo": "#CC79A7"}
_SCEN_MARKERS = {"desktop": "^", "dense-text": "o", "photo": "s"}
_SCEN_LABELS = {
    "desktop": "desktop (flat UI)",
    "dense-text": "dense text",
    "photo": "photo",
}


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
    """Two panels on ONE shared log-KiB scale: (1) bytes vs JPEG quality at the
    native 1280 px, all three scenes overlaid with their PNG baselines dashed;
    (2) the downscale ladder at JPEG q80 vs PNG. The suptitle states the measured
    takeaway, with every number computed from the payload. Per-rep latency stays
    in the JSON (the action-latency suite carries the latency story)."""
    from matplotlib.lines import Line2D

    points = payload["datapoints"]
    qualities = payload["qualities"]
    scenarios = payload["scenarios"]
    ladder = sorted((s or 1280, s) for s in payload["scales"])
    xs_px = [px for px, _ in ladder]

    fig, (ax_q, ax_d) = new_axes(2, width=5.7, height=4.5)
    all_kib: list[float] = []

    # Panel 1 — bytes vs JPEG quality at native resolution, PNG baselines dashed.
    for scen in scenarios:
        hue = _SCEN_HUES[scen]
        stats = [_band(points, scen, "jpeg", q, None) for q in qualities]
        means = [s[0] for s in stats]
        ax_q.plot(
            qualities, means, marker=_SCEN_MARKERS[scen], ms=5.5, lw=2.0, color=hue
        )
        ax_q.fill_between(
            qualities,
            [s[1] for s in stats],
            [s[2] for s in stats],
            color=hue,
            alpha=0.18,
            lw=0,
        )
        png = _band(points, scen, "png", None, None)[0]
        ax_q.axhline(png, color=hue, ls="--", lw=1.8, alpha=0.9)
        all_kib += means + [png]

    # The headline gap, drawn where it happens: PNG vs JPEG q80 on the photo scene.
    png_photo = _band(points, "photo", "png", None, None)[0]
    j80_photo = _band(points, "photo", "jpeg", 80, None)[0]
    ax_q.annotate(
        "",
        xy=(80, j80_photo * 1.15),
        xytext=(80, png_photo * 0.87),
        arrowprops=dict(arrowstyle="<->", color=_SCEN_HUES["photo"], lw=1.6),
    )
    ax_q.text(
        78.5,
        j80_photo * 2.2,  # the clear band between the dense-text and photo curves
        f"{png_photo / j80_photo:.0f}$\\times$ at q80",
        color=_SCEN_HUES["photo"],
        ha="right",
        va="center",
        fontsize=12,
        fontweight="bold",
        bbox=dict(facecolor="white", alpha=0.8, edgecolor="none", pad=1.5),
    )

    # Panel 2 — the downscale ladder: JPEG q80 (solid) vs PNG (dashed, open markers).
    for scen in scenarios:
        hue = _SCEN_HUES[scen]
        for fmt, q, ls, mfc, lw in (
            ("png", None, "--", "white", 1.8),
            ("jpeg", 80, "-", None, 2.0),
        ):
            stats = [_band(points, scen, fmt, q, scale) for _, scale in ladder]
            means = [s[0] for s in stats]
            ax_d.errorbar(
                xs_px,
                means,
                yerr=[
                    [m - s[1] for m, s in zip(means, stats)],
                    [s[2] - m for m, s in zip(means, stats)],
                ],
                marker=_SCEN_MARKERS[scen],
                ms=5.5,
                lw=lw,
                ls=ls,
                color=hue,
                markerfacecolor=mfc or hue,
                capsize=2.5,
            )
            all_kib += means

    # One shared y-scale so the panels compare directly.
    for ax in (ax_q, ax_d):
        ax.set_yscale("log")
        _log_ticks(ax, [min(all_kib), max(all_kib)])
        ax.set_ylabel("KiB per frame (log)")
    ax_q.set_xlabel("JPEG quality")
    ax_q.set_title("Bytes vs JPEG quality — native 1280 px")
    ax_d.set_xticks(xs_px)
    ax_d.set_xlabel("max long edge (px)")
    ax_d.set_title("Downscale ladder — JPEG q80 vs PNG")

    # The compounded lever, where panel 2 shows it: photo PNG@1280 vs JPEG q80@512.
    j80_512_photo = _band(points, "photo", "jpeg", 80, 512)[0]
    ax_d.text(
        xs_px[-1],
        min(all_kib) * 1.05,
        f"photo, PNG 1280 px $\\to$ q80 512 px: {png_photo / j80_512_photo:.0f}$\\times$ smaller",
        color=_SCEN_HUES["photo"],
        ha="right",
        va="bottom",
        fontsize=12,
        fontweight="bold",
    )

    # Takeaway suptitle — every figure computed from the tracked payload.
    j80_dense = _band(points, "dense-text", "jpeg", 80, None)[0]
    png_dense = _band(points, "dense-text", "png", None, None)[0]
    levers = [
        _band(points, s, "jpeg", 80, None)[0] / _band(points, s, "jpeg", 80, 512)[0]
        for s in scenarios
    ]
    lever = sum(levers) / len(levers)
    fig.suptitle(
        f"JPEG q80 cuts the photo frame {png_photo / j80_photo:.0f}$\\times$ vs PNG but only "
        f"{png_dense / j80_dense:.1f}$\\times$ on dense text (PNG wins the flat desktop);\n"
        f"downscaling 1280 $\\to$ 512 px is a uniform $\\sim${lever:.1f}$\\times$ lever on every scene",
        y=1.10,
    )

    # One figure-level legend below both panels: scene = hue, codec = linestyle.
    handles = [
        Line2D(
            [],
            [],
            color=_SCEN_HUES[s],
            marker=_SCEN_MARKERS[s],
            ms=6,
            lw=2.0,
            label=_SCEN_LABELS[s],
        )
        for s in scenarios
    ] + [
        Line2D([], [], color="0.3", lw=2.0, label="JPEG (solid)"),
        Line2D([], [], color="0.3", lw=1.8, ls="--", label="PNG, lossless (dashed)"),
    ]
    fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.0),
        ncols=5,
        frameon=False,
        columnspacing=1.6,
    )
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
