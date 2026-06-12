"""S13 — observation legibility envelope: is a lossy/downscaled tier still READABLE?

Every byte number in S1/S2 (and the remote B1 ladder) is bytes-only: it proves a
(quality, scale) cell is SMALL, not that an agent can still read the screen it
encodes. This suite measures that missing half with zero model cost: scripted
ground-truth text at known screen regions, a host-side OCR judge (tesseract), and
three metrics per (scene x tier x element):

- **legibility** — OCR the element's ground-truth bbox crop (scaled to the tier's
  resolution) and score the normalized edit distance vs the scripted truth; an
  element is *legible* when that distance is <= 0.2,
- **resolvability** — OCR the whole tier frame (word boxes) and require the best
  match for the element's unique token to land its centroid inside the true bbox:
  could an agent still *locate* the target it needs to click?
- **text-region SSIM** — structural similarity of the element crop vs the same
  crop in the lossless PNG control (a model-free degradation score).

Tier ladder: PNG@native (control), JPEG q80@1280, q80@1024, q50@1024, q50@512,
q10@768 — the exact cells the byte stories quote — plus ONE composited
delta-JPEG-q80 stream cell (keyframe + dirty tiles composited client-side, then
assessed: bounds compositing drift on the text-strata scene).

Scenes (fresh sandbox each):

- ``desktop``     — one 80x24-class xterm, default bitmap font (6x13 "fixed"),
                    8 scripted lines: the sparse-desktop class of S1,
- ``dense-text``  — near-fullscreen xterm, 59 rows of ANSI-colored filler with 9
                    scripted truth rows at known positions: the heavy-UI class,
- ``text-strata`` — three xterms rendering scripted lines at DejaVu Sans Mono
                    -fs {8, 11, 16}: the font-size stratification (small text
                    breaks first; this is where the envelope edge lives),
- ``gui-zenity``  — a zenity dialog; ground truth is the **structured
                    observation** (``observe(structured=True)`` -> per-element
                    text + bbox from the guest a11y engine),
- ``photo``       — the S1 procedural photographic frame (no text): bytes +
                    full-frame SSIM anchor only.

Ground-truth bboxes for the xterm scenes derive from ``list_windows`` geometry +
the terminal grid (cols x rows -> cell size; verified pixel-accurate); the PNG
control conditions everything — an element the judge cannot read in the lossless
control is excluded from the envelope (that is an OCR limit, not codec damage).

Needs ``tesseract`` + ``pytesseract`` on the host (brew install tesseract;
pip install pytesseract) — exits with a clear message when absent. Emits
benchmarks/results/obs_quality.json and docs/assets/bench/obs_quality.png.

Run:  python benchmarks/bench_obs_quality.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from _common import (
    GEOMETRY,
    PALETTE,
    boot,
    image_bytes,
    now_ms,
    summarize,
    write_result,
)

NATIVE_W, NATIVE_H = (int(v) for v in GEOMETRY.split("x")[:2])
REPS = 3
LEGIBLE_NED = 0.2  # normalized edit distance at/below which an element is legible
RESOLVE_NED = 1.0 / 3.0  # token-match looseness for the full-frame locate metric

# The static tier ladder: the control + the exact cells the byte stories quote.
TIERS: list[tuple[str, dict]] = [
    ("png-native", {"format": None, "quality": None, "max_long_edge": None}),
    ("jpeg-q80-native", {"format": "jpeg", "quality": 80, "max_long_edge": None}),
    ("jpeg-q80-1024", {"format": "jpeg", "quality": 80, "max_long_edge": 1024}),
    ("jpeg-q50-1024", {"format": "jpeg", "quality": 50, "max_long_edge": 1024}),
    ("jpeg-q10-768", {"format": "jpeg", "quality": 10, "max_long_edge": 768}),
    ("jpeg-q50-512", {"format": "jpeg", "quality": 50, "max_long_edge": 512}),
]
DELTA_TIER = "delta-jpeg-q80-native"
CONTROL_TIER = "png-native"
XTERM_BORDER = 2  # -b 2 internal border, also the booted xterm's default

DESKTOP_LINES = [
    "WREN-104 the quick brown fox jumps",
    "KILN-237 pack my box with five jugs",
    "MOSS-518 vexing wizards jolt quartz",
    "DUNE-642 sphinx of black quartz vow",
    "REEF-379 how quickly daft zebras go",
    "PINE-803 bright vixens leap fences",
    "CLAY-466 jovial pandas mix dough",
    "FERN-921 azure brooks pass the mill",
]
DENSE_LINES = [
    "TARN-115 the quick brown fox jumps over",
    "GULF-228 pack my box with five dozen jugs",
    "VALE-331 vexing wizards jolt the quartz",
    "CRAG-447 sphinx of black quartz judge me",
    "FJORD-553 how quickly daft zebras jump",
    "HEATH-660 bright vixens leap the fence",
    "BLUFF-774 jovial pandas mix the dough",
    "MARSH-881 azure brooks pass the mill",
    "GLEN-996 quiet harbors hold the light",
]
DENSE_TRUTH_ROWS = [3, 9, 15, 21, 27, 33, 39, 45, 51]
STRATA = [
    # (stratum tag, -fs points, frame position, scripted lines)
    (
        "fs8",
        8,
        "+30+40",
        [
            "ALFA-310 small print on the panel",
            "ECHO-425 quick brown fox jumps it",
            "INDIA-538 five dozen quartz jugs",
            "OSCAR-647 vexed wizards jolt back",
            "ROMEO-752 daft zebras jump quick",
            "TANGO-869 bright vixens leap far",
        ],
    ),
    (
        "fs11",
        11,
        "+30+250",
        [
            "BAKER-113 medium print on panel",
            "CIDER-227 quick brown fox jumps",
            "DELTA-334 five dozen quartz jugs",
            "FLORA-449 vexed wizards jolt back",
            "GROVE-556 daft zebras jump quick",
            "HONEY-668 bright vixens leap far",
        ],
    ),
    (
        "fs16",
        16,
        "+30+490",
        [
            "KAPPA-218 large print on panel",
            "LARCH-327 quick brown fox jump",
            "MAPLE-436 five dozen quartz jug",
            "NOBLE-545 vexed wizards jolt it",
            "ORBIT-654 daft zebras jump fast",
            "PRISM-763 bright vixens leap on",
        ],
    ),
]
ZENITY_TEXT = "BIRCH-204 keep the draft copy?"
ZENITY_BUTTONS = ["KEEP-61", "DROP-83", "ARCHIVE-19", "EXPORT-44"]


# --------------------------------------------------------------------------- guards
def ocr_ready() -> str | None:
    """None when tesseract + pytesseract are usable, else the human-readable reason."""
    if shutil.which("tesseract") is None:
        return "tesseract binary not found (macOS: brew install tesseract)"
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return "python package pytesseract not installed (pip install pytesseract)"
    return None


# ----------------------------------------------------------------- guest-side setup
def _put_script(env, name: str, content: str) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".sh")
    guest = f"/tmp/skn_{name}.sh"
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o644)
        env.put_file(tmp, guest)
    finally:
        os.unlink(tmp)
    return guest


def _spawn_window(env, cmd: str, timeout_s: float = 12.0) -> dict:
    """Spawn a desktop app detached (setsid, so it outlives the exec channel's
    process group) and return the NEW window's ``list_windows`` entry."""
    before = {w["id"] for w in env.list_windows()}
    env.exec(shell=f"setsid {cmd} >/dev/null 2>&1 &", env={"DISPLAY": ":0"})
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        new = [w for w in env.list_windows() if w["id"] not in before]
        if new:
            return new[0]
        time.sleep(0.15)
    raise RuntimeError(f"window never appeared for: {cmd}")


