#!/usr/bin/env python3
"""Generate the benchmark plots embedded in README + docs/benchmarks/README.md.

Reads the first-party CSVs in docs/benchmarks/data/ and spikes/a11y-coverage/evidence.json;
writes PNGs to docs/assets/bench/. Pure matplotlib, no seaborn. Reproduce:

    python docs/benchmarks/plots.py

All numbers are first-party (this repo's SDK/runtime). The remote-substrate datapoints were
taken over an intercontinental WAN against a generic remote sandbox; the substrate is not
identified (vendor-neutral). See docs/benchmarks/README.md for provenance.
"""
from __future__ import annotations

import csv
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = os.path.join(ROOT, "docs/benchmarks/data")
OUT = os.path.join(ROOT, "docs/assets/bench")
os.makedirs(OUT, exist_ok=True)
plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.grid": True, "grid.alpha": 0.3})

C = {"png": "#c0392b", "jpeg": "#2980b9", "shared": "#27ae60", "sync": "#c0392b", "accent": "#8e44ad"}


def _csv(name):
    with open(os.path.join(DATA, name)) as f:
        return list(csv.DictReader(f))


def codec_pareto():
    rows = _csv("codec_ladder.csv")
    res_order = ["full", "1280", "960", "768", "512"]
    fig, ax = plt.subplots(figsize=(7, 4.3))
    for res in res_order:
        jp = [(int(r["quality"]), float(r["kib"])) for r in rows if r["format"] == "jpeg" and r["res"] == res]
        jp.sort()
        ax.plot([q for q, _ in jp], [k for _, k in jp], "-o", ms=4, label=f"JPEG {res}")
    # PNG full baseline as a reference line
    png_full = next(float(r["kib"]) for r in rows if r["format"] == "png" and r["res"] == "full")
    ax.axhline(png_full, ls="--", color=C["png"], lw=1.4, label=f"PNG full ({png_full:.0f} KiB)")
    ax.set_yscale("log")
    ax.set_xlabel("JPEG quality")
    ax.set_ylabel("bytes per frame (KiB, log)")
    ax.set_title("Observation size: JPEG quality × downscale vs lossless PNG (1 live desktop frame)")
    ax.invert_xaxis()
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "codec_pareto.png"))
    plt.close(fig)


def bandwidth_bars():
    rows = _csv("codec_ladder.csv")

    def g(fmt, res, q=None):
        for r in rows:
            if r["format"] == fmt and r["res"] == res and (q is None or r["quality"] == str(q)):
                return float(r["kib"])
        return 0.0

    labels = ["PNG\nfull", "JPEG q80\nfull", "JPEG q80\n@1280", "JPEG q80\n@768", "delta-PNG\ntyping*"]
    vals = [g("png", "full"), g("jpeg", "full", 80), g("jpeg", "1280", 80), g("jpeg", "768", 80), 2.3]
    base = vals[0]
    fig, ax = plt.subplots(figsize=(7, 4.0))
    bars = ax.bar(labels, vals, color=[C["png"], C["jpeg"], C["jpeg"], C["jpeg"], C["shared"]])
    ax.set_yscale("log")
    ax.set_ylabel("bytes per frame (KiB, log)")
    ax.set_title("Per-frame bandwidth: lossless → lossy → lossless dirty-tile delta")
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v * 1.08, f"{v:.1f}\n{base / v:.0f}×", ha="center", fontsize=8)
    ax.text(0.99, -0.18, "*delta-PNG = changed-tile stream while typing in a terminal (separate scenario)",
            transform=ax.transAxes, ha="right", fontsize=7, color="#555")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "bandwidth_bars.png"))
    plt.close(fig)


