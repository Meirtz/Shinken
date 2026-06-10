"""S2 — dirty-tile delta screencast (B2) under a live typing workload, per-frame.

The B2 table in docs/engineering/streaming-bandwidth.md §2 reported per-mode means.
This suite records EVERY delivered frame (seq, payload bytes, keyframe-vs-tiles,
tile count, arrival time) for four modes — full-PNG, full-JPEG q80, delta-PNG,
delta-JPEG q80 — under the same workload: typing into the xterm at ~12 chars/s,
fps=10, up to FRAMES frames per mode. A separate idle window per mode measures
what an unchanged desktop costs (idle suppression + the initial keyframe).

Payload bytes are the decoded image/tile bytes (base64+JSON wire framing adds
~33%). Emits benchmarks/results/delta_screencast.json and
docs/assets/bench/delta_screencast.png.

Run:  python benchmarks/bench_delta_screencast.py
"""

from __future__ import annotations

import sys
import threading
import time

from _common import PALETTE, boot, new_axes, save_plot, summarize, write_result

MODES: list[tuple[str, dict]] = [
    ("full-png", {}),
    ("full-jpeg-q80", {"format": "jpeg", "quality": 80}),
    ("delta-png", {"delta": True}),
    ("delta-jpeg-q80", {"delta": True, "format": "jpeg", "quality": 80}),
]
FPS = 10.0
FRAMES = 80  # ~8 s typing window at fps=10
TYPE_INTERVAL_S = 0.08  # ~12 chars/s
IDLE_WINDOW_S = 3.0
_TEXT = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "


def _typist(env, stop: threading.Event) -> None:
    i = 0
    while not stop.is_set():
        env.type_text(_TEXT[i % len(_TEXT)])
        i += 1
        time.sleep(TYPE_INTERVAL_S)


def _frame_row(mode: str, phase: str, frame: dict, t0: float) -> dict:
    tiles = frame.get("tiles")
    if tiles is not None:
        return {
            "mode": mode,
            "phase": phase,
            "seq": frame.get("seq"),
            "kind": "tiles",
            "n_tiles": len(tiles),
            "bytes": sum(len(t["bytes"]) for t in tiles),
            "t_rel_s": round(time.perf_counter() - t0, 3),
        }
    return {
        "mode": mode,
        "phase": phase,
        "seq": frame.get("seq"),
        "kind": "full",
        "n_tiles": None,
        "bytes": len(frame.get("bytes") or b""),
        "t_rel_s": round(time.perf_counter() - t0, 3),
    }


def run() -> dict:
    provider, handle, env = boot()
    points: list[dict] = []
    try:
        env.click(x=120, y=120)  # focus the xterm
        for mode, kw in MODES:
            # Typing phase: frames under continuous small-region change.
            stop = threading.Event()
            typist = threading.Thread(target=_typist, args=(env, stop), daemon=True)
            typist.start()
            t0 = time.perf_counter()
            with env.screencast(fps=FPS, timeout=3.0, limit=FRAMES, **kw) as stream:
                for frame in stream:
                    points.append(_frame_row(mode, "typing", frame, t0))
            stop.set()
            typist.join(timeout=2.0)
            env.key("Return")  # flush the typed line so modes start from a fresh prompt
            time.sleep(0.5)

            # Idle phase: an unchanged desktop for IDLE_WINDOW_S — expect the initial
            # capture/keyframe, then idle suppression (nothing).
            t0 = time.perf_counter()
            with env.screencast(fps=FPS, timeout=1.0, limit=FRAMES, **kw) as stream:
                for frame in stream:
                    points.append(_frame_row(mode, "idle", frame, t0))
                    if time.perf_counter() - t0 > IDLE_WINDOW_S:
                        break
            typed = sum(1 for p in points if p["mode"] == mode and p["phase"] == "typing")
            idle = sum(1 for p in points if p["mode"] == mode and p["phase"] == "idle")
            print(f"{mode}: {typed} typing frames, {idle} idle frames", flush=True)
    finally:
        env.close()
        provider.destroy(handle)
    return {
        "modes": [m for m, _ in MODES],
        "fps": FPS,
        "frames_per_mode": FRAMES,
        "type_chars_per_s": round(1.0 / TYPE_INTERVAL_S, 1),
        "idle_window_s": IDLE_WINDOW_S,
        "datapoints": points,
    }


def _mode_rows(points: list[dict], mode: str, phase: str) -> list[dict]:
    return [p for p in points if p["mode"] == mode and p["phase"] == phase]


# Semantic colors per _common.PALETTE: PNG red, JPEG blue, delta green, delta+JPEG accent.
_MODE_STYLE = {
    "full-png": ("full-PNG", PALETTE["png"]),
    "full-jpeg-q80": ("full-JPEG q80", PALETTE["jpeg"]),
    "delta-png": ("delta-PNG", PALETTE["delta"]),
    "delta-jpeg-q80": ("delta-JPEG q80", PALETTE["accent"]),
}


