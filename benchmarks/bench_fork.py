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

**Pool mode** (S4b): ``SHINKEN_BENCH_FORK_MODE=pool`` runs the same protocol against
a provider with the opt-in warm pool (``warm_pool_size`` =
``SHINKEN_BENCH_POOL_K``, default 8): restore claims a pre-booted base container and
grafts the checkpoint's filesystem delta instead of cold-booting the committed
image. Every replica still verifies the inherited golden marker, and each row
records whether it was a pool graft or a classic fallback (pool empty), so the
numbers never mix silently. Pool mode emits fork_resume_pool.json/.png.

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
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)

MODE = os.environ.get("SHINKEN_BENCH_FORK_MODE", "classic")
POOL_K = int(os.environ.get("SHINKEN_BENCH_POOL_K", "8"))
SUITE = "fork_resume_pool" if MODE == "pool" else "fork_resume"
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
    """One replica. A provider-level infra failure (the contended Docker daemon
    refusing a create under burst) is recorded HONESTLY as a failed row — excluded
    from timing stats and from the verified-inheritance denominator, never faked."""
    from shinken.providers.base import ProviderError

    try:
        return _resume_once_inner(provider, ckpt_id, idx)
    except ProviderError as exc:
        return {
            "replica": idx,
            "infra_failure": str(exc)[:200],
            "inherited_golden_state": False,
        }


def _resume_once_inner(provider, ckpt_id: str, idx: int) -> dict:
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
        "pool_graft": bool(handle.metadata.get("pool_graft")),
    }


def _boot_golden():
    """Like _common.boot(), but pool-mode builds the provider with the warm pool on.
    The golden boot is suite SETUP (not a measured row), so a rare boot-readiness
    flake (~1% under heavy daemon churn — see benchmarks.md §7 caveats) gets one
    bounded retry instead of killing a multi-minute run."""
    from shinken.providers.base import ProviderError, SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    kwargs = {}
    if MODE == "pool":
        kwargs = {
            "warm_pool_size": POOL_K,
            "warm_pool_spec": SandboxSpec(screen_geometry=GEOMETRY),
            # Block briefly for a warm container before falling back to a classic
            # cold boot — both outcomes are recorded per replica (pool_graft).
            "warm_pool_claim_timeout": 0.25,
        }
    provider = DockerLocalProvider(image=IMAGE, name_prefix="shinken-bench", **kwargs)
    for attempt in range(2):
        try:
            handle = provider.create(SandboxSpec(screen_geometry=GEOMETRY))
            break
        except ProviderError as exc:
            if attempt == 1:
                raise
            print(f"golden boot attempt {attempt} failed ({exc}); retrying", flush=True)
    env = provider.connect(handle)
    return provider, handle, env


def _wait_pool_refill(provider, n: int, budget_s: float = 90.0) -> int:
    """Pool mode: wait (bounded) for the replenisher to restock before a fan-out
    round, so the suite measures BOTH steady-state pool service and — when n exceeds
    the pool — the exhaustion fallback. Returns the pool size at round start."""
    import time as _time

    want = min(n, POOL_K)
    deadline = _time.time() + budget_s
    while provider.warm_pool_available() < want and _time.time() < deadline:
        _time.sleep(0.25)
    return provider.warm_pool_available()