def _grid_elements(
    win: dict, cols: int, rows: int, lines: list[str], stratum: str, prefix: str
) -> list[dict]:
    """Per-line ground-truth bboxes from the terminal grid: the client window is
    ``cols x rows`` character cells inside the internal border, so cell size falls
    out of the window geometry (verified pixel-accurate against captures)."""
    cw = (win["w"] - 2 * XTERM_BORDER) / cols
    ch = (win["h"] - 2 * XTERM_BORDER) / rows
    els = []
    for k, line in enumerate(lines):
        els.append(
            {
                "id": f"{prefix}{k}",
                "stratum": stratum,
                "truth": line,
                "bbox": [
                    int(win["x"] + XTERM_BORDER),
                    int(win["y"] + XTERM_BORDER + k * ch),
                    int(len(line) * cw + 1),
                    int(ch + 1),
                ],
                "cell_h": round(ch, 2),
                "psm": 7,
            }
        )
    return els


def _print_script(lines: list[str]) -> str:
    body = "\n".join(f"printf '%s\\n' '{line}'" for line in lines)
    return f"clear\n{body}\nsleep 600\n"


def _setup_desktop(env) -> list[dict]:
    script = _put_script(env, "desktop", _print_script(DESKTOP_LINES))
    win = _spawn_window(env, f"xterm -b 2 -geometry 46x10+520+340 -e bash {script}")
    time.sleep(1.0)
    return _grid_elements(win, 46, 10, DESKTOP_LINES, "term-6x13", "d")