def _log_kib_axis(ax, lo: float, hi: float) -> None:
    """Plain-number ticks on a log KiB axis (no bare mid-decade mathtext gaps)."""
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter

    ticks = [t for t in (0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200) if lo <= t <= hi]
    ax.set_ylim(lo, hi)
    ax.yaxis.set_major_locator(FixedLocator(ticks))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())


def plot(payload: dict) -> None:
    from matplotlib.lines import Line2D

    points = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)
    names = {m: _MODE_STYLE.get(m, (m,))[0] for m in payload["modes"]}
    colors = {m: _MODE_STYLE.get(m, (m, PALETTE["neutral"]))[1] for m in payload["modes"]}

    all_kib: list[float] = []
    for mode in payload["modes"]:
        rows = _mode_rows(points, mode, "typing")
        xs = [p["seq"] for p in rows]
        ys = [max(p["bytes"], 1) / 1024.0 for p in rows]
        all_kib.extend(ys)
        ax1.plot(xs, ys, "-", linewidth=1.0, alpha=0.85, color=colors[mode], label=names[mode])
        keyframes = [(p["seq"], p["bytes"] / 1024.0) for p in rows if p["kind"] == "full"]
        if keyframes and mode.startswith("delta"):
            ax1.plot(*zip(*keyframes), "o", ms=6, mew=1.2, color=colors[mode], mfc="none")
    ax1.set_yscale("log")
    _log_kib_axis(ax1, min(all_kib) * 0.7, max(all_kib) * 1.5)
    ax1.set_xlabel(f"frame seq (fps={payload['fps']:g}, ~{payload['type_chars_per_s']:g} chars/s)")
    ax1.set_ylabel("KiB / frame (log)")
    ax1.set_title("Per-frame payload while typing")
    handles, labels = ax1.get_legend_handles_labels()
    handles.append(
        Line2D([], [], linestyle="none", marker="o", ms=6, mew=1.2,
               mec=PALETTE["neutral"], mfc="none")
    )
    labels.append("delta keyframe")
    ax1.legend(  # below the axes, fully out of the trace region
        handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.22),
        ncol=3, frameon=False, columnspacing=1.2, handlelength=1.6,
    )

    stats = []  # (mode, mean, p10, p90) KiB — show spread, not bare means
    for mode in payload["modes"]:
        kib = sorted(p["bytes"] / 1024.0 for p in _mode_rows(points, mode, "typing"))
        n = len(kib)
        mean = sum(kib) / max(1, n)
        p10 = kib[round(0.10 * (n - 1))] if kib else 0.0
        p90 = kib[round(0.90 * (n - 1))] if kib else 0.0
        stats.append((mode, mean, p10, p90))
    base = stats[0][1] if stats else 1.0
    xs = list(range(len(stats)))
    ax2.bar(xs, [s[1] for s in stats], color=[colors[s[0]] for s in stats], alpha=0.9)
    for x, (mode, mean, p10, p90) in zip(xs, stats):
        ax2.vlines(x, p10, p90, color="#333333", linewidth=1.3)
        ax2.plot([x, x], [p10, p90], "_", color="#333333", ms=9, mew=1.3)
        ax2.text(
            x, max(mean, p90) * 1.3,
            f"{mean:.1f} KiB\n{base / mean:.1f}x" if mean else "0",
            ha="center", va="bottom", fontsize=9,
        )
    ax2.set_yscale("log")
    # log-scale headroom so the tallest label clears the title
    _log_kib_axis(ax2, min(s[2] for s in stats) * 0.6, max(max(s[1], s[3]) for s in stats) * 4.5)
    ax2.set_xticks(xs)
    ax2.set_xticklabels([names[s[0]].replace(" q80", "") for s in stats])
    ax2.set_ylabel("mean KiB / frame (log)")
    ax2.set_xlabel("JPEG q80 · whiskers p10–p90 · x = vs full-PNG")
    ax2.set_title("Mean payload per delivered frame (typing)")
    save_plot(fig, "delta_screencast")


def main() -> int:
    payload = run()
    summary = []
    for mode in payload["modes"]:
        for phase in ("typing", "idle"):
            rows = _mode_rows(payload["datapoints"], mode, phase)
            summary.append(
                {
                    "mode": mode,
                    "phase": phase,
                    "frames": len(rows),
                    "keyframes": sum(1 for p in rows if p["kind"] == "full"),
                    "tile_frames": sum(1 for p in rows if p["kind"] == "tiles"),
                    "total_bytes": sum(p["bytes"] for p in rows),
                    "bytes_per_frame": summarize([float(p["bytes"]) for p in rows]),
                    "tiles_per_frame": summarize(
                        [float(p["n_tiles"]) for p in rows if p["n_tiles"] is not None]
                    ),
                }
            )
    payload["summary"] = summary
    write_result("delta_screencast", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