def run() -> dict:
    cold = [_cold_boot_once(rep) for rep in range(REPS)]
    print(f"cold boots: {[c['total_ms'] for c in cold]}", flush=True)

    # Golden sandbox: boot once, reach a real state, then measure checkpointing it.
    provider, handle, env = _boot_golden()
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
            pool_at_start = _wait_pool_refill(provider, n) if MODE == "pool" else 0
            t0 = now_ms()
            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                rows = list(
                    pool.map(lambda i: _resume_once(provider, ckpt_id, i), range(n))
                )
            wall = now_ms() - t0
            for row in rows:
                fanout_rows.append({"n": n, **row})
            real = [r for r in rows if "infra_failure" not in r]
            fanout_walls.append(
                {
                    "n": n,
                    "wall_ms": round(wall, 1),
                    "wall_per_replica_ms": round(wall / n, 1),
                    "all_inherited": bool(real)
                    and all(r["inherited_golden_state"] for r in real),
                    "pool_grafts": sum(1 for r in real if r.get("pool_graft")),
                    "infra_failures": len(rows) - len(real),
                    **({"pool_at_start": pool_at_start} if MODE == "pool" else {}),
                }
            )
            print(
                f"fan-out n={n}: wall={wall:.0f} ms, "
                f"inherited={sum(r['inherited_golden_state'] for r in real)}/{len(real)}, "
                f"pool_grafts={sum(1 for r in real if r.get('pool_graft'))}, "
                f"infra_failures={len(rows) - len(real)}",
                flush=True,
            )
    finally:
        env.close()
        provider.destroy(handle)
        if MODE == "pool":
            provider.shutdown_pool()
        provider.cleanup_snapshots()
    payload: dict = {
        "mode": MODE,
        "reps": REPS,
        "fanout": FANOUT,
        "contended_host": os.environ.get("SHINKEN_BENCH_CONTENDED", "0") == "1",
        "datapoints": {
            "cold_boot": cold,
            "checkpoint": checkpoints,
            "fork_resume": fanout_rows,
            "fanout_walls": fanout_walls,
        },
    }
    if MODE == "pool":
        payload["pool"] = {"k": POOL_K, "claim_timeout_s": 0.25}
    return payload


def plot(payload: dict) -> None:
    if payload.get("mode") == "pool":
        plot_pool(payload)
        return
    plot_classic(payload)


def plot_pool(payload: dict) -> None:
    """Pool-mode figure: cold boot vs classic fork vs warm-pool graft (left), and
    per-replica fork→usable across the fan-out, colored by pool hit/miss (right)."""
    from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter

    d = payload["datapoints"]
    fr = [r for r in d["fork_resume"] if "infra_failure" not in r]
    hits = [r["total_ms"] / 1000.0 for r in fr if r.get("pool_graft")]
    misses = [r["total_ms"] / 1000.0 for r in fr if not r.get("pool_graft")]
    cold = [c["total_ms"] / 1000.0 for c in d["cold_boot"]]
    fig, (ax1, ax2) = new_axes(2)

    def mean(vals):
        return sum(vals) / len(vals)

    bars = [("cold boot\n(no state)", cold, PALETTE["neutral"])]
    if misses:
        bars.append(("fork, classic\n(pool empty)", misses, PALETTE["png"]))
    if hits:
        bars.append(("fork, warm-pool\ngraft", hits, PALETTE["delta"]))
    for x, (_, vals, color) in enumerate(bars):
        m = mean(vals)
        ax1.bar(x, m, width=0.62, color=color, zorder=2)
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
        label = f"{m:.2f} s" if m >= 1 else f"{m * 1000:.0f} ms"
        ax1.text(x, max(vals) + max(cold) * 0.03, label, ha="center", fontsize=9)
    ax1.set_xticks(range(len(bars)))
    ax1.set_xticklabels([b[0] for b in bars], fontsize=9)
    ax1.set_ylabel("seconds to a usable replica (mean, min–max)")
    k = payload.get("pool", {}).get("k")
    ax1.set_title(f"Fork→usable: warm-pool graft vs cold paths (K={k})")

    walls = sorted(d["fanout_walls"], key=lambda w: w["n"])
    ns = [w["n"] for w in walls]
    for flag, color, label in (
        (True, PALETTE["delta"], "pool graft"),
        (False, PALETTE["png"], "classic fallback"),
    ):
        xs = [r["n"] for r in fr if bool(r.get("pool_graft")) == flag]
        ys = [r["total_ms"] / 1000.0 for r in fr if bool(r.get("pool_graft")) == flag]
        if xs:
            ax2.scatter(xs, ys, s=22, color=color, alpha=0.75, label=label, zorder=3)
    ax2.plot(
        ns,
        [w["wall_ms"] / 1000.0 for w in walls],
        "o-",
        color=PALETTE["neutral"],
        label="fan-out wall-clock",
        zorder=2,
    )
    _annotate_infra_failures(ax2, walls)
    ax2.set_xscale("log", base=2)
    ax2.xaxis.set_major_locator(FixedLocator(ns))
    ax2.xaxis.set_major_formatter(ScalarFormatter())
    ax2.xaxis.set_minor_locator(NullLocator())
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
    ax2.set_title("Per-replica fork→usable across the fan-out")
    ax2.legend(loc="center right", fontsize=8.5)
    save_plot(fig, "fork_resume_pool")