def _setup_dense(env) -> list[dict]:
    filler_base = (
        "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
        "tempor incididunt ut labore et dolore magna aliqua "
    )
    rows: list[str] = []
    truth_at = dict(zip(DENSE_TRUTH_ROWS, DENSE_LINES, strict=True))
    for i in range(59):
        if i in truth_at:
            rows.append(f"printf '%s\\n' '{truth_at[i]}'")
        else:
            filler = (f"{i:04d} " + filler_base * 2)[:208]
            rows.append(f"printf '\\e[3%sm%s\\e[0m\\n' '{1 + (i % 6)}' '{filler}'")
    script = _put_script(env, "dense", "clear\n" + "\n".join(rows) + "\nsleep 600\n")
    win = _spawn_window(env, f"xterm -b 2 -geometry 209x61+0+0 -e bash {script}")
    time.sleep(1.5)
    els = []
    for row, line in truth_at.items():
        cw = (win["w"] - 2 * XTERM_BORDER) / 209
        ch = (win["h"] - 2 * XTERM_BORDER) / 61
        els.append(
            {
                "id": f"n{row}",
                "stratum": "term-6x13-dense",
                "truth": line,
                "bbox": [
                    int(win["x"] + XTERM_BORDER),
                    int(win["y"] + XTERM_BORDER + row * ch),
                    int(len(line) * cw + 1),
                    int(ch + 1),
                ],
                "cell_h": round(ch, 2),
                "psm": 7,
            }
        )
    return els


def _setup_strata(env) -> list[dict]:
    """Three xterms at DejaVu Sans Mono -fs {8,11,16}. Each runs a trigger loop:
    ``touch /tmp/skn_go.<i>`` clears and reprints the SAME lines — stage 1 paints
    the static scene; later stages replay the paint while a delta screencast is
    live, so the text arrives as dirty tiles (the compositing-drift cell)."""
    els: list[dict] = []
    for tag, fs, pos, lines in STRATA:
        body = "\n".join(f"  printf '%s\\n' '{line}'" for line in lines)
        script = _put_script(
            env,
            f"strata_{tag}",
            "clear\ni=0\nwhile :; do\n  i=$((i+1))\n"
            '  until [ -e "/tmp/skn_go.$i" ]; do sleep 0.2; done\n'
            f"  clear\n  sleep 0.3\n{body}\ndone\n",
        )
        win = _spawn_window(
            env,
            f"xterm -fa 'DejaVu Sans Mono' -fs {fs} -b 2 -geometry 40x7{pos} -e bash {script}",
        )
        els.extend(_grid_elements(win, 40, 7, lines, tag, f"{tag}-"))
    env.exec(["touch", "/tmp/skn_go.1"])
    time.sleep(1.5)
    return els


def _setup_zenity(env) -> list[dict]:
    """Ground truth from the guest a11y engine: launch a zenity dialog, then take
    ``observe(structured=True)`` and record (text, bbox) for the label + buttons."""
    cmd = (
        'zenity --question --title "Draft export" '
        f'--text "{ZENITY_TEXT}" --ok-label "{ZENITY_BUTTONS[0]}" '
        f'--cancel-label "{ZENITY_BUTTONS[1]}" --extra-button "{ZENITY_BUTTONS[2]}" '
        f'--extra-button "{ZENITY_BUTTONS[3]}" --width 360'
    )
    _spawn_window(env, cmd)
    time.sleep(1.5)
    obs = env.observe(structured=True)
    by_name = {
        e.get("name"): e
        for e in obs.get("elements", [])
        if e.get("name") and e.get("bbox") and e["bbox"][2] > 0 and e["bbox"][3] > 0
    }
    els = []
    for truth in [ZENITY_TEXT, *ZENITY_BUTTONS]:
        el = by_name.get(truth)
        if el is None:
            raise RuntimeError(
                f"a11y ground truth missing {truth!r}; structured observation "
                f"returned names: {sorted(by_name)}"
            )
        els.append(
            {
                "id": f"z{len(els)}",
                "stratum": "gui",
                "truth": truth,
                "bbox": list(el["bbox"]),
                "cell_h": 20.0,  # GTK default label line height, for OCR upscale only
                "psm": 6 if truth == ZENITY_TEXT else 7,  # the label may wrap
            }
        )
    return els


