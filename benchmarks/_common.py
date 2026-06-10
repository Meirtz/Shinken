"""Shared plumbing for the local benchmark suites (docs/engineering/benchmarks.md).

Every suite in this directory is a standalone, rerunnable script that measures the
LOCAL vertical slice (DockerLocalProvider + shinkend over loopback WebSocket) and
emits two artifacts:

- ``benchmarks/results/<suite>.json`` — raw datapoints + environment metadata
  (tracked, so the numbers in the doc are reproducible and auditable), and
- ``docs/assets/bench/<suite>.png`` — the matplotlib figure the
  benchmarks doc embeds.

Run from the repo root (Docker + the ``shinken/sandbox-linux`` image required)::

    python benchmarks/bench_codec_ladder.py        # or any other bench_*.py

``PYTHONPATH=sdk/python/src`` is optional — this module bootstraps the in-repo SDK
when ``shinken`` is not installed. No suite needs network access beyond the local
Docker daemon; raw JSON stays lean (numbers only, never image payloads).
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
PLOTS_DIR = REPO_ROOT / "docs" / "assets" / "bench"

# One semantic palette for every figure in the repo (both the local suites here and
# plot_remote.py): a codec/mode always renders in the same color everywhere.
PALETTE = {
    "png": "#c0392b",  # lossless PNG — red
    "jpeg": "#2980b9",  # lossy JPEG — blue
    "delta": "#27ae60",  # lossless dirty-tile delta — green
    "shared": "#27ae60",  # SharedLoop / async single-thread — green
    "sync": "#c0392b",  # thread-per-session sync facade — red
    "async": "#2980b9",  # async core — blue
    "accent": "#8e44ad",
    "neutral": "#555555",
}

# Always measure THIS checkout's SDK: prepend it so an installed/editable `shinken`
# from another checkout can never shadow the code under test.
_SDK = REPO_ROOT / "sdk" / "python" / "src"
if str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))

IMAGE = os.environ.get("SHINKEN_BENCH_IMAGE", "shinken/sandbox-linux")
GEOMETRY = os.environ.get("SHINKEN_BENCH_GEOMETRY", "1280x800x24")


def _cmd(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def env_meta() -> dict[str, Any]:
    """Environment fingerprint recorded into every result JSON — enough to judge
    what the numbers mean (host class, image arch, native-vs-emulated) without
    leaking anything host-private beyond hardware model and tool versions."""
    meta: dict[str, Any] = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": _cmd(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]
        ),
        "python": platform.python_version(),
        "host_os": f"{platform.system()} {platform.release()}",
        "host_arch": platform.machine(),
    }
    if platform.system() == "Darwin":
        meta["host_cpu"] = _cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
        meta["host_cores"] = _cmd(["sysctl", "-n", "hw.ncpu"])
        mem = _cmd(["sysctl", "-n", "hw.memsize"])
        if mem.isdigit():
            meta["host_mem_gib"] = round(int(mem) / 2**30, 1)
        ver = _cmd(["sw_vers", "-productVersion"])
        if ver:
            meta["host_os"] = f"macOS {ver}"
    meta["docker_server"] = _cmd(
        ["docker", "version", "--format", "{{.Server.Version}}"]
    )
    # Honesty marker: any non-suite containers running on the same daemon when the suite
    # finished. Byte numbers are robust to host contention; latency numbers less so.
    meta["concurrent_containers"] = [
        n
        for n in _cmd(["docker", "ps", "--format", "{{.Names}}"]).splitlines()
        if n and not n.startswith("shinken-bench")
    ]
    meta["image"] = IMAGE
    meta["image_arch"] = _cmd(
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Os}}/{{.Architecture}}"]
    )
    meta["screen_geometry"] = GEOMETRY
    return meta


def boot(image: str = IMAGE, geometry: str = GEOMETRY):
    """Create one local Docker sandbox and connect; returns (provider, handle, env).
    Caller owns teardown: ``env.close()`` then ``provider.destroy(handle)``."""
    from shinken.providers.base import SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(image=image, name_prefix="shinken-bench")
    handle = provider.create(SandboxSpec(screen_geometry=geometry))
    env = provider.connect(handle)
    return provider, handle, env


def fill_xterm(env, lines: int = 40, settle_timeout: float = 15.0) -> None:
    """Put realistic text content on the desktop: focus the xterm the image boots
    (80x24 at +20+20) and fill it, so codec benchmarks see a real UI frame rather
    than a near-empty desktop.

    The guest-side ready signal fires when the root is painted (S8), which can be
    a few hundred ms BEFORE the xterm maps — so first probe the desktop: click +
    keystroke, and require the frame to change before trusting the xterm with the
    real fill command (otherwise the content scenario would silently be empty)."""
    deadline = time.time() + settle_timeout
    last = env.screenshot()["png"]
    while time.time() < deadline:
        env.click(x=120, y=120)
        env.type_text("true")
        env.key("Return")
        time.sleep(0.3)
        cur = env.screenshot()["png"]
        if cur != last:  # the probe (or the xterm mapping) painted — it is live
            break
        last = cur
    env.click(x=120, y=120)
    cmd = (
        f"for i in $(seq 1 {lines}); do "
        "printf '%4d lorem ipsum dolor sit amet, consectetur adipiscing elit %s\\n'"
        " $i $RANDOM; done"
    )
    env.type_text(cmd)
    env.key("Return")
    time.sleep(1.5)  # let the shell paint


def _value_noise(rng, w: int, h: int, cells: int):
    """One octave of smooth value noise: a seeded random lattice, bilinearly
    interpolated (with smoothstep easing) up to w x h."""
    import numpy as np

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


def synth_photo_ppm(w: int, h: int, seed: int = 42) -> bytes:
    """A deterministic, procedurally generated frame with NATURAL-IMAGE statistics
    (the photographic operating point the xterm scenarios cannot reach), as binary
    PPM (P6): multi-octave value noise (Perlin-style fBm) drives both luminance and
    a slowly varying hue field, broad sinusoidal gradients add large-scale structure,
    and Gaussian grain emulates sensor noise. Everything derives from the fixed seed,
    so the frame is byte-identical across runs — fully reproducible with no binary
    asset (and no photo licensing) in the repo. At 1280x800 the frame PNG-encodes to
    ~2 MiB while JPEG q80 lands near 110 KiB — the same compression class as a real
    content-rich desktop (see docs/engineering/streaming-bandwidth.md B1). numpy is
    already a host dependency (matplotlib requires it)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    fbm = np.zeros((h, w))
    total = 0.0
    for cells, amp in [
        (3, 1.0),
        (6, 0.5),
        (12, 0.25),
        (24, 0.125),
        (48, 0.0625),
        (96, 0.03125),
    ]:
        fbm += amp * _value_noise(rng, w, h, cells)
        total += amp
    fbm /= total
    hue = (_value_noise(rng, w, h, 4) + 0.4 * _value_noise(rng, w, h, 16)) / 1.4
    yy = np.linspace(0, 1, h)[:, None]
    xx = np.linspace(0, 1, w)[None, :]
    lum = (
        70
        + 130 * fbm
        + 35 * np.sin(2.2 * np.pi * yy + 1.0)
        + 20 * np.sin(1.7 * np.pi * xx)
    )
    sat = 60 * (0.4 + 0.6 * fbm)
    img = np.stack(
        [lum + sat * np.sin(2 * np.pi * hue + phase) for phase in (0.0, 2.1, 4.2)],
        axis=-1,
    )
    img += rng.normal(0.0, 6.0, img.shape)  # sensor-like grain
    pixels = np.clip(img, 0, 255).astype(np.uint8)
    return b"P6\n%d %d\n255\n" % (w, h) + pixels.tobytes()


