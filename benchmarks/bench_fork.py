"""S4 — runtime-state checkpoint / fork / resume on the Docker disk tier.

The differentiating loop: reach a state once, checkpoint it, then mint N live
replicas of that state — vs paying a cold boot per replica. This suite measures
each leg with repetitions, against the local DockerLocalProvider (disk tier:
``docker commit`` + fresh container off the committed layer):

- cold boot:     create (container run + readiness wait) + connect + first
                 observation — the baseline cost of "a usable sandbox",
- checkpoint:    ``provider.checkpoint(handle)`` on a live sandbox with real
                 state (a golden marker file) — REPS repetitions,
- resume/fork:   fan-out N in {1, 2, 4, 8} live replicas from ONE checkpoint
                 (concurrently, like ``run_eval_forked``), per-replica resume +
                 connect + first observation, plus the fan-out wall-clock. Every
                 replica's inherited golden state is verified (``get_file``), so
                 a timing row only counts if the fork was REAL.

Emits benchmarks/results/fork_resume.json and
docs/engineering/assets/benchmarks/fork_resume.png.

Run:  python benchmarks/bench_fork.py
"""

from __future__ import annotations

import concurrent.futures
import sys
import tempfile
from pathlib import Path

from _common import GEOMETRY, IMAGE, boot, new_axes, now_ms, save_plot, summarize, write_result

REPS = 8
FANOUT = [1, 2, 4, 8]
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
                rows = list(pool.map(lambda i: _resume_once(provider, ckpt_id, i), range(n)))
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
    d = payload["datapoints"]
    fig, (ax1, ax2) = new_axes(2)

    # Panel 1: cold boot vs fork-resume, stacked mean legs.
    legs = ["provision", "connect", "first obs"]
    cold_means = [
        sum(c["create_ms"] for c in d["cold_boot"]) / len(d["cold_boot"]),
        sum(c["connect_ms"] for c in d["cold_boot"]) / len(d["cold_boot"]),
        sum(c["first_obs_ms"] for c in d["cold_boot"]) / len(d["cold_boot"]),
    ]
    fr = d["fork_resume"]
    fork_means = [
        sum(r["resume_ms"] for r in fr) / len(fr),
        sum(r["connect_ms"] for r in fr) / len(fr),
        sum(r["first_obs_ms"] for r in fr) / len(fr),
    ]
    ckpt_mean = sum(c["checkpoint_ms"] for c in d["checkpoint"]) / len(d["checkpoint"])
    bottoms = [0.0, 0.0]
    for i, leg in enumerate(legs):
        vals = [cold_means[i], fork_means[i]]
        ax1.bar(
            ["cold boot\n(no state)", "fork from checkpoint\n(inherits state)"],
            vals,
            bottom=bottoms,
            label=leg,
            color=f"C{i}",
        )
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax1.axhline(
        ckpt_mean,
        color="C3",
        linestyle="--",
        linewidth=1.2,
        label=f"checkpoint (one-time): {ckpt_mean:.0f} ms",
    )
    for x, total in enumerate(bottoms):
        ax1.text(x, total + 30, f"{total:.0f} ms", ha="center", fontsize=9)
    ax1.set_ylabel("ms (mean)")
    ax1.set_title("Time to a USABLE sandbox: cold boot vs disk-tier fork\n(stacked mean legs)")
    ax1.legend(fontsize=8)

    # Panel 2: fan-out scaling.
    ns = [w["n"] for w in d["fanout_walls"]]
    ax2.plot(
        ns, [w["wall_ms"] / 1000.0 for w in d["fanout_walls"]], "o-", label="fan-out wall-clock"
    )
    ax2.plot(
        ns,
        [w["wall_per_replica_ms"] / 1000.0 for w in d["fanout_walls"]],
        "s--",
        label="wall / replica",
    )
    per_replica = {n: [r["total_ms"] / 1000.0 for r in fr if r["n"] == n] for n in ns}
    ax2.errorbar(
        ns,
        [sum(v) / len(v) for v in per_replica.values()],
        yerr=[
            [sum(v) / len(v) - min(v) for v in per_replica.values()],
            [max(v) - sum(v) / len(v) for v in per_replica.values()],
        ],
        fmt="^:",
        capsize=3,
        label="per-replica total (min..max)",
    )
    ax2.set_xticks(ns)
    ax2.set_xlabel("N replicas forked from one checkpoint")
    ax2.set_ylabel("seconds")
    ax2.set_title(
        "Fork fan-out from one golden checkpoint\n(every replica verified to inherit state)"
    )
    ax2.legend(fontsize=8)
    save_plot(fig, "fork_resume")


def main() -> int:
    payload = run()
    d = payload["datapoints"]
    payload["summary"] = {
        "cold_boot_total_ms": summarize([c["total_ms"] for c in d["cold_boot"]]),
        "checkpoint_ms": summarize([c["checkpoint_ms"] for c in d["checkpoint"]]),
        "fork_total_ms": summarize([r["total_ms"] for r in d["fork_resume"]]),
        "replicas_verified": sum(1 for r in d["fork_resume"] if r["inherited_golden_state"]),
        "replicas_total": len(d["fork_resume"]),
    }
    write_result("fork_resume", payload)
    plot(payload)
    ok = payload["summary"]["replicas_verified"] == payload["summary"]["replicas_total"]
    print("all forks inherited the golden state" if ok else "FORK STATE VERIFICATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
