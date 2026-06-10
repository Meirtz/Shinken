"""S2 — dirty-tile delta screencast (B2) under a live typing workload, per-frame.

The B2 table in docs/engineering/streaming-bandwidth.md §2 reported per-mode means.
This suite records EVERY delivered frame (seq, payload bytes, WIRE bytes, keyframe-
vs-tiles, tile count, arrival time) for four modes — full-PNG, full-JPEG q80,
delta-PNG, delta-JPEG q80 — under the same workload: typing into the xterm at
~12 chars/s, fps=10, up to FRAMES frames per mode. Each mode runs over BOTH wire
formats: `binary` (binary WS media frames, the negotiated default) and `text`
(base64-in-JSON, pinned via ``connect(..., binary_frames=False)``), so the wire
overhead is measured, not assumed. A separate idle window per cell measures what an
unchanged desktop costs (idle suppression + the initial keyframe).

Payload bytes are the decoded image/tile bytes; wire bytes are the actual WebSocket
message sizes (binary: header+payload; text: the full JSON message, where base64
alone adds ~33%).

The suite also VERIFIES correctness of the delta path: a delta-PNG stream is
composited client-side (keyframe + tiles) and, once the desktop is quiescent,
compared pixel-for-pixel against a full lossless screenshot. Both are PNG, so the
reconstruction must be IDENTICAL (the keyframe cadence bounds drift only for lossy
JPEG tiles); a mismatch fails the suite.

Emits benchmarks/results/delta_screencast.json and
docs/assets/bench/delta_screencast.png.

Run:  python benchmarks/bench_delta_screencast.py
"""

from __future__ import annotations

import io
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
WIRES = ["binary", "text"]
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


def _frame_row(mode: str, wire: str, phase: str, frame: dict, t0: float) -> dict:
    tiles = frame.get("tiles")
    if tiles is not None:
        return {
            "mode": mode,
            "wire": wire,
            "phase": phase,
            "seq": frame.get("seq"),
            "kind": "tiles",
            "n_tiles": len(tiles),
            "bytes": sum(len(t["bytes"]) for t in tiles),
            "wire_bytes": frame.get("wire_len"),
            "t_rel_s": round(time.perf_counter() - t0, 3),
        }
    return {
        "mode": mode,
        "wire": wire,
        "phase": phase,
        "seq": frame.get("seq"),
        "kind": "full",
        "n_tiles": None,
        "bytes": len(frame.get("bytes") or b""),
        "wire_bytes": frame.get("wire_len"),
        "t_rel_s": round(time.perf_counter() - t0, 3),
    }


def _verify_reconstruction(env) -> dict:
    """Composite a delta-PNG stream client-side and compare it pixel-for-pixel with
    a full lossless screenshot once the desktop is quiescent. Returns the verdict;
    raises AssertionError on a gap-free mismatch (a real correctness failure)."""
    from PIL import Image

    env.click(x=120, y=120)
    canvas = None
    applied_tiles = 0
    frames_seen = 0
    last_seq = None
    gaps = 0
    typed = False
    # timeout=1.5: the iterator ends after 1.5s of silence — quiescence detection.
    with env.screencast(fps=FPS, timeout=1.5, delta=True) as stream:
        for frame in stream:
            frames_seen += 1
            seq = frame.get("seq")
            if last_seq is not None and seq != last_seq + 1:
                gaps += 1  # a dropped frame would invalidate compositing until rekey
            last_seq = seq
            if frame.get("tiles") is not None:
                for t in frame["tiles"]:
                    tile = Image.open(io.BytesIO(t["bytes"])).convert("RGB")
                    canvas.paste(tile, (t["x"], t["y"]))
                    applied_tiles += 1
            else:
                canvas = Image.open(io.BytesIO(frame["bytes"])).convert("RGB")
            if not typed:
                # dirty a few tiles AFTER the keyframe, then let the screen settle
                env.type_text("reconstruction-check")
                env.key("Return")
                typed = True
        # the stream went quiet for 1.5s: the desktop is static — ground-truth it
        shot = env.screenshot()  # lossless PNG
    truth = Image.open(io.BytesIO(shot["bytes"])).convert("RGB")
    identical = canvas is not None and canvas.size == truth.size and (
        canvas.tobytes() == truth.tobytes()
    )
    verdict = {
        "checked": True,
        "frames": frames_seen,
        "tiles_applied": applied_tiles,
        "seq_gaps": gaps,
        "identical": bool(identical),
    }
    print(f"reconstruction: {verdict}", flush=True)
    if gaps == 0:
        assert identical, (
            "delta-PNG reconstruction must be pixel-identical to a full screenshot "
            f"on a quiescent desktop (frames={frames_seen}, tiles={applied_tiles})"
        )
    return verdict


