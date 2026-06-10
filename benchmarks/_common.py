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
        "git_commit": _cmd(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"]),
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
    meta["docker_server"] = _cmd(["docker", "version", "--format", "{{.Server.Version}}"])
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


def fill_xterm(env, lines: int = 40) -> None:
    """Put realistic text content on the desktop: focus the xterm the image boots
    (80x24 at +20+20) and fill it, so codec benchmarks see a real UI frame rather
    than a near-empty desktop."""
    env.click(x=120, y=120)
    cmd = (
        f"for i in $(seq 1 {lines}); do "
        "printf '%4d lorem ipsum dolor sit amet, consectetur adipiscing elit %s\\n'"
        " $i $RANDOM; done"
    )
    env.type_text(cmd)
    env.key("Return")
    time.sleep(1.5)  # let the shell paint


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


def new_axes(ncols: int = 1, *, height: float = 4.2, width: float = 6.4, nrows: int = 1):
    """Consistent matplotlib axes for every suite figure."""
    style()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(nrows, ncols, figsize=(width * ncols, height * nrows))
    for ax in fig.axes:
        ax.grid(True, which="both", alpha=0.25, linewidth=0.6)
    return fig, axes


def save_plot(fig, suite: str) -> Path:
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    out = PLOTS_DIR / f"{suite}.png"
    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return out


def now_ms() -> float:
    return time.perf_counter() * 1000.0