def _setup_photo(env) -> list[dict]:
    """The S1 photographic frame, painted via the typed exec channel (deterministic —
    no keystroke routing) and verified by polling a native PNG into the
    natural-image size class, exactly like ``_common.show_photo``."""
    from _common import PHOTO_MIN_PNG_BYTES, synth_photo_ppm

    fd, tmp = tempfile.mkstemp(suffix=".ppm")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(synth_photo_ppm(NATIVE_W, NATIVE_H))
        os.chmod(tmp, 0o644)
        env.put_file(tmp, "/tmp/photo.ppm")
    finally:
        os.unlink(tmp)
    r = env.exec(
        shell="xloadimage -onroot -quiet /tmp/photo.ppm && "
        "xdotool search --class xterm windowunmap %@",
        env={"DISPLAY": ":0"},
        timeout=20,
    )
    if r.get("exit_code") != 0:
        raise RuntimeError(f"photo paint failed: {r.get('stderr')!r}")
    deadline = time.time() + 20.0
    last = 0
    while time.time() < deadline:
        last = len(image_bytes(env.act("screenshot", scope="screen")))
        if last > PHOTO_MIN_PNG_BYTES:
            return []  # no text: bytes + full-frame SSIM anchor only
        time.sleep(0.5)
    raise RuntimeError(f"photo never painted: native PNG stayed at {last} bytes")


SCENES = [
    ("desktop", _setup_desktop),
    ("dense-text", _setup_dense),
    ("text-strata", _setup_strata),
    ("gui-zenity", _setup_zenity),
    ("photo", _setup_photo),
]


# ----------------------------------------------------------------------- capture
def _capture_static(env, scene: str, frames_dir: Path, frame_rows: list[dict]) -> None:
    for rep in range(REPS):
        for tier, kw in TIERS:
            t0 = now_ms()
            reply = env.act("screenshot", scope="screen", **kw)
            ms = now_ms() - t0
            img = reply.get("image") or {}
            raw = image_bytes(reply)
            ext = "jpg" if img.get("format") == "jpeg" else "png"
            (frames_dir / f"{scene}__{tier}__{rep}.{ext}").write_bytes(raw)
            frame_rows.append(
                {
                    "scene": scene,
                    "tier": tier,
                    "rep": rep,
                    "bytes": len(raw),
                    "ms": round(ms, 3),
                    "w": img.get("w"),
                    "h": img.get("h"),
                }
            )


def _capture_delta(env, scene: str, frames_dir: Path, frame_rows: list[dict]) -> None:
    """The composited-stream cell: open a delta-JPEG screencast, trigger a guest-side
    clear+reprint of the strata text, composite keyframe+tiles client-side (exactly
    what a streaming consumer does), and hand the CANVAS to the same OCR judge. The
    text regions are repainted by lossy tiles over a lossy keyframe — the cell bounds
    compositing drift. Bytes are the whole stream's payload (keyframe + tiles)."""
    from PIL import Image

    for rep in range(REPS):
        trigger = rep + 2  # stage 1 painted the static scene
        canvas = None
        total = 0
        keyframes = 0
        tiles_applied = 0
        frames = 0
        triggered = False
        with env.screencast(
            fps=10, timeout=2.5, delta=True, format="jpeg", quality=80
        ) as stream:
            for frame in stream:
                frames += 1
                if frame.get("tiles") is not None:
                    for t in frame["tiles"]:
                        tile = Image.open(io.BytesIO(t["bytes"])).convert("RGB")
                        canvas.paste(tile, (t["x"], t["y"]))
                        tiles_applied += 1
                    total += sum(len(t["bytes"]) for t in frame["tiles"])
                else:
                    canvas = Image.open(io.BytesIO(frame["bytes"])).convert("RGB")
                    keyframes += 1
                    total += len(frame["bytes"])
                if not triggered:
                    env.exec(["touch", f"/tmp/skn_go.{trigger}"])
                    triggered = True
        if canvas is None or tiles_applied == 0:
            raise RuntimeError(
                f"delta rep {rep}: no composited canvas ({frames} frames, "
                f"{tiles_applied} tiles) — did the strata trigger loop run?"
            )
        canvas.save(frames_dir / f"{scene}__{DELTA_TIER}__{rep}.png")
        frame_rows.append(
            {
                "scene": scene,
                "tier": DELTA_TIER,
                "rep": rep,
                "bytes": total,  # whole-stream payload: keyframe(s) + tiles
                "ms": None,
                "w": canvas.size[0],
                "h": canvas.size[1],
                "stream_frames": frames,
                "keyframes": keyframes,
                "tiles_applied": tiles_applied,
            }
        )
        print(
            f"{scene} {DELTA_TIER} rep{rep}: {frames} frames, {keyframes} keyframes, "
            f"{tiles_applied} tiles, {total / 1024:.1f} KiB stream",
            flush=True,
        )