# A 1280x800 frame of photographic content PNG-encodes to ~2 MiB; the sparse default
# desktop is ~65 KiB. Anything above this threshold proves the photo is actually painted.
PHOTO_MIN_PNG_BYTES = 800 * 1024


def show_photo(env, timeout_s: float = 30.0) -> int:
    """Paint the deterministic photographic frame across the WHOLE screen: generate it
    host-side (synth_photo_ppm), ``put_file`` it into the guest, then drive the booted
    xterm to ``xloadimage -onroot`` it onto the root window and unmap the xterm itself,
    leaving 100% photographic pixels. Verifies the paint actually happened by polling a
    native PNG screenshot until its size is in the natural-image class (>
    PHOTO_MIN_PNG_BYTES) — never trusts the keystrokes blindly. Returns the verified
    PNG byte size."""
    w, h = (int(v) for v in GEOMETRY.split("x")[:2])
    fd, tmp = tempfile.mkstemp(suffix=".ppm")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(synth_photo_ppm(w, h))
        # mkstemp creates 0600 and `docker cp` preserves mode + maps ownership to root;
        # the guest desktop runs as user `shinken`, so make the file world-readable.
        os.chmod(tmp, 0o644)
        env.put_file(tmp, "/tmp/photo.ppm")
    finally:
        os.unlink(tmp)
    env.click(x=120, y=120)  # focus the xterm the image boots (80x24 at +20+20)
    env.type_text(
        "xloadimage -onroot -quiet /tmp/photo.ppm && xdotool getactivewindow windowunmap"
    )
    env.key("Return")
    deadline = time.time() + timeout_s
    last = 0
    while time.time() < deadline:
        reply = env.act("screenshot", scope="screen")
        last = len(image_bytes(reply))
        if last > PHOTO_MIN_PNG_BYTES:
            return last
        time.sleep(0.5)
    raise RuntimeError(
        f"photo scenario never painted: native PNG stayed at {last} bytes "
        f"(< {PHOTO_MIN_PNG_BYTES}) after {timeout_s}s — is xloadimage in the image? "
        "(rebuild shinken/sandbox-linux from this checkout)"
    )


