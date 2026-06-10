"""S4 — runtime-state checkpoint / fork / resume on the Docker disk tier.

The differentiating loop: reach a state once, checkpoint it, then mint N live
replicas of that state — vs paying a cold boot per replica. This suite measures
each leg with repetitions, against the local DockerLocalProvider (disk tier:
``docker commit`` + fresh container off the committed layer):

- cold boot:     create (container run + readiness wait) + connect + first
                 observation — the baseline cost of "a usable sandbox",
- checkpoint:    ``provider.checkpoint(handle)`` on a live sandbox with real
                 state (a golden marker file) — REPS repetitions,
- resume/fork:   fan-out N in {1, 2, 4, 8, 16, 32} live replicas from ONE
                 checkpoint (concurrently, like ``run_eval_forked``), per-replica
                 resume + connect + first observation, plus the fan-out
                 wall-clock. Every replica's inherited golden state is verified
                 (``get_file``), so a timing row only counts if the fork was REAL.

Rep counts are env-overridable: ``SHINKEN_BENCH_FORK_REPS`` (default 16) and
``SHINKEN_BENCH_FORK_NS`` (comma-separated, default ``1,2,4,8,16,32``).

Emits benchmarks/results/fork_resume.json and
docs/assets/bench/fork_resume.png.

Run:  python benchmarks/bench_fork.py
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import tempfile
from pathlib import Path

from _common import (
    GEOMETRY,
    IMAGE,
    PALETTE,
    boot,
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)

REPS = int(os.environ.get("SHINKEN_BENCH_FORK_REPS", "16"))
FANOUT = [
    int(n) for n in os.environ.get("SHINKEN_BENCH_FORK_NS", "1,2,4,8,16,32").split(",")
]
MARKER = "golden-state-bench-v1"
GUEST_FILE = "/tmp/shinken_bench_golden.txt"


def _cold_boot_once(rep: int) -> dict:
    from shinken.providers.base import SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(image=IMAGE, name_prefix="shinken-bench")
    t0 = now_ms()
    handle = provider.create(SandboxSpec(screen_geometry=GEOMETRY))
    create_ms = now_ms() - t0
    try:
        t0 = now_ms()
        env = provider.connect(handle)
        connect_ms = now_ms() - t0
        try:
            t0 = now_ms()
            env.screenshot(format="jpeg", quality=80)
            first_obs_ms = now_ms() - t0
        finally:
            env.close()
    finally:
        provider.destroy(handle)
    return {
        "rep": rep,
        "create_ms": round(create_ms, 1),
        "connect_ms": round(connect_ms, 1),
        "first_obs_ms": round(first_obs_ms, 1),
        "total_ms": round(create_ms + connect_ms + first_obs_ms, 1),
    }


def _resume_once(provider, ckpt_id: str, idx: int) -> dict:
    t0 = now_ms()
    handle = provider.resume(ckpt_id)
    resume_ms = now_ms() - t0
    try:
        t0 = now_ms()
        env = provider.connect(handle)
        connect_ms = now_ms() - t0
        try:
            t0 = now_ms()
            env.screenshot(format="jpeg", quality=80)
            first_obs_ms = now_ms() - t0
            out = Path(tempfile.mkdtemp()) / "got.txt"
            env.get_file(GUEST_FILE, str(out))
            inherited = out.read_text().strip() == MARKER
        finally:
            env.close()
    finally:
        provider.destroy(handle)
    return {
        "replica": idx,
        "resume_ms": round(resume_ms, 1),
        "connect_ms": round(connect_ms, 1),
        "first_obs_ms": round(first_obs_ms, 1),
        "total_ms": round(resume_ms + connect_ms + first_obs_ms, 1),
        "inherited_golden_state": inherited,
    }


def run() -> dict:
    cold = [_cold_boot_once(rep) for rep in range(REPS)]
    print(f"cold boots: {[c['total_ms'] for c in cold]}", flush=True)

    # Golden sandbox: boot once, reach a real state, then measure checkpointing it.
    provider, handle, env = boot()
    checkpoints: list[dict] = []
    fanout_rows: list[dict] = []
    fanout_walls: list[dict] = []
    try:
        src = Path(tempfile.mkdtemp()) / "golden.txt"
        src.write_text(MARKER)
        env.put_file(str(src), GUEST_FILE)
        env.click(x=120, y=120)
        env.type_text("echo golden state reached")
        env.key("Return")

        ckpt_id = ""
        for rep in range(REPS):
            t0 = now_ms()
            ckpt_id = provider.checkpoint(handle, name=f"bench-{rep}")
            checkpoints.append({"rep": rep, "checkpoint_ms": round(now_ms() - t0, 1)})
        print(f"checkpoints: {[c['checkpoint_ms'] for c in checkpoints]}", flush=True)

        # Fan-out from the LAST checkpoint (all reps capture the same state).
        for n in FANOUT:
            t0 = now_ms()
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                rows = list(
                    pool.map(lambda i: _resume_once(provider, ckpt_id, i), range(n))
                )
            wall = now_ms() - t0
            for row in rows:
                fanout_rows.append({"n": n, **row})
            fanout_walls.append(
                {
                    "n": n,
                    "wall_ms": round(wall, 1),
                    "wall_per_replica_ms": round(wall / n, 1),
                    "all_inherited": all(r["inherited_golden_state"] for r in rows),
                }
            )
            print(
                f"fan-out n={n}: wall={wall:.0f} ms, inherited={rows[-1]['inherited_golden_state']}",
                flush=True,
            )
    finally:
        env.close()
        provider.destroy(handle)
        provider.cleanup_snapshots()
    return {
        "reps": REPS,
        "fanout": FANOUT,
        "datapoints": {
            "cold_boot": cold,
            "checkpoint": checkpoints,
            "fork_resume": fanout_rows,
            "fanout_walls": fanout_walls,
        },
    }


def plot(payload: dict) -> None:
    from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter

    d = payload["datapoints"]
    cold = d["cold_boot"]
    fr = d["fork_resume"]
    fig, (ax1, ax2) = new_axes(2)

    def mean(vals: list[float]) -> float:
        return sum(vals) / len(vals)

    # Panel 1: plain bars (mean, min-max whiskers) for the three costs. The
    # sub-second legs (connect, first obs) are annotated numerically — on an
    # ~8 s bar they are invisible as stack segments.
    cold_tot = [c["total_ms"] / 1000.0 for c in cold]
    fork_tot = [r["total_ms"] / 1000.0 for r in fr]
    ckpt_s = [c["checkpoint_ms"] / 1000.0 for c in d["checkpoint"]]
    bars = [
        ("cold boot\n(no state)", cold_tot, PALETTE["neutral"], 1.0),
        ("fork from ckpt\n(inherits state)", fork_tot, PALETTE["accent"], 1.0),
        ("checkpoint\n(paid once)", ckpt_s, PALETTE["accent"], 0.45),
    ]
    for x, (_, vals, color, alpha) in enumerate(bars):
        m = mean(vals)
        ax1.bar(x, m, width=0.62, color=color, alpha=alpha, zorder=2)
        ax1.errorbar(
            x,
            m,
            yerr=[[m - min(vals)], [max(vals) - m]],
            fmt="none",
            ecolor="#222222",
            elinewidth=1.1,
            capsize=4,
            zorder=3,
        )
        ax1.text(x, max(vals) + 0.25, f"{m:.2f} s", ha="center", fontsize=9)
    ax1.set_xticks(range(len(bars)))
    ax1.set_xticklabels([b[0] for b in bars], fontsize=9)
    cold_legs = (
        f"docker run {mean([c['create_ms'] for c in cold]) / 1000.0:.2f} s\n"
        f"connect {mean([c['connect_ms'] for c in cold]):.0f} ms\n"
        f"first obs {mean([c['first_obs_ms'] for c in cold]):.0f} ms"
    )
    fork_legs = (
        f"resume {mean([r['resume_ms'] for r in fr]) / 1000.0:.2f} s\n"
        f"connect {mean([r['connect_ms'] for r in fr]):.0f} ms\n"
        f"first obs {mean([r['first_obs_ms'] for r in fr]):.0f} ms"
    )
    ax1.text(
        0,
        mean(cold_tot) / 2,
        cold_legs,
        ha="center",
        va="center",
        color="white",
        fontsize=8,
    )
    ax1.text(
        1,
        mean(fork_tot) / 2,
        fork_legs,
        ha="center",
        va="center",
        color="white",
        fontsize=8,
    )
    ax1.text(
        0.98,
        0.98,
        "fork inherits mid-task state —\nskips setup replay, not the boot\n"
        "(disk tier: fork time ≈ boot time)",
        transform=ax1.transAxes,
        ha="right",
        va="top",
        fontsize=9,
        bbox=dict(boxstyle="round,pad=0.4", fc="#f6f1f9", ec=PALETTE["accent"], lw=0.8),
    )
    ax1.set_ylim(0, max(cold_tot + fork_tot) * 1.38)
    ax1.set_ylabel("seconds to a usable sandbox (mean, min–max)")
    ax1.set_title("Cold boot vs disk-tier fork")

    # Panel 2: fan-out scaling, with the per-replica spread as a visible band.
    walls = sorted(d["fanout_walls"], key=lambda w: w["n"])
    ns = [w["n"] for w in walls]
    per = {n: [r["total_ms"] / 1000.0 for r in fr if r["n"] == n] for n in ns}
    ns_band = [n for n in ns if per[n]]
    ax2.fill_between(
        ns_band,
        [min(per[n]) for n in ns_band],
        [max(per[n]) for n in ns_band],
        color=PALETTE["accent"],
        alpha=0.18,
        linewidth=0,
    )
    band_means = [mean(per[n]) for n in ns_band]
    ax2.errorbar(
        ns_band,
        band_means,
        yerr=[
            [m - min(per[n]) for m, n in zip(band_means, ns_band)],
            [max(per[n]) - m for m, n in zip(band_means, ns_band)],
        ],
        fmt="^:",
        markersize=5,
        color=PALETTE["accent"],
        ecolor=PALETTE["accent"],
        capsize=3,
        label="per-replica total (min–max)",
    )
    ax2.plot(
        ns,
        [w["wall_ms"] / 1000.0 for w in walls],
        "o-",
        color=PALETTE["neutral"],
        label="fan-out wall-clock",
    )
    ax2.plot(
        ns,
        [w["wall_per_replica_ms"] / 1000.0 for w in walls],
        "s--",
        color=PALETTE["accent"],
        label="wall / replica (amortized)",
    )
    ax2.set_xscale("log", base=2)
    ax2.xaxis.set_major_locator(FixedLocator(ns))
    ax2.xaxis.set_major_formatter(ScalarFormatter())
    ax2.xaxis.set_minor_locator(NullLocator())
    ax2.set_ylim(0, max(w["wall_ms"] for w in walls) / 1000.0 * 1.18)
    verified = sum(1 for r in fr if r["inherited_golden_state"])
    ax2.text(
        0.5,
        0.99,
        f"every replica verified to inherit the golden state ({verified}/{len(fr)})",
        transform=ax2.transAxes,
        ha="center",
        va="top",
        fontsize=10,
        color="#333333",
    )
    ax2.set_xlabel("N replicas forked from one checkpoint")
    ax2.set_ylabel("seconds")
    ax2.set_title("Fork fan-out from one checkpoint")
    ax2.legend(loc="center right", fontsize=8.5)
    save_plot(fig, "fork_resume")


def main() -> int:
    payload = run()
    d = payload["datapoints"]
    payload["summary"] = {
        "cold_boot_total_ms": summarize([c["total_ms"] for c in d["cold_boot"]]),
        "checkpoint_ms": summarize([c["checkpoint_ms"] for c in d["checkpoint"]]),
        "fork_total_ms": summarize([r["total_ms"] for r in d["fork_resume"]]),
        "replicas_verified": sum(
            1 for r in d["fork_resume"] if r["inherited_golden_state"]
        ),
        "replicas_total": len(d["fork_resume"]),
    }
    write_result("fork_resume", payload)
    plot(payload)
    ok = payload["summary"]["replicas_verified"] == payload["summary"]["replicas_total"]
    print(
        "all forks inherited the golden state"
        if ok
        else "FORK STATE VERIFICATION FAILED"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