def run(frames_dir: Path) -> dict:
    elements: dict[str, list[dict]] = {}
    frame_rows: list[dict] = []
    for scene, setup in SCENES:
        provider, handle, env = boot()
        try:
            elements[scene] = setup(env)
            _capture_static(env, scene, frames_dir, frame_rows)
            if scene == "text-strata":
                _capture_delta(env, scene, frames_dir, frame_rows)
            print(f"{scene}: {len(elements[scene])} elements captured", flush=True)
        finally:
            env.close()
            provider.destroy(handle)
    return {
        "tiers": [t for t, _ in TIERS] + [DELTA_TIER],
        "tier_params": {t: kw for t, kw in TIERS},
        "control_tier": CONTROL_TIER,
        "reps": REPS,
        "legible_ned_max": LEGIBLE_NED,
        "native": [NATIVE_W, NATIVE_H],
        "scenes": [s for s, _ in SCENES],
        "elements": elements,
        "frame_rows": frame_rows,
    }


# ---------------------------------------------------------------- host-side judge
def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _ned(a: str, b: str) -> float:
    """Normalized Levenshtein distance over whitespace/case-normalized strings."""
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / max(len(a), len(b))


def _ssim(a, b) -> float:
    """SSIM on two equal-size uint8 grayscale arrays — skimage when present, else
    a uniform-window (8x8) implementation with the standard constants."""
    import numpy as np

    try:
        from skimage.metrics import structural_similarity

        win = min(7, min(a.shape) | 1)  # odd, never larger than the crop
        if win < 3:
            win = 3
        return float(structural_similarity(a, b, data_range=255.0, win_size=win))
    except ImportError:
        pass
    a = a.astype("float64")
    b = b.astype("float64")
    k = min(8, *a.shape)

    def win_mean(x):
        c = np.cumsum(np.cumsum(x, 0), 1)
        c = np.pad(c, ((1, 0), (1, 0)))
        return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)

    mu_a, mu_b = win_mean(a), win_mean(b)
    va = win_mean(a * a) - mu_a**2
    vb = win_mean(b * b) - mu_b**2
    cov = win_mean(a * b) - mu_a * mu_b
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / (
        (mu_a**2 + mu_b**2 + c1) * (va + vb + c2)
    )
    return float(s.mean())


def _frame_path(frames_dir: Path, scene: str, tier: str, rep: int) -> Path:
    for ext in ("png", "jpg"):
        p = frames_dir / f"{scene}__{tier}__{rep}.{ext}"
        if p.exists():
            return p
    raise FileNotFoundError(f"{scene}__{tier}__{rep}")


def _crop(img, bbox: list[int], scale: float, cell_h: float, pad_frac: float = 0.18):
    x, y, w, h = bbox
    pad = max(1, round(cell_h * scale * pad_frac))
    box = (
        max(0, round(x * scale) - pad),
        max(0, round(y * scale) - pad),
        min(img.size[0], round((x + w) * scale) + pad),
        min(img.size[1], round((y + h) * scale) + pad),
    )
    return img.crop(box)


def _ocr_crop(crop, cell_h: float, scale: float, psm: int) -> str:
    """OCR one element crop, upscaled so a text line is ~48 px tall (tesseract's
    comfort zone) — the same deterministic pipeline for every tier, so the tiers
    differ only in the pixels the runtime actually delivered."""
    import pytesseract
    from PIL import Image

    f = min(8.0, max(1.0, 48.0 / max(1.0, cell_h * scale)))
    if f > 1.0:
        crop = crop.resize(
            (max(1, round(crop.size[0] * f)), max(1, round(crop.size[1] * f))),
            Image.LANCZOS,
        )
    return pytesseract.image_to_string(crop.convert("L"), config=f"--psm {psm}").strip()


def _frame_words(img) -> list[tuple[str, float, float]]:
    """Word boxes for the resolvability metric: OCR the whole frame at native
    resolution (sparse + block segmentation merged) -> (text, cx, cy) in native px."""
    import pytesseract

    words: list[tuple[str, float, float]] = []
    gray = img.convert("L")
    for psm in (11, 6):
        d = pytesseract.image_to_data(
            gray, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
        )
        for i, text in enumerate(d["text"]):
            if text.strip():
                words.append(
                    (
                        text.strip(),
                        d["left"][i] + d["width"][i] / 2.0,
                        d["top"][i] + d["height"][i] / 2.0,
                    )
                )
    return words


