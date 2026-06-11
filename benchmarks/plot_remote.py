#!/usr/bin/env python3
"""Remote-WAN, projection, and a11y-spike figures (NOT a rerunnable suite).

Unlike the ``bench_*.py`` suites (local, rerunnable, JSON-backed, driven via
``replot.py``), the figures here come from a ONE-OFF remote-substrate measurement
taken over an intercontinental WAN (~0.28 s RTT) against a generic remote sandbox
(vendor-neutral; the substrate is not identified). The raw CSVs are tracked in
``benchmarks/results/remote/`` (``codec_ladder.csv``, ``fanout_remote.csv``) so the
numbers stay auditable, but the driving harness is not published. The
aggregate-egress figure is a PROJECTION from those measured frame sizes, anchored
by one measured datapoint from ``benchmarks/results/client_scale.json``; the a11y
figure reads ``spikes/a11y-coverage/evidence.json``.

Regenerate (no Docker, no network)::

    cd benchmarks && python3 plot_remote.py
"""
from __future__ import annotations

import csv
import json

from matplotlib.colors import to_rgb
from matplotlib.ticker import FixedLocator, NullFormatter, NullLocator, ScalarFormatter

from _common import style, PALETTE, REPO_ROOT, RESULTS_DIR, new_axes, save_plot

REMOTE_DATA = RESULTS_DIR / "remote"


def _csv(name: str) -> list[dict]:
    with open(REMOTE_DATA / name) as f:
        return list(csv.DictReader(f))


def _shade(hex_color: str, f: float):
    """Lighten (f>0, toward white) or darken (f<0, toward black) a palette color,
    so a resolution ladder stays within one semantic codec hue."""
    r, g, b = to_rgb(hex_color)
    if f >= 0:
        return (r + (1 - r) * f, g + (1 - g) * f, b + (1 - b) * f)
    return (r * (1 + f), g * (1 + f), b * (1 + f))


def _log_yticks(ax, ticks: list[float]) -> None:
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(ScalarFormatter())
    ax.yaxis.set_minor_formatter(NullFormatter())


def remote_codec_ladder():
    rows = _csv("codec_ladder.csv")
    # darkest blue = full res, lightest = smallest downscale; one hue per codec.
    res_order = [("full", -0.35), ("1280", 0.0), ("960", 0.25), ("768", 0.45), ("512", 0.62)]
    fig, ax = new_axes()
    for res, f in res_order:
        jp = sorted(
            (int(r["quality"]), float(r["kib"]))
            for r in rows
            if r["format"] == "jpeg" and r["res"] == res
        )
        label = "JPEG full (1920)" if res == "full" else f"JPEG @{res}"
        ax.plot(
            [q for q, _ in jp],
            [k for _, k in jp],
            "-o",
            ms=4,
            color=_shade(PALETTE["jpeg"], f),
            label=label,
        )
    png_full = next(float(r["kib"]) for r in rows if r["format"] == "png" and r["res"] == "full")
    ax.axhline(png_full, ls="--", color=PALETTE["png"], lw=1.4, label=f"PNG full ({png_full:.0f} KiB)")
    ax.set_yscale("log")
    _log_yticks(ax, [10, 30, 100, 300, 1000, 1800])
    ax.set_xticks([30, 40, 50, 60, 70, 80, 90, 95])
    ax.set_xlabel("JPEG quality")
    ax.set_ylabel("bytes per frame (KiB, log)")
    ax.set_title("Remote WAN codec ladder — bytes vs JPEG quality × downscale")
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False)
    ax.text(
        0.98,
        0.82,
        "one content-rich 1080p frame, n=1/cell,\nintercontinental WAN (~0.28 s RTT)",
        transform=ax.transAxes,
        fontsize=8,
        color=PALETTE["neutral"],
        ha="right",
        va="top",
    )
    save_plot(fig, "remote_codec_ladder")


def bandwidth_bars():
    rows = _csv("codec_ladder.csv")

    def g(fmt, res, q=None):
        for r in rows:
            if r["format"] == fmt and r["res"] == res and (q is None or r["quality"] == str(q)):
                return float(r["kib"])
        return 0.0

    labels = ["PNG\nfull", "JPEG q80\nfull", "JPEG q80\n@1280", "JPEG q80\n@768"]
    vals = [g("png", "full"), g("jpeg", "full", 80), g("jpeg", "1280", 80), g("jpeg", "768", 80)]
    base = vals[0]
    fig, ax = new_axes(height=4.0)
    bars = ax.bar(labels, vals, color=[PALETTE["png"]] + [PALETTE["jpeg"]] * 3)
    ax.set_yscale("log")
    ax.set_ylim(10, 5000)  # headroom so the value labels clear the title
    _log_yticks(ax, [10, 30, 100, 300, 1000, 3000])
    ax.set_ylabel("bytes per frame (KiB, log)")
    ax.set_title("Per-frame observation size — PNG vs JPEG (remote WAN)")
    for i, (b, v) in enumerate(zip(bars, vals)):
        tag = "baseline" if i == 0 else f"{base / v:.0f}× vs PNG"
        ax.text(b.get_x() + b.get_width() / 2, v * 1.12, f"{v:.1f} KiB\n{tag}", ha="center", fontsize=9)
    save_plot(fig, "bandwidth_bars")