def run() -> dict:
    import shinken

    provider, handle, env = boot()
    text_env = shinken.connect(handle.addr, token=handle.token, binary_frames=False)
    points: list[dict] = []
    try:
        env.click(x=120, y=120)  # focus the xterm
        for wire, e in (("binary", env), ("text", text_env)):
            for mode, kw in MODES:
                # Typing phase: frames under continuous small-region change. The
                # typist always types via the binary session; the SAME desktop is
                # what each session streams.
                stop = threading.Event()
                typist = threading.Thread(target=_typist, args=(env, stop), daemon=True)
                typist.start()
                t0 = time.perf_counter()
                with e.screencast(fps=FPS, timeout=3.0, limit=FRAMES, **kw) as stream:
                    for frame in stream:
                        points.append(_frame_row(mode, wire, "typing", frame, t0))
                stop.set()
                typist.join(timeout=2.0)
                env.key("Return")  # flush the typed line so modes start fresh
                time.sleep(0.5)

                # Idle phase: an unchanged desktop for IDLE_WINDOW_S — expect the
                # initial capture/keyframe, then idle suppression (nothing).
                t0 = time.perf_counter()
                with e.screencast(fps=FPS, timeout=1.0, limit=FRAMES, **kw) as stream:
                    for frame in stream:
                        points.append(_frame_row(mode, wire, "idle", frame, t0))
                        if time.perf_counter() - t0 > IDLE_WINDOW_S:
                            break
                n_t = sum(
                    1
                    for p in points
                    if p["mode"] == mode and p["wire"] == wire and p["phase"] == "typing"
                )
                n_i = sum(
                    1
                    for p in points
                    if p["mode"] == mode and p["wire"] == wire and p["phase"] == "idle"
                )
                print(f"{mode} ({wire}): {n_t} typing frames, {n_i} idle frames", flush=True)
        reconstruction = _verify_reconstruction(env)
    finally:
        text_env.close()
        env.close()
        provider.destroy(handle)
    return {
        "modes": [m for m, _ in MODES],
        "wires": WIRES,
        "fps": FPS,
        "frames_per_mode": FRAMES,
        "type_chars_per_s": round(1.0 / TYPE_INTERVAL_S, 1),
        "idle_window_s": IDLE_WINDOW_S,
        "reconstruction": reconstruction,
        "datapoints": points,
    }


def _rows(points: list[dict], mode: str, wire: str, phase: str) -> list[dict]:
    return [
        p for p in points if p["mode"] == mode and p["wire"] == wire and p["phase"] == phase
    ]


def plot(payload: dict) -> None:
    points = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)
    # repo-wide semantics: full-PNG red, full-JPEG blue, delta-PNG green, delta-JPEG accent
    _mode_colors = {
        "full-png": PALETTE["png"],
        "full-jpeg-q80": PALETTE["jpeg"],
        "delta-png": PALETTE["delta"],
        "delta-jpeg-q80": PALETTE["accent"],
    }
    colors = {m: _mode_colors.get(m, "C7") for m in payload["modes"]}

    for mode in payload["modes"]:
        rows = _rows(points, mode, "binary", "typing")
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

    # Mean WIRE bytes per delivered frame: text vs binary framing, per mode.
    width = 0.38
    xticks = range(len(payload["modes"]))
    for off, wire, alpha in ((-width / 2, "text", 0.55), (width / 2, "binary", 1.0)):
        means = []
        for mode in payload["modes"]:
            rows = [p for p in _rows(points, mode, wire, "typing") if p["wire_bytes"]]
            means.append(
                sum(p["wire_bytes"] for p in rows) / max(1, len(rows)) / 1024.0
            )
        bars = ax2.bar(
            [x + off for x in xticks],
            means,
            width,
            color=[colors[m] for m in payload["modes"]],
            alpha=alpha,
            label=f"{wire} wire",
        )
        for bar, m in zip(bars, means):
            ax2.text(
                bar.get_x() + bar.get_width() / 2,
                m * 1.05,
                f"{m:.1f}",
                ha="center",
                fontsize=9,
            )
    ax2.set_yscale("log")
    ax2.set_xticks(list(xticks))
    ax2.set_xticklabels(payload["modes"], fontsize=8)
    ax2.set_ylabel("mean wire KiB / delivered frame (log)")
    ax2.set_title("On-the-wire bytes per frame (typing)\nfaded = text (base64-JSON), solid = binary")
    ax2.legend(fontsize=8)
    save_plot(fig, "delta_screencast")


def main() -> int:
    payload = run()
    summary = []
    for mode in payload["modes"]:
        for wire in payload["wires"]:
            for phase in ("typing", "idle"):
                rows = _rows(payload["datapoints"], mode, wire, phase)
                summary.append(
                    {
                        "mode": mode,
                        "wire": wire,
                        "phase": phase,
                        "frames": len(rows),
                        "keyframes": sum(1 for p in rows if p["kind"] == "full"),
                        "tile_frames": sum(1 for p in rows if p["kind"] == "tiles"),
                        "total_bytes": sum(p["bytes"] for p in rows),
                        "total_wire_bytes": sum(p["wire_bytes"] or 0 for p in rows),
                        "bytes_per_frame": summarize([float(p["bytes"]) for p in rows]),
                        "wire_bytes_per_frame": summarize(
                            [float(p["wire_bytes"]) for p in rows if p["wire_bytes"]]
                        ),
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