def _resolvable(words, el: dict) -> bool:
    token = el["truth"].split()[0]
    x, y, w, h = el["bbox"]
    pad = max(8.0, el["cell_h"])
    best = None
    for text, cx, cy in words:
        d = _ned(text, token)
        if d <= RESOLVE_NED and (best is None or d < best[0]):
            best = (d, cx, cy)
    if best is None:
        return False
    _, cx, cy = best
    return (x - pad) <= cx <= (x + w + pad) and (y - pad) <= cy <= (y + h + pad)


def assess(payload: dict, frames_dir: Path) -> None:
    """Host-side judge: every captured frame, scaled coords, one OCR pipeline."""
    import numpy as np
    from PIL import Image

    element_rows: list[dict] = []
    controls: dict[str, Image.Image] = {}
    for scene in payload["scenes"]:
        controls[scene] = Image.open(
            _frame_path(frames_dir, scene, CONTROL_TIER, 0)
        ).convert("RGB")

    for row in payload["frame_rows"]:
        scene, tier, rep = row["scene"], row["tier"], row["rep"]
        img = Image.open(_frame_path(frames_dir, scene, tier, rep)).convert("RGB")
        scale = img.size[0] / NATIVE_W
        control = controls[scene]
        # full-frame SSIM vs the control (tiers upscaled back to native first)
        up = (
            img if img.size == control.size else img.resize(control.size, Image.BICUBIC)
        )
        row["ssim_full"] = round(
            _ssim(
                np.asarray(up.convert("L"), dtype="uint8"),
                np.asarray(control.convert("L"), dtype="uint8"),
            ),
            4,
        )
        els = payload["elements"].get(scene) or []
        if not els:
            continue
        native = img if scale == 1.0 else img.resize(control.size, Image.LANCZOS)
        words = _frame_words(native)
        for el in els:
            crop = _crop(img, el["bbox"], scale, el["cell_h"])
            ocr = _ocr_crop(crop, el["cell_h"], scale, el["psm"])
            ned = round(_ned(ocr, el["truth"]), 4)
            c_crop = _crop(control, el["bbox"], 1.0, el["cell_h"])
            t_crop = (
                crop
                if crop.size == c_crop.size
                else crop.resize(c_crop.size, Image.BICUBIC)
            )
            ssim = round(
                _ssim(
                    np.asarray(t_crop.convert("L"), dtype="uint8"),
                    np.asarray(c_crop.convert("L"), dtype="uint8"),
                ),
                4,
            )
            element_rows.append(
                {
                    "scene": scene,
                    "tier": tier,
                    "rep": rep,
                    "element": el["id"],
                    "stratum": el["stratum"],
                    "ned": ned,
                    "legible": ned <= LEGIBLE_NED,
                    "resolvable": _resolvable(words, el),
                    "ssim": ssim,
                    "ocr": ocr[:80],
                }
            )
        print(f"judged {scene} {tier} rep{rep}", flush=True)
    payload["element_rows"] = element_rows

    # Control conditioning: an element unreadable in the lossless control is an OCR
    # limit, not codec damage — excluded from the legibility envelope (recorded).
    for scene, els in payload["elements"].items():
        for el in els:
            ctl = [
                r
                for r in element_rows
                if r["scene"] == scene
                and r["element"] == el["id"]
                and r["tier"] == CONTROL_TIER
            ]
            el["control_legible"] = bool(ctl) and all(r["legible"] for r in ctl)
            el["control_resolvable"] = bool(ctl) and all(r["resolvable"] for r in ctl)


def _summarize(payload: dict) -> None:
    rows = payload["element_rows"]
    eligible = {
        (s, e["id"]): (e["control_legible"], e["control_resolvable"])
        for s, els in payload["elements"].items()
        for e in els
    }

    def frac(sub: list[dict], key: str, which: int) -> tuple[float | None, int]:
        ok = [r for r in sub if eligible[(r["scene"], r["element"])][which]]
        if not ok:
            return None, 0
        return round(sum(1 for r in ok if r[key]) / len(ok), 4), len(ok)

    cells = []
    for scene in payload["scenes"]:
        for tier in payload["tiers"]:
            frows = [
                r
                for r in payload["frame_rows"]
                if r["scene"] == scene and r["tier"] == tier
            ]
            if not frows:
                continue
            erows = [r for r in rows if r["scene"] == scene and r["tier"] == tier]
            leg, n_leg = frac(erows, "legible", 0)
            res, n_res = frac(erows, "resolvable", 1)
            cells.append(
                {
                    "scene": scene,
                    "tier": tier,
                    "bytes": summarize([float(r["bytes"]) for r in frows]),
                    "ssim_full": round(
                        sum(r["ssim_full"] for r in frows) / len(frows), 4
                    ),
                    "legible_frac": leg,
                    "n_legible_dp": n_leg,
                    "resolvable_frac": res,
                    "n_resolvable_dp": n_res,
                }
            )
    payload["cells"] = cells

    strata = sorted({r["stratum"] for r in rows})
    envelope = []
    for stratum in strata:
        for tier in payload["tiers"]:
            sub = [r for r in rows if r["stratum"] == stratum and r["tier"] == tier]
            if not sub:
                continue
            leg, n = frac(sub, "legible", 0)
            res, _ = frac(sub, "resolvable", 1)
            mean_ssim = round(sum(r["ssim"] for r in sub) / len(sub), 4)
            envelope.append(
                {
                    "stratum": stratum,
                    "tier": tier,
                    "n": n,
                    "legible_frac": leg,
                    "resolvable_frac": res,
                    "mean_ssim": mean_ssim,
                    "safe": leg is not None and leg >= 0.99,
                }
            )
    payload["envelope"] = envelope