def aggregate_projection():
    # Measured per-frame sizes: PNG full from the WAN codec ladder; JPEG q80 @1280
    # from the remote fan-out (per-sandbox observation bytes). N is extrapolated.
    png_kib = next(
        float(r["kib"]) for r in _csv("codec_ladder.csv") if r["format"] == "png" and r["res"] == "full"
    )
    jpeg_kib = float(_csv("fanout_remote.csv")[0]["kib_per_sandbox"])
    Ns = [16, 64, 256, 1024]

    def mbps(kib, n):
        return kib * 1024 * 8 * n / 1e6  # @1 Hz, decoded payload bytes

    fig, ax = new_axes()
    ax.plot(
        Ns,
        [mbps(png_kib, n) for n in Ns],
        ls="--",
        marker="o",
        mfc="none",
        color=PALETTE["png"],
        label=f"PNG full — {png_kib:.0f} KiB/frame",
    )
    ax.plot(
        Ns,
        [mbps(jpeg_kib, n) for n in Ns],
        ls="--",
        marker="o",
        mfc="none",
        color=PALETTE["jpeg"],
        label=f"JPEG q80 @1280 — {jpeg_kib:.0f} KiB/frame",
    )
    ax.axhline(1000, ls=":", color=PALETTE["neutral"], lw=1.2, label="1 Gbps")
    # One measured anchor: sustained client-plane decoded ingest at N=1024 from the
    # local client_scale suite (mock servers, 48 KiB frames).
    cs = json.loads((RESULTS_DIR / "client_scale.json").read_text())
    measured = cs["datapoints"]["sustained"]["sustained_decoded_mbps"]
    ax.plot(
        [1024],
        [measured],
        marker="*",
        ls="None",
        ms=15,
        color=PALETTE["accent"],
        label="measured client-plane ingest @1024\n(mock servers, 48 KiB frames)",
    )
    for n, v in ((n, mbps(jpeg_kib, n)) for n in Ns):
        ax.annotate(f"{v:.0f}", (n, v), textcoords="offset points", xytext=(0, 7), fontsize=8, ha="center")
    ax.annotate(
        "PNG crosses 1 Gbps\nnear N=64",
        xy=(67.7, 1000),
        xytext=(115, 230),
        fontsize=8.5,
        color=PALETTE["neutral"],
        arrowprops=dict(arrowstyle="->", color=PALETTE["neutral"], lw=0.8),
    )
    ax.set_xscale("log", base=2)
    ax.set_xlim(13, 1400)
    ax.set_xticks(Ns)
    ax.xaxis.set_major_formatter(ScalarFormatter())
    ax.xaxis.set_minor_locator(NullLocator())
    ax.set_yscale("log")
    _log_yticks(ax, [10, 100, 1000, 10000])
    ax.set_xlabel("concurrent sandboxes (N), 1 observation/s")
    ax.set_ylabel("aggregate egress (Mbps, log)")
    ax.set_title("Projected aggregate egress — N sandboxes × 1 Hz")
    ax.text(
        0.02,
        0.97,
        "frame sizes measured;\nN extrapolated",
        transform=ax.transAxes,
        fontsize=8.5,
        style="italic",
        color=PALETTE["neutral"],
        va="top",
    )
    ax.text(
        0.0,
        -0.18,
        "decoded payload bytes; base64+JSON wire framing adds ~33%",
        transform=ax.transAxes,
        fontsize=8,
        color=PALETTE["neutral"],
    )
    ax.legend(loc="center left", bbox_to_anchor=(1.01, 0.5), frameon=False)
    save_plot(fig, "aggregate_egress")


