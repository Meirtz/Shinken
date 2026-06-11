"""S8 — guest CPU of the capture loop: poll-diff vs XDamage-driven, per sandbox.

The delta screencast already made idle cost ~zero BYTES (S2); this suite measures what
idle and active streaming cost in GUEST CPU — the resource that bounds sandbox density
on a host. Two identical sandboxes, one with damage-driven capture (the default), one
with ``SHINKEND_DAMAGE=off`` (the pre-damage poll-diff loop: full GetImage + diff every
tick). For each: a no-stream baseline, then fps ∈ {5, 10, 30} × workload ∈ {idle,
typing ~12 chars/s} windows of a delta-JPEG q80 screencast (the recommended streaming
mode), reading per-process CPU (utime+stime from ``/proc/<pid>/stat`` via ``docker
exec``) for shinkend and Xvfb plus whole-container ``cpu.stat usage_usec`` before and
after each window.

The headline: with XDamage, an IDLE stream costs ~0 guest CPU at any fps (a clean tick
captures nothing), and an active stream's cost is proportional to the damaged region —
poll-diff pays a full-frame GetImage + tile diff every tick regardless.

Emits benchmarks/results/guest_cpu.json and
docs/assets/bench/guest_cpu.png.

Run:  python benchmarks/bench_guest_cpu.py
"""

from __future__ import annotations

import subprocess
import sys
import threading
import os
import time

from _common import boot as _boot_default
from _common import GEOMETRY, IMAGE, new_axes, save_plot, write_result

FPS_LEVELS = [5.0, 10.0, 30.0]
WINDOW_S = 8.0
TYPE_INTERVAL_S = 0.08  # ~12 chars/s, matching S2
_TEXT = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "


def _exec(cid: str, *argv: str) -> str:
    out = subprocess.run(
        ["docker", "exec", cid, *argv], capture_output=True, text=True, timeout=20
    )
    return out.stdout.strip()


def _guest_pids(cid: str) -> dict[str, int]:
    return {
        name: int(_exec(cid, "pgrep", "-x", name).splitlines()[0])
        for name in ("shinkend", "Xvfb")
    }


def _clk_tck(cid: str) -> int:
    out = _exec(cid, "getconf", "CLK_TCK")
    return int(out) if out.isdigit() else 100


def _proc_cpu_s(cid: str, pid: int, hz: int) -> float:
    """utime+stime (seconds) of one guest process, from /proc/<pid>/stat."""
    stat = _exec(cid, "cat", f"/proc/{pid}/stat")
    fields = stat.rsplit(")", 1)[1].split()  # after the (comm) field
    utime, stime = int(fields[11]), int(fields[12])  # fields 14/15, 1-indexed
    return (utime + stime) / hz


def _container_cpu_s(cid: str) -> float | None:
    """Whole-container CPU seconds (cgroup v2 cpu.stat usage_usec)."""
    out = _exec(cid, "cat", "/sys/fs/cgroup/cpu.stat")
    for line in out.splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1]) / 1e6
    return None


def _sample(cid: str, pids: dict[str, int], hz: int) -> dict:
    return {
        "shinkend": _proc_cpu_s(cid, pids["shinkend"], hz),
        "xvfb": _proc_cpu_s(cid, pids["Xvfb"], hz),
        "container": _container_cpu_s(cid),
    }


def _typist(env, stop: threading.Event) -> None:
    i = 0
    while not stop.is_set():
        env.type_text(_TEXT[i % len(_TEXT)])
        i += 1
        time.sleep(TYPE_INTERVAL_S)


def _measure_window(env, cid, pids, hz, fps: float | None, workload: str) -> dict:
    """One cell: optionally stream delta-JPEG at `fps` for WINDOW_S while idle or
    typing; return CPU seconds burned per process and frames/bytes delivered."""
    stop = threading.Event()
    typist = None
    frames = 0
    payload = 0

    def window() -> tuple[dict, dict, float]:
        nonlocal frames, payload
        s0 = _sample(cid, pids, hz)
        t0 = time.perf_counter()
        t_end = t0 + WINDOW_S
        if fps is None:
            time.sleep(WINDOW_S)
        else:
            while time.perf_counter() < t_end:
                try:
                    frame = next(stream)
                except StopIteration:
                    continue  # idle: no frame within the read timeout — keep waiting
                frames += 1
                tiles = frame.get("tiles")
                payload += (
                    sum(len(t["bytes"]) for t in tiles)
                    if tiles is not None
                    else len(frame.get("bytes") or b"")
                )
        elapsed = time.perf_counter() - t0
        s1 = _sample(cid, pids, hz)
        return s0, s1, elapsed

    if workload == "typing":
        typist = threading.Thread(target=_typist, args=(env, stop), daemon=True)

    if fps is None:
        if typist:
            typist.start()
        s0, s1, elapsed = window()
    else:
        with env.screencast(
            fps=fps, timeout=0.5, delta=True, format="jpeg", quality=80
        ) as stream:
            if typist:
                typist.start()
            s0, s1, elapsed = window()
    if typist:
        stop.set()
        typist.join(timeout=2.0)
        env.key("Return")
        time.sleep(0.3)

    def pct(key: str) -> float | None:
        if s0[key] is None or s1[key] is None:
            return None
        return round((s1[key] - s0[key]) / elapsed * 100.0, 2)

    return {
        "fps": fps,
        "workload": workload,
        "window_s": round(elapsed, 2),
        "frames": frames,
        "payload_bytes": payload,
        "shinkend_cpu_pct": pct("shinkend"),
        "xvfb_cpu_pct": pct("xvfb"),
        "container_cpu_pct": pct("container"),
    }