def summarize(values: list[float]) -> dict[str, float]:
    """min/p50/mean/(p90/p99)/max for a latency or size series.

    Tail percentiles are only emitted when the sample actually supports them
    (nearest-rank p90 needs n>=30, p99 needs n>=100) — below that they would just
    relabel the max. Consumers should ``.get("p90")`` and fall back to ``max``."""
    if not values:
        return {}
    s = sorted(values)

    def pct(q: float) -> float:
        idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
        return s[idx]

    out = {
        "n": len(s),
        "min": round(s[0], 3),
        "p50": round(pct(0.50), 3),
        "mean": round(statistics.fmean(s), 3),
        "max": round(s[-1], 3),
    }
    if len(s) >= 30:
        out["p90"] = round(pct(0.90), 3)
    if len(s) >= 100:
        out["p99"] = round(pct(0.99), 3)
    return out


def write_result(suite: str, payload: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{suite}.json"
    payload = {"suite": suite, "meta": env_meta(), **payload}
    out.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return out


def style() -> None:
    """Apply the one shared matplotlib style (Agg, no display). Idempotent; called
    by ``new_axes`` and importable by any plotting script outside this package."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.dpi": 140,
            "font.size": 11,
            "axes.titlesize": 12,
            "axes.labelsize": 11,
            "legend.fontsize": 9,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linewidth": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )


def new_axes(
    ncols: int = 1, *, height: float = 4.2, width: float = 6.4, nrows: int = 1
):
    """Consistent matplotlib axes for every suite figure."""
    style()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(nrows, ncols, figsize=(width * ncols, height * nrows))
    for ax in fig.axes:
        ax.grid(True, which="both", alpha=0.25, linewidth=0.6)
    return fig, axes


def save_plot(fig, suite: str, out_dir: Path | None = None) -> Path:
    """Save the suite figure. Default target is ``docs/assets/bench/`` (``PLOTS_DIR``,
    where ALL tracked figures live); ``out_dir`` overrides for one-off destinations."""
    target = PLOTS_DIR if out_dir is None else Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{suite}.png"
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return out


def image_bytes(reply_or_image: dict) -> bytes:
    """Raw encoded bytes of an observation's image, on EITHER wire surface: the
    binary path carries raw bytes (``data``/``bytes``), the text path a base64
    ``ref``. Accepts the full observation reply or the image dict itself."""
    import base64 as _b64

    img = reply_or_image.get("image") or reply_or_image
    for key in ("data", "bytes"):
        raw = img.get(key)
        if raw is not None:
            return bytes(raw)
    return _b64.b64decode(img.get("ref", ""))


def now_ms() -> float:
    return time.perf_counter() * 1000.0
