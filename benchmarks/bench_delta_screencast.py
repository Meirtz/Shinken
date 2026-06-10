"""S2 — dirty-tile delta screencast (B2) under a live typing workload, per-frame.

The B2 table in docs/engineering/streaming-bandwidth.md §2 reported per-mode means.
This suite records EVERY delivered frame (seq, payload bytes, keyframe-vs-tiles,
tile count, arrival time) for four modes — full-PNG, full-JPEG q80, delta-PNG,
delta-JPEG q80 — under the same workload: typing into the xterm at ~12 chars/s,
fps=10, up to FRAMES frames per mode. A separate idle window per mode measures
what an unchanged desktop costs (idle suppression + the initial keyframe).

Payload bytes are the decoded image/tile bytes (base64+JSON wire framing adds
~33%). Emits benchmarks/results/delta_screencast.json and
docs/engineering/assets/benchmarks/delta_screencast.png.

Run:  python benchmarks/bench_delta_screencast.py
"""

from __future__ import annotations

import sys
import threading
import time

from _common import boot, new_axes, save_plot, summarize, write_result

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


def plot(payload: dict) -> None:
    points = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)
    colors = dict(zip(payload["modes"], ["C0", "C1", "C2", "C3"]))

    for mode in payload["modes"]:
        rows = _mode_rows(points, mode, "typing")
        xs = [p["seq"] for p in rows]
        ys = [max(p["bytes"], 1) / 1024.0 for p in rows]
        ax1.plot(xs, ys, "-", linewidth=0.9, alpha=0.8, color=colors[mode], label=mode)
        keyframes = [(p["seq"], p["bytes"] / 1024.0) for p in rows if p["kind"] == "full"]
        if keyframes and mode.startswith("delta"):
            ax1.plot(*zip(*keyframes), "o", ms=5, color=colors[mode], mfc="none")
    ax1.set_yscale("log")
    ax1.set_xlabel("frame seq")
    ax1.set_ylabel("KiB / frame (log)")
    ax1.set_title(
        f"Per-frame payload, typing at ~{payload['type_chars_per_s']} chars/s, "
        f"fps={payload['fps']:g}\n(circles = delta keyframes)"
    )
    ax1.legend(fontsize=8)

    means = []
    for mode in payload["modes"]:
        rows = _mode_rows(points, mode, "typing")
        means.append(sum(p["bytes"] for p in rows) / max(1, len(rows)) / 1024.0)
    base = means[0] if means else 1.0
    bars = ax2.bar(payload["modes"], means, color=[colors[m] for m in payload["modes"]])
    for bar, m in zip(bars, means):
        ax2.text(
            bar.get_x() + bar.get_width() / 2,
            m * 1.05,
            f"{m:.1f} KiB\n({base / m:.1f}x)" if m else "0",
            ha="center",
            fontsize=8,
        )
    ax2.set_yscale("log")
    # log-scale headroom so the tallest bar's label clears the title
    ax2.set_ylim(min(means) * 0.5, max(means) * 4.0)
    ax2.set_ylabel("mean KiB / frame (log)")
    ax2.set_title("Mean payload per delivered frame (typing)\n(x = reduction vs full-PNG)")
    ax2.tick_params(axis="x", labelsize=8)
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