# ----------------------------------------------------------------------- figure
# Left panel: color = tier FAMILY (the three native-scale codecs collapse into one
# family — they are indistinguishable at 100%), marker = scene. Colorblind-safe:
# blue ramp for the @1024 pair, orange/brown for the aggressive downscales.
_NATIVE_TIERS = ("png-native", "jpeg-q80-native", "delta-jpeg-q80-native")
_TIER_FAMILY = {
    "png-native": "#1b4f72",
    "jpeg-q80-native": "#1b4f72",
    "delta-jpeg-q80-native": "#1b4f72",
    "jpeg-q80-1024": "#2e86c1",
    "jpeg-q50-1024": "#85c1e9",
    "jpeg-q10-768": "#e67e22",
    "jpeg-q50-512": "#935116",
}
_FAMILY_LEGEND = [  # <= 6 entries, grouped by tier family
    ("native 1280 (PNG / q80 / delta)", "#1b4f72"),
    ("q80 @1024", "#2e86c1"),
    ("q50 @1024", "#85c1e9"),
    ("q10 @768", "#e67e22"),
    ("q50 @512", "#935116"),
]
_SCENE_MARKERS = {
    "desktop": "^",
    "dense-text": "o",
    "text-strata": "s",
    "gui-zenity": "D",
}
_SCENE_KEY = [  # inline marker key (scene), keeps the legend at 5 entries
    ("^", "desktop"),
    ("o", "dense text"),
    ("s", "mixed fonts"),
    ("D", "GTK dialog"),
]
_TIER_TICKS = {
    "png-native": "PNG native",
    "jpeg-q80-native": "q80 @1280",
    "jpeg-q80-1024": "q80 @1024",
    "jpeg-q50-1024": "q50 @1024",
    "jpeg-q10-768": "q10 @768",
    "jpeg-q50-512": "q50 @512",
    "delta-jpeg-q80-native": "delta-q80",
}
_STRATUM_STYLE = {  # color + marker per stratum (no red/green pair)
    "term-6x13": ("#0b3d66", "o", "fixed 6x13 (desktop)"),
    "term-6x13-dense": ("#2e86c1", "s", "fixed 6x13 (dense)"),
    "fs8": ("#dc7633", "v", "mono fs8"),
    "fs11": ("#935116", "D", "mono fs11"),
    "fs16": ("#6c3483", "^", "mono fs16"),
    "gui": (PALETTE["neutral"], "X", "GTK dialog"),
}