def concurrency():
    rows = _csv("concurrency.csv")
    sh = sorted([r for r in rows if r["mode"] == "shared_loop"], key=lambda r: int(r["n"]))
    sy = sorted([r for r in rows if r["mode"] == "sync_facade"], key=lambda r: int(r["n"]))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4.0))
    a1.plot([int(r["n"]) for r in sy], [int(r["loop_threads"]) for r in sy], "-o", color=C["sync"], label="sync facade (1 thread/session)")
    a1.plot([int(r["n"]) for r in sh], [int(r["loop_threads"]) for r in sh], "-o", color=C["shared"], label="SharedLoop (1 thread total)")
    a1.set_xscale("log", base=2)
    a1.set_xlabel("sandboxes in one process (N)")
    a1.set_ylabel("event-loop OS threads")
    a1.set_title("Threads vs N")
    a1.legend(fontsize=8)
    a2.plot([int(r["n"]) for r in sh], [float(r["rss_mb"]) for r in sh], "-o", color=C["shared"])
    a2.set_xscale("log", base=2)
    a2.set_xlabel("sandboxes in one process (N)")
    a2.set_ylabel("process RSS (MiB)")
    a2.set_title("SharedLoop memory vs N (mock servers, client-side only)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "concurrency.png"))
    plt.close(fig)


def aggregate_projection():
    # Per-sandbox observation bytes from the remote fan-out (JPEG q80 @1280 ≈ 48 KiB) vs PNG full.
    Ns = [16, 64, 256, 1024]
    jpeg_kib, png_kib = 48.2, 1804.5

    def mbps(kib):
        return [kib * 1024 * n * 8 / 1e6 for n in Ns]  # @1 Hz

    fig, ax = plt.subplots(figsize=(7, 4.0))
    ax.plot(Ns, mbps(png_kib), "-o", color=C["png"], label="PNG full (1804 KiB/frame)")
    ax.plot(Ns, mbps(jpeg_kib), "-o", color=C["jpeg"], label="JPEG q80 @1280 (48 KiB/frame)")
    ax.axhline(1000, ls=":", color="#555", label="1 Gbps")
    ax.set_xscale("log", base=2)
    ax.set_yscale("log")
    ax.set_xlabel("concurrent sandboxes, 1 observation/s")
    ax.set_ylabel("aggregate egress (Mbps, log)")
    ax.set_title("Why the codec matters at scale: aggregate observation egress")
    for n, v in zip(Ns, mbps(jpeg_kib)):
        ax.annotate(f"{v:.0f}", (n, v), textcoords="offset points", xytext=(0, 6), fontsize=7, ha="center")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "aggregate_egress.png"))
    plt.close(fig)


def a11y_coverage():
    ev = json.load(open(os.path.join(ROOT, "spikes/a11y-coverage/evidence.json")))
    apps = ev["atspi_coverage"]["per_app"]
    items = [("calculator\n(Qt/AT-SPI)", apps["calculator"]["pct_addressable"]),
             ("chromium page\n(CDP)", ev["cdp_coverage"]["coverage"]["pct_addressable"]),
             ("zenity\n(GTK dialog)", apps["zenity"]["pct_addressable"]),
             ("gnome-text-editor\n(GTK4)", apps["gnome-text-editor"]["pct_addressable"]),
             ("xterm\n(X11 terminal)", 0.0)]
    fig, ax = plt.subplots(figsize=(7, 4.0))
    colors = [C["shared"] if v >= 0.5 else (C["jpeg"] if v >= 0.2 else C["png"]) for _, v in items]
    bars = ax.bar([k for k, _ in items], [v for _, v in items], color=colors)
    for b, (_, v) in zip(bars, items):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}", ha="center", fontsize=8)
    ax.set_ylabel("fraction addressable (roled + bbox + actionable)")
    ax.set_ylim(0, 1.0)
    ax.set_title("Accessibility-tree coverage by surface (E5/#2) — why D3 default stays hybrid")
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "a11y_coverage.png"))
    plt.close(fig)


if __name__ == "__main__":
    codec_pareto()
    bandwidth_bars()
    concurrency()
    aggregate_projection()
    a11y_coverage()
    print("wrote plots to", OUT)
    for f in sorted(os.listdir(OUT)):
        print(" ", f)
