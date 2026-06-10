#!/usr/bin/env python3
"""Orchestrate the video-codec spike matrix and write evidence.json.

Matrix: {typing, scroll, photo-pan} x {10, 30} fps x encoders:
  in-process (vcspike, single thread):
    delta-png    shinkend's B2 dirty-tile path (faithful replication) — the incumbent
    delta-jpeg   B2 with jpeg q80 tiles (the runtime's other tile codec)
    jpeg         full-frame JPEG q80 + idle suppression (runtime delta=false)
    h264@{1,2,4}Mbps  openh264 ScreenContentRealTime (the candidate)
  ffmpeg CLI (cross-check instruments, -threads 1, zero-latency settings):
    vp8@{1,4}Mbps     libvpx -deadline realtime -cpu-used 8 -lag-in-frames 0
    vp9@{1,4}Mbps     libvpx-vp9, same realtime settings
    x264@4Mbps        libx264 veryfast + zerolatency — GPL, REFERENCE ONLY (not
                      distributable with Apache-2.0); included as an upper bound.

ffmpeg encode CPU comes from `-benchmark` utime and INCLUDES rawvideo demux +
rgb24->yuv420p swscale (the vcspike h264 numbers include the same RGB->YUV step,
reported split). Raw .rgb sequences are regenerated per cell and deleted afterwards
(lean artifacts); evidence.json is the only kept output.

Usage: python3 run_matrix.py [--keep-rgb]    (run from spikes/video-codec/)
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
VCSPIKE = HERE / "harness" / "target" / "release" / "vcspike"
W, H, SECONDS = 1280, 800, 10
CONTENTS = ("typing", "scroll", "photo-pan")
FPSES = (10, 30)
H264_BPS = (1_000_000, 2_000_000, 4_000_000)
VPX_BPS = (1_000_000, 4_000_000)


def sh(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=True, **kw)


def gen(content: str, fps: int) -> Path:
    rgb = RUNS / f"{content}_{fps}.rgb"
    if not rgb.exists():
        print(f"  gen {rgb.name}", flush=True)
        sh([sys.executable, str(HERE / "gen_frames.py"), "--content", content,
            "--fps", str(fps), "--out", str(rgb)])
    return rgb


def run_vcspike(rgb: Path, mode: str, fps: int, bitrate: int | None = None) -> dict:
    args = [str(VCSPIKE), mode, str(W), str(H), str(fps)]
    if bitrate is not None:
        args.append(str(bitrate))
    with rgb.open("rb") as f:
        out = subprocess.run(args, stdin=f, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


_BENCH_RE = re.compile(r"bench: utime=([0-9.]+)s stime=([0-9.]+)s rtime=([0-9.]+)s")
_PSNR_RE = re.compile(r"PSNR.*average:([0-9.inf]+)")


def _bench(stderr: str) -> dict:
    m = _BENCH_RE.search(stderr)
    if not m:
        raise RuntimeError(f"no bench line in ffmpeg stderr:\n{stderr[-2000:]}")
    return {"utime_s": float(m.group(1)), "stime_s": float(m.group(2)),
            "rtime_s": float(m.group(3))}


def ffmpeg_encode(rgb: Path, fps: int, codec_args: list[str], out: Path) -> dict:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-benchmark",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
           "-i", str(rgb), *codec_args, str(out)]
    r = sh(cmd)
    return _bench(r.stderr)


def ffmpeg_decode(out: Path, fps: int) -> dict:
    in_args = ["-r", str(fps)] if out.suffix == ".h264" else []
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-benchmark", "-threads", "1",
           *in_args, "-i", str(out), "-f", "null", "-"]
    return _bench(sh(cmd).stderr)


def ffmpeg_psnr(out: Path, rgb: Path, fps: int) -> float | None:
    in_args = ["-r", str(fps)] if out.suffix == ".h264" else []
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-threads", "1",
           *in_args, "-i", str(out),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
           "-i", str(rgb),
           "-lavfi", "[0:v]format=rgb24[d];[d][1:v]psnr", "-f", "null", "-"]
    m = _PSNR_RE.search(sh(cmd).stderr)
    return float(m.group(1)) if m and m.group(1) != "inf" else None


def packet_count(out: Path) -> int:
    r = sh(["ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(out)])
    return int(r.stdout.strip())


def ffmpeg_cell(rgb: Path, fps: int, name: str, codec_args: list[str], suffix: str,
                frames_in: int) -> dict:
    out = RUNS / f"{rgb.stem}_{name}.{suffix}"
    enc = ffmpeg_encode(rgb, fps, codec_args, out)
    dec = ffmpeg_decode(out, fps)
    psnr = ffmpeg_psnr(out, rgb, fps)
    pkts = packet_count(out)
    size = out.stat().st_size
    out.unlink()
    return {
        "encoder": name,
        "instrument": "ffmpeg-cli",
        "frames_in": frames_in,
        "frames_delivered": pkts,
        "bytes_total": size,
        "bytes_per_s": size / SECONDS,
        # utime over ALL input frames; includes rawvideo demux + swscale rgb24->yuv420p.
        "encode_ms_per_frame": {"mean": enc["utime_s"] * 1e3 / frames_in},
        "decode_ms_per_delivered_frame": {"mean": dec["utime_s"] * 1e3 / max(pkts, 1)},
        "psnr_db": psnr,
        "bench_raw": {"encode": enc, "decode": dec},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-rgb", action="store_true",
                    help="keep the raw .rgb sequences (~0.3-1 GB each)")
    args = ap.parse_args()

    RUNS.mkdir(exist_ok=True)
    if not VCSPIKE.exists():
        print("building vcspike (cargo build --release)...", flush=True)
        sh(["cargo", "build", "--release"], cwd=HERE / "harness")

    results = []
    t0 = time.time()
    for content in CONTENTS:
        for fps in FPSES:
            frames_in = SECONDS * fps
            print(f"== {content} @ {fps} fps ==", flush=True)
            rgb = gen(content, fps)

            for mode in ("delta-png", "delta-jpeg", "jpeg"):
                r = run_vcspike(rgb, mode, fps)
                r.update(encoder=r.pop("mode"), instrument="vcspike-in-process")
                results.append({"content": content, "fps": fps, **r})
                print(f"  {r['encoder']:14s} {r['bytes_per_s']/1e3:12.1f} KB/s", flush=True)
            for bps in H264_BPS:
                r = run_vcspike(rgb, "h264", fps, bps)
                r.update(encoder=f"h264-openh264@{bps//1_000_000}M",
                         instrument="vcspike-in-process")
                r.pop("mode")
                results.append({"content": content, "fps": fps, **r})
                print(f"  {r['encoder']:14s} {r['bytes_per_s']/1e3:12.1f} KB/s", flush=True)

            for bps in VPX_BPS:
                m = bps // 1_000_000
                results.append({"content": content, "fps": fps, **ffmpeg_cell(
                    rgb, fps, f"vp8-libvpx@{m}M",
                    ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
                     "-lag-in-frames", "0", "-threads", "1", "-b:v", str(bps),
                     "-g", "30", "-f", "ivf"], "ivf", frames_in)})
                results.append({"content": content, "fps": fps, **ffmpeg_cell(
                    rgb, fps, f"vp9-libvpx@{m}M",
                    ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                     "-lag-in-frames", "0", "-row-mt", "0", "-threads", "1",
                     "-b:v", str(bps), "-g", "30", "-f", "ivf"], "ivf", frames_in)})
            # GPL reference point — licensing excludes it from any shippable tier.
            results.append({"content": content, "fps": fps, **ffmpeg_cell(
                rgb, fps, "x264-ref@4M",
                ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                 "-threads", "1", "-b:v", "4000000", "-g", "30", "-f", "h264"],
                "h264", frames_in)})
            for row in results:
                if row["content"] == content and row["fps"] == fps and \
                        row["instrument"] == "ffmpeg-cli":
                    print(f"  {row['encoder']:14s} {row['bytes_per_s']/1e3:12.1f} KB/s",
                          flush=True)

            if not args.keep_rgb:
                rgb.unlink()

    meta = {
        "spike": "video-codec",
        "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu": subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, text=True).stdout.strip()
            or platform.processor(),
        },
        "resolution": f"{W}x{H}",
        "seconds_per_sequence": SECONDS,
        "ffmpeg": sh(["ffmpeg", "-version"]).stdout.splitlines()[0],
        "rustc": sh(["rustc", "--version"]).stdout.strip(),
        "notes": [
            "bytes are raw payload bytes (pre-base64); the runtime's base64-in-JSON "
            "wire adds +1/3 to every contender equally",
            "all encoders single-threaded; vcspike times pure compute in-process; "
            "ffmpeg utime includes rawvideo demux + swscale rgb24->yuv420p",
            "psnr_db null = lossless (delta-png); harness PSNR is RGB-domain, "
            "ffmpeg PSNR uses the psnr filter forced to rgb24",
            "x264 rows are a GPL REFERENCE ONLY — not distributable with Apache-2.0",
        ],
    }
    evidence = {"meta": meta, "results": results}
    (HERE / "evidence.json").write_text(json.dumps(evidence, indent=1) + "\n")
    print(f"wrote evidence.json ({len(results)} rows) in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