def _annotate_infra_failures(ax, walls: list[dict]) -> None:
    """Mark fan-out rounds whose wall-clock is dominated by replicas that hit the
    known ~1% boot-readiness flake and timed out (recorded as infra_failure rows) —
    the figure must say so rather than let the point read as fork scaling."""
    for w in walls:
        if w.get("infra_failures"):
            ax.annotate(
                f"{w['infra_failures']} replica(s) hit the known\n"
                "boot flake → 45 s timeout\n(infra_failure rows, excluded)",
                xy=(w["n"], w["wall_ms"] / 1000.0),
                xytext=(0.40, 0.72),
                textcoords="axes fraction",
                fontsize=8,
                ha="center",
                arrowprops=dict(arrowstyle="->", lw=0.8, color="#555555"),
                bbox=dict(boxstyle="round,pad=0.3", fc="#fff8e6", ec="#bba14f", lw=0.7),
            )


def plot_classic(payload: dict) -> None:
    from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter

    d = payload["datapoints"]
    cold = d["cold_boot"]
    fr = [r for r in d["fork_resume"] if "infra_failure" not in r]
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
    # Leg breakdowns: inside the bar when it is tall enough to hold three lines,
    # otherwise beside it (post-S8 the cold-boot bar is ~0.2 s — tiny).
    ymax = max(cold_tot + fork_tot) * 1.45
    for x, (legs, vals) in enumerate(((cold_legs, cold_tot), (fork_legs, fork_tot))):
        m = mean(vals)
        if m > 0.33 * ymax:
            ax1.text(x, m / 2, legs, ha="center", va="center", color="white", fontsize=8)
        else:
            ax1.text(
                x + 0.36, m / 2, legs, ha="left", va="center", color="#333333", fontsize=8
            )
    ax1.text(
        0.98,
        0.98,
        "fork inherits mid-task state —\nskips setup replay\n"
        "(classic fork = boot off the\ncommitted layer, ~0.7 s)",
        transform=ax1.transAxes,
        ha="right",
        va="top",
        fontsize=9,
        bbox=dict(boxstyle="round,pad=0.4", fc="#f6f1f9", ec=PALETTE["accent"], lw=0.8),
    )
    ax1.set_ylim(0, ymax)
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
    _annotate_infra_failures(ax2, walls)
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
    real = [r for r in d["fork_resume"] if "infra_failure" not in r]
    payload["summary"] = {
        "cold_boot_total_ms": summarize([c["total_ms"] for c in d["cold_boot"]]),
        "checkpoint_ms": summarize([c["checkpoint_ms"] for c in d["checkpoint"]]),
        "fork_total_ms": summarize([r["total_ms"] for r in real]),
        "replicas_verified": sum(1 for r in real if r["inherited_golden_state"]),
        "replicas_total": len(real),
        "infra_failures": len(d["fork_resume"]) - len(real),
    }
    if MODE == "pool":
        hits = [r["total_ms"] for r in real if r.get("pool_graft")]
        misses = [r["total_ms"] for r in real if not r.get("pool_graft")]
        payload["summary"]["pool_graft_total_ms"] = summarize(hits)
        payload["summary"]["classic_fallback_total_ms"] = summarize(misses)
        payload["summary"]["pool_hit_rate"] = (
            round(len(hits) / len(real), 3) if real else None
        )
    write_result(SUITE, payload)
    plot(payload)
    ok = (
        payload["summary"]["replicas_total"] > 0
        and payload["summary"]["replicas_verified"] == payload["summary"]["replicas_total"]
    )
    if payload["summary"]["infra_failures"]:
        print(
            f"NOTE: {payload['summary']['infra_failures']} replica(s) lost to provider/"
            "daemon infra failures (recorded in the JSON, excluded from timing stats)"
        )
    print(
        "all materialized forks inherited the golden state"
        if ok
        else "FORK STATE VERIFICATION FAILED"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