def _boot(damage: bool):
    from shinken.providers.base import SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(image=IMAGE, name_prefix="shinken-bench-cpu")
    spec = SandboxSpec(
        screen_geometry=GEOMETRY,
        extra_env={} if damage else {"SHINKEND_DAMAGE": "off"},
    )
    handle = provider.create(spec)
    env = provider.connect(handle)
    return provider, handle, env


def _damage_mode_from_logs(cid: str) -> str:
    out = subprocess.run(
        ["docker", "logs", cid], capture_output=True, text=True, timeout=20
    )
    text = out.stdout + out.stderr
    if "damage tracking = on" in text:
        return "on"
    if "damage tracking = off" in text:
        return "off"
    return "unknown"


def run() -> dict:
    _ = _boot_default  # (suite uses its own boot to inject SHINKEND_DAMAGE)
    cells: list[dict] = []
    modes: list[dict] = []
    for damage in (False, True):
        provider, handle, env = _boot(damage)
        cid = handle.metadata.get("container_id") or handle.sandbox_id
        try:
            mode = "damage" if damage else "poll"
            confirmed = _damage_mode_from_logs(str(cid))
            assert confirmed == ("on" if damage else "off"), (
                f"expected damage tracking {'on' if damage else 'off'}, "
                f"shinkend log says {confirmed!r}"
            )
            modes.append({"mode": mode, "shinkend_log_damage": confirmed})
            env.click(x=120, y=120)  # focus the xterm
            pids = _guest_pids(str(cid))
            hz = _clk_tck(str(cid))
            # Let the boot tail fully drain before any idle window: start.sh
            # re-asserts the root paint at 1 Hz for 30 s (the focus/paint fixes),
            # and each re-paint fires a damage event — an idle window inside that
            # tail measures the tail, not idle.
            time.sleep(float(os.environ.get("SHINKEN_BENCH_BOOT_SETTLE_S", "33")))

            # no-stream baseline (idle desktop, no screencast)
            cell = _measure_window(env, str(cid), pids, hz, None, "idle")
            cell["mode"] = mode
            cells.append(cell)
            print(f"{mode}: baseline {cell['shinkend_cpu_pct']}% shinkend", flush=True)

            for fps in FPS_LEVELS:
                for workload in ("idle", "typing"):
                    cell = _measure_window(env, str(cid), pids, hz, fps, workload)
                    cell["mode"] = mode
                    cells.append(cell)
                    print(
                        f"{mode}: fps={fps:4.0f} {workload:6s} shinkend "
                        f"{cell['shinkend_cpu_pct']:6.2f}%  xvfb {cell['xvfb_cpu_pct']:6.2f}%  "
                        f"container {cell['container_cpu_pct']}%  frames {cell['frames']}",
                        flush=True,
                    )
        finally:
            env.close()
            provider.destroy(handle)
    return {
        "fps_levels": FPS_LEVELS,
        "window_s": WINDOW_S,
        "stream_mode": "delta-jpeg-q80",
        "type_chars_per_s": round(1.0 / TYPE_INTERVAL_S, 1),
        "modes": modes,
        "cells": cells,
    }


def plot(payload: dict) -> None:
    cells = payload["cells"]
    fig, (ax1, ax2) = new_axes(2)
    for ax, workload in ((ax1, "idle"), (ax2, "typing")):
        labels = [f"fps {int(f)}" for f in payload["fps_levels"]]
        x = range(len(labels))
        width = 0.38
        for off, mode, color in (
            (-width / 2, "poll", "C3"),
            (width / 2, "damage", "C2"),
        ):
            ys = []
            for f in payload["fps_levels"]:
                row = next(
                    c
                    for c in cells
                    if c["mode"] == mode and c["fps"] == f and c["workload"] == workload
                )
                ys.append((row["shinkend_cpu_pct"] or 0) + (row["xvfb_cpu_pct"] or 0))
            bars = ax.bar(
                [i + off for i in x], ys, width, color=color, label=f"{mode}-capture"
            )
            for bar, y in zip(bars, ys):
                ax.text(
                    bar.get_x() + bar.get_width() / 2,
                    y + 0.3,
                    f"{y:.1f}",
                    ha="center",
                    fontsize=7,
                )
        base = [
            (c["shinkend_cpu_pct"] or 0) + (c["xvfb_cpu_pct"] or 0)
            for c in cells
            if c["fps"] is None
        ]
        ax.axhline(
            sum(base) / len(base),
            linestyle=":",
            color="gray",
            linewidth=1,
            label="no-stream baseline",
        )
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels)
        ax.set_ylabel("guest CPU \\% of one core (shinkend + Xvfb)")
        ax.set_title(
            f"Guest capture cost — {workload} desktop\n"
            f"(delta-JPEG q80 stream, {payload['window_s']:g}s windows)"
        )
        ax.legend(fontsize=8)
    save_plot(fig, "guest_cpu")


def main() -> int:
    payload = run()
    write_result("guest_cpu", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
