#!/usr/bin/env python3
"""Deterministic 1280x800 RGB24 frame sequences for the video-codec spike.

Three content classes, chosen to bracket the dirty-tile delta path (B2):

  typing     mostly-static desktop + ~12 chars/s typed into a white terminal window
             (the workload shape of benchmarks/bench_delta_screencast.py) + 1 Hz cursor
             blink — the case delta-PNG already wins.
  scroll     a full-width text page scrolling vertically at 120 px/s (4 px/frame @30fps)
             — every tile in the text area dirties every frame.
  photo-pan  a procedurally generated photographic frame (vendored
             benchmarks/_common.synth_photo_ppm, natural-image statistics) panning
             horizontally at 240 px/s — the worst case: all tiles dirty, photo entropy.

Output is headerless rawvideo (rgb24) on stdout or --out FILE, consumable by both the
rust harness (vcspike) and ffmpeg (-f rawvideo -pix_fmt rgb24 -s 1280x800).
Everything derives from fixed seeds; reruns are byte-identical on the same Pillow/numpy.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 800
SECONDS = 10
TYPE_CHARS_PER_S = 12.0  # matches bench_delta_screencast.py's typist
SCROLL_PX_PER_S = 120
PAN_PX_PER_S = 240
# bench_delta_screencast.py's _TEXT
_TEXT = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "


# ---- vendored from benchmarks/_common.py (paper-hardening branch) ----
# (the worktree base predates synth_photo_ppm; vendored verbatim so the spike is
# self-contained — same fixed seed, same byte-identical output)
def _value_noise(rng, w: int, h: int, cells: int):
    lat = rng.random((cells + 1, cells + 1))
    gy = np.linspace(0, cells, h, endpoint=False)
    gx = np.linspace(0, cells, w, endpoint=False)
    y0 = np.floor(gy).astype(int)
    x0 = np.floor(gx).astype(int)
    ty = gy - y0
    tx = gx - x0
    ty = (ty * ty * (3 - 2 * ty))[:, None]  # smoothstep
    tx = (tx * tx * (3 - 2 * tx))[None, :]
    a = lat[np.ix_(y0, x0)]
    b = lat[np.ix_(y0, x0 + 1)]
    c = lat[np.ix_(y0 + 1, x0)]
    d = lat[np.ix_(y0 + 1, x0 + 1)]
    return a * (1 - tx) * (1 - ty) + b * tx * (1 - ty) + c * (1 - tx) * ty + d * tx * ty


def synth_photo(w: int, h: int, seed: int = 42) -> np.ndarray:
    """benchmarks/_common.synth_photo_ppm minus the PPM header: returns (h, w, 3) u8."""
    rng = np.random.default_rng(seed)
    fbm = np.zeros((h, w))
    total = 0.0
    for cells, amp in [(3, 1.0), (6, 0.5), (12, 0.25), (24, 0.125), (48, 0.0625), (96, 0.03125)]:
        fbm += amp * _value_noise(rng, w, h, cells)
        total += amp
    fbm /= total
    hue = (_value_noise(rng, w, h, 4) + 0.4 * _value_noise(rng, w, h, 16)) / 1.4
    yy = np.linspace(0, 1, h)[:, None]
    xx = np.linspace(0, 1, w)[None, :]
    lum = 70 + 130 * fbm + 35 * np.sin(2.2 * np.pi * yy + 1.0) + 20 * np.sin(1.7 * np.pi * xx)
    sat = 60 * (0.4 + 0.6 * fbm)
    img = np.stack(
        [lum + sat * np.sin(2 * np.pi * hue + phase) for phase in (0.0, 2.1, 4.2)], axis=-1
    )
    img += rng.normal(0.0, 6.0, img.shape)  # sensor-like grain
    return np.clip(img, 0, 255).astype(np.uint8)
# ---- end vendored ----


def _font(size: int):
    return ImageFont.load_default(size=size)


def _desktop_base() -> Image.Image:
    """A sparse desktop: flat background, taskbar, one window frame (xterm-like)."""
    img = Image.new("RGB", (W, H), (46, 52, 64))
    d = ImageDraw.Draw(img)
    d.rectangle([0, H - 36, W, H], fill=(28, 32, 40))  # taskbar
    d.rectangle([16, 14, 16 + 724, 14 + 26], fill=(70, 80, 100))  # title bar
    d.text((26, 18), "xterm", fill=(220, 224, 232), font=_font(14))
    d.rectangle([16, 40, 16 + 724, 40 + 424], fill=(255, 255, 255))  # terminal body
    return img


def gen_typing(fps: int):
    """Chars appear at TYPE_CHARS_PER_S inside the terminal; the cursor blinks at 1 Hz."""
    base = _desktop_base()
    font = _font(15)
    n = SECONDS * fps
    pad_x, pad_y, line_h, cols = 26, 48, 19, 76
    for i in range(n):
        t = i / fps
        nchars = int(t * TYPE_CHARS_PER_S)
        text = (_TEXT * 4)[:nchars]
        img = base.copy()
        d = ImageDraw.Draw(img)
        lines = [text[j : j + cols] for j in range(0, max(len(text), 1), cols)]
        for li, line in enumerate(lines):
            d.text((pad_x, pad_y + li * line_h), line, fill=(0, 0, 0), font=font)
        if int(t * 2) % 2 == 0:  # 1 Hz blink (on half a second, off half a second)
            cx = pad_x + (len(lines[-1]) if lines else 0) * 9
            cy = pad_y + (len(lines) - 1 if lines else 0) * line_h
            d.rectangle([cx, cy + 2, cx + 8, cy + 16], fill=(0, 0, 0))
        yield np.asarray(img, dtype=np.uint8)


def gen_scroll(fps: int):
    """A tall rendered text page scrolling up through a 1240x740 viewport."""
    rng = np.random.default_rng(7)
    total_scroll = SCROLL_PX_PER_S * SECONDS
    page_h = H + total_scroll + 100
    page = Image.new("RGB", (W - 40, page_h), (250, 250, 250))
    d = ImageDraw.Draw(page)
    font = _font(15)
    words = _TEXT.split()
    y = 8
    while y < page_h - 24:
        k = int(rng.integers(8, 16))
        line = " ".join(words[int(rng.integers(0, len(words)))] for _ in range(k))
        d.text((12, y), line, fill=(20, 20, 20), font=font)
        y += 22
    page_a = np.asarray(page, dtype=np.uint8)
    frame = np.full((H, W, 3), (46, 52, 64), dtype=np.uint8)  # window chrome border
    n = SECONDS * fps
    step = SCROLL_PX_PER_S // fps
    for i in range(n):
        off = i * step
        frame[30 : 30 + 740, 20 : 20 + (W - 40)] = page_a[off : off + 740]
        yield frame.copy()


def gen_photo_pan(fps: int):
    """A wide procedural photo cropped 1280x800, panning right at PAN_PX_PER_S."""
    total_pan = PAN_PX_PER_S * SECONDS
    wide = synth_photo(W + total_pan + 16, H, seed=42)
    n = SECONDS * fps
    step = PAN_PX_PER_S // fps
    for i in range(n):
        off = i * step
        yield np.ascontiguousarray(wide[:, off : off + W])


GENERATORS = {"typing": gen_typing, "scroll": gen_scroll, "photo-pan": gen_photo_pan}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content", required=True, choices=sorted(GENERATORS))
    ap.add_argument("--fps", type=int, required=True, choices=(10, 30))
    ap.add_argument("--out", default="-", help="output file ('-' = stdout)")
    args = ap.parse_args()

    sink = sys.stdout.buffer if args.out == "-" else open(args.out, "wb")
    try:
        for frame in GENERATORS[args.content](args.fps):
            assert frame.shape == (H, W, 3) and frame.dtype == np.uint8
            sink.write(frame.tobytes())
    finally:
        if sink is not sys.stdout.buffer:
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