def plot(payload: dict) -> None:
    from _common import new_axes, pct, save_plot
    from matplotlib.lines import Line2D

    fig, (ax1, ax2) = new_axes(2, width=5.8, height=5.1)
    cells = payload["cells"]
    tiers = payload["tiers"]

    # Panel 1 — the Pareto the byte stories were missing: bytes vs legibility.
    text_cells = [c for c in cells if c["legible_frac"] is not None]  # photo: no text
    for c in text_cells:
        ax1.scatter(
            c["bytes"]["mean"] / 1024.0,
            100.0 * c["legible_frac"],
            s=70,
            marker=_SCENE_MARKERS[c["scene"]],
            color=_TIER_FAMILY[c["tier"]],
            edgecolors="white",
            linewidths=0.6,
            zorder=3,
        )
    # Title claims come from the payload, never hardcoded.
    native_min = min(
        c["legible_frac"]
        for c in text_cells
        if c["tier"] in ("jpeg-q80-native", "delta-jpeg-q80-native")
    )
    down_max = max(
        c["legible_frac"] for c in text_cells if c["tier"] not in _NATIVE_TIERS
    )
    ax1.set_title(
        pct(
            f"q80 at native scale stays {100 * native_min:.0f}% legible;\n"
            f"every downscale breaks text (best cell {100 * down_max:.0f}%)"
        )
    )
    ax1.axhline(99.0, color=PALETTE["neutral"], ls=":", lw=1.2)
    ax1.text(
        0.02,
        0.915,
        pct("99% legibility bar"),
        transform=ax1.transAxes,
        fontsize=11,
        color=PALETTE["neutral"],
    )
    ax1.set_xscale("log")
    ax1.set_xlabel("KiB per frame, log (delta tier: whole stream)")
    ax1.set_ylabel(pct("% elements legible (control-conditioned)"))
    ax1.set_ylim(-4, 112)
    ax1.legend(
        handles=[
            Line2D([], [], marker="s", ls="", ms=8, color=col, label=lab)
            for lab, col in _FAMILY_LEGEND
        ],
        loc="upper right",
        bbox_to_anchor=(0.99, 0.86),
        fontsize=11,
        labelspacing=0.35,
        borderpad=0.35,
        handletextpad=0.5,
    )
    ax1.text(
        0.60,
        0.355,
        "marker = scene",
        transform=ax1.transAxes,
        fontsize=11,
        style="italic",
        color="0.25",
        va="top",
    )
    for i, (m, name) in enumerate(_SCENE_KEY):
        row, col = divmod(i, 2)
        kx, ky = 0.60 + 0.21 * col, 0.272 - 0.072 * row
        ax1.plot(
            [kx],
            [ky],
            transform=ax1.transAxes,
            marker=m,
            ms=7,
            ls="",
            color="0.35",
            mec="white",
            mew=0.5,
        )
        ax1.text(
            kx + 0.025, ky, name, transform=ax1.transAxes, fontsize=11, va="center"
        )

    # Panel 2 — the font-size stratification: where the envelope edge lives.
    env_rows = payload["envelope"]
    xs = list(range(len(tiers)))
    for i, (stratum, (color, marker, label)) in enumerate(_STRATUM_STYLE.items()):
        pts = {e["tier"]: e for e in env_rows if e["stratum"] == stratum}
        xv = [
            x
            for x, t in zip(xs, tiers, strict=True)
            if t in pts and pts[t]["legible_frac"] is not None
        ]
        if not xv:
            continue
        off = (i - (len(_STRATUM_STYLE) - 1) / 2.0) * 0.05  # dodge exact overlaps
        yv = [100.0 * pts[tiers[x]]["legible_frac"] for x in xv]
        ax2.plot(
            [x + off for x in xv],
            yv,
            marker=marker,
            ms=6,
            lw=1.7,
            color=color,
            mec="white",
            mew=0.4,
            label=label,
        )
    ax2.axhline(99.0, color=PALETTE["neutral"], ls=":", lw=1.2)
    ax2.text(
        0.02,
        0.835,
        pct("99% bar"),
        transform=ax2.transAxes,
        fontsize=11,
        color=PALETTE["neutral"],
    )
    ax2.set_xticks(xs)
    ax2.set_xticklabels(
        [_TIER_TICKS[t] for t in tiers], rotation=20, ha="right", fontsize=11
    )
    ax2.set_ylabel(pct("% elements legible (control-conditioned)"))
    ax2.set_ylim(-4, 112)
    ax2.set_title("Legibility by font size — small text breaks first")
    ax2.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.34),
        ncols=3,
        fontsize=11,
        frameon=False,
        columnspacing=1.2,
        handletextpad=0.5,
    )
    save_plot(fig, "obs_quality")


def main() -> int:
    reason = ocr_ready()
    if reason:
        print(f"bench_obs_quality SKIPPED: {reason}")
        return 2
    import pytesseract

    frames_dir = Path(tempfile.mkdtemp(prefix="skn_obs_quality_"))
    try:
        payload = run(frames_dir)
        payload["tesseract"] = str(pytesseract.get_tesseract_version())
        assess(payload, frames_dir)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
    _summarize(payload)
    write_result("obs_quality", payload)
    plot(payload)
    # plain-language verdict on stdout: which advertised tiers are NOT safe
    unsafe = [
        f"{e['stratum']} x {e['tier']} ({100 * e['legible_frac']:.0f}% of {e['n']})"
        for e in payload["envelope"]
        if e["legible_frac"] is not None and not e["safe"]
    ]
    print("UNSAFE cells (legibility < 99%):" if unsafe else "all cells legible >= 99%")
    for u in unsafe:
        print(f"  {u}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