def a11y_coverage():
    ev = json.loads((REPO_ROOT / "spikes" / "a11y-coverage" / "evidence.json").read_text())
    apps = ev["atspi_coverage"]["per_app"]
    # xterm exposes a bare 1-node AT-SPI tree; fall back to 0.0 if the spike JSON
    # ever drops the entry.
    xterm = apps.get("xterm", {}).get("pct_addressable", 0.0)
    cdp = ev["cdp_coverage"]["coverage"]["pct_addressable"]
    electron_atspi = ev["electron"]["atspi"]["pct_addressable"]
    electron_cdp = ev["electron"]["cdp"]["coverage"]["pct_addressable"]
    canvas_cdp = ev["canvas"]["cdp"]["coverage"]["pct_addressable"]
    items = [
        ("calculator\n(Qt/AT-SPI)", apps["calculator"]["pct_addressable"]),
        ("electron app\n(AT-SPI, forced)", electron_atspi),
        ("chromium page\n(CDP)", cdp),
        ("electron page\n(CDP)", electron_cdp),
        ("zenity\n(GTK dialog)", apps["zenity"]["pct_addressable"]),
        ("gnome-text-\neditor (GTK4)", apps["gnome-text-editor"]["pct_addressable"]),
        ("xterm\n(X11 terminal)", xterm),
        ("canvas-UI page\n(CDP)", canvas_cdp),
    ]
    fig, ax = new_axes(height=4.0, width=8.8)
    # one neutral hue, shaded by coverage: the codec palette stays reserved for
    # codec semantics, and bar heights + labels carry the comparison
    colors = ["#34495e" if v >= 0.5 else ("#5d6d7e" if v >= 0.2 else "#aeb6bf") for _, v in items]
    bars = ax.bar([k for k, _ in items], [v for _, v in items], color=colors)
    for b, (name, v) in zip(bars, items):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}", ha="center", fontsize=9)
    # the canvas zero is a measurement, not a gap: 5 drawn controls, none in the tree
    ax.annotate(
        "5 interactive controls drawn\nin-canvas; tree = 2 nodes,\n0 actionable (measured)",
        xy=(6.85, 0.085),
        xytext=(5.85, 0.33),
        fontsize=8,
        color=PALETTE["neutral"],
        ha="center",
        arrowprops=dict(arrowstyle="->", color=PALETTE["neutral"], lw=0.8),
    )
    ax.tick_params(axis="x", labelsize=8.5)
    ax.set_ylabel("fraction addressable")
    ax.set_ylim(0, 1.0)
    ax.set_title("Accessibility-tree coverage by app surface")
    ax.text(
        0.98,
        0.95,
        "addressable = roled + bbox + actionable\n"
        "CDP page rows: 0.23 of all nodes but 1.00 of labeled controls\n"
        "one-off a11y-coverage spike (E5/#2), Linux/X11 image",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=8,
        color=PALETTE["neutral"],
    )
    save_plot(fig, "a11y_coverage")


def fork_ladder():
    """THE runtime-state figure: time to one USABLE replica of a mid-task state,
    per tier — each tier's p50 read from its own tracked suite JSON. Carried
    state grows; latency does not."""
    import json

    def p50(name, key):
        return json.loads((RESULTS_DIR / f"{name}.json").read_text())["summary"][key]["p50"]

    rows = [
        ("cold boot\n(no state)", p50("fork_resume", "cold_boot_total_ms"),
         "nothing — setup must replay", "0.45"),
        ("disk fork\n(docker commit)", p50("fork_resume", "fork_total_ms"),
         "files", PALETTE["jpeg"]),
        ("memory fork\n(CRIU restore)", p50("fork_resume_memory", "fork_total_ms"),
         "files + processes + heap", PALETTE["accent"]),
        ("warm-pool graft", p50("fork_resume_pool", "pool_graft_total_ms"),
         "files, onto a pre-booted base", PALETTE["delta"]),
    ]
    style()
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.6, 4.2))
    bars = ax.barh([r[0] for r in rows][::-1], [r[1] / 1000.0 for r in rows][::-1],
                   color=[r[3] for r in rows][::-1], height=0.62)
    ax.set_xscale("log")
    ax.set_xticks([0.1, 0.2, 0.5, 1.0])
    ax.set_xticklabels(["0.1 s", "0.2 s", "0.5 s", "1 s"])
    ax.set_xlim(0.08, 1.45)
    for bar, (_label, ms, carried, _c) in zip(bars, rows[::-1]):
        ax.text(bar.get_width() * 1.07, bar.get_y() + bar.get_height() / 2,
                f"{ms / 1000:.2f} s", va="center", fontweight="bold", fontsize=12)
        if bar.get_width() >= 0.35:  # room inside the bar
            ax.text(0.082, bar.get_y() + bar.get_height() / 2,
                    f"carries: {carried}", va="center", ha="left", fontsize=10,
                    style="italic", color="white")
        else:  # short bar: annotate to the right of the value label
            ax.text(bar.get_width() * 1.45, bar.get_y() + bar.get_height() / 2,
                    f"carries: {carried}", va="center", ha="left", fontsize=10,
                    style="italic", color="0.35")
    ax.set_xlabel("time to a usable replica of a mid-task state (p50, log scale)")
    ax.set_title("The fork ladder — every rung state-verified, the donor stays live")
    ax.text(0.99, 0.03,
            "checkpointing the LIVE sandbox: 0.53 s (disk) / 0.70 s (memory, donor keeps running)",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=10, color="0.35")
    save_plot(fig, "fork_ladder")


if __name__ == "__main__":
    remote_codec_ladder()
    fork_ladder()
    bandwidth_bars()
    aggregate_projection()
    a11y_coverage()
