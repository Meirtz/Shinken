"""S5 — local many-sandbox fan-out: one process drives N Docker sandboxes.

The local counterpart of the WAN fan-out (docs/engineering/streaming-bandwidth.md
§3) and a first datapoint for the N=64/256/1024 concurrency plan
(docs/engineering/many-sandbox-concurrency.md): for N in {1, 2, 4, 8, 16}, boot N
local sandboxes, multiplex ALL sync sessions onto one ``shinken.SharedLoop`` (one
background event-loop thread total, not one per sandbox), then run ROUNDS
synchronized rounds of {screenshot JPEG q80 @1024 + click} across all N
concurrently. Records per-sandbox observe latency, per-round wall-clock, host
process RSS + thread count, and the summed guest container RSS — the local
host-resource envelope, where loopback replaces the WAN RTT.

Emits benchmarks/results/local_fanout.json and
docs/engineering/assets/benchmarks/local_fanout.png.

Run:  python benchmarks/bench_fanout.py
"""

from __future__ import annotations

import base64
import concurrent.futures
import os
import subprocess
import sys
import threading

from _common import GEOMETRY, IMAGE, new_axes, now_ms, save_plot, summarize, write_result

NS = [1, 2, 4, 8, 16]
ROUNDS = 10
BOOT_WORKERS = 4  # parallel container boots (bounded so create timings stay sane)


def _proc_rss_mib() -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
    )
    try:
        return round(int(out.stdout.strip()) / 1024.0, 1)  # ps reports KiB
    except ValueError:
        return 0.0


def _observe(env) -> tuple[float, int]:
    t0 = now_ms()
    reply = env.act("screenshot", format="jpeg", quality=80, max_long_edge=1024)
    ms = now_ms() - t0
    raw = base64.b64decode((reply.get("image") or {}).get("ref", ""))
    return ms, len(raw)


def run() -> dict:
    import shinken
    from shinken.providers.base import SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(image=IMAGE, name_prefix="shinken-bench")
    tiers: list[dict] = []
    rounds: list[dict] = []
    obs_rows: list[dict] = []
    for n in NS:
        handles, envs = [], []
        loop = shinken.SharedLoop()
        try:
            t0 = now_ms()
            with concurrent.futures.ThreadPoolExecutor(max_workers=BOOT_WORKERS) as pool:
                handles = list(
                    pool.map(
                        lambda _i: provider.create(SandboxSpec(screen_geometry=GEOMETRY)), range(n)
                    )
                )
            boot_wall_ms = now_ms() - t0

            t0 = now_ms()
            envs = [shinken.connect(h.addr, token=h.token, loop=loop) for h in handles]
            connect_wall_ms = now_ms() - t0

            with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
                for rnd in range(ROUNDS):
                    t0 = now_ms()
                    results = list(pool.map(_observe, envs))
                    wall = now_ms() - t0
                    for idx, (ms, nbytes) in enumerate(results):
                        obs_rows.append(
                            {
                                "n": n,
                                "round": rnd,
                                "sandbox": idx,
                                "ms": round(ms, 3),
                                "bytes": nbytes,
                            }
                        )
                    # one synchronized click round too (input-plane fan-out)
                    t1 = now_ms()
                    list(pool.map(lambda e: e.click(x=10, y=10), envs))
                    click_wall = now_ms() - t1
                    rounds.append(
                        {
                            "n": n,
                            "round": rnd,
                            "observe_wall_ms": round(wall, 1),
                            "click_wall_ms": round(click_wall, 1),
                        }
                    )

            guest_rss = [provider._container_rss(h) for h in handles]
            guest_rss = [g for g in guest_rss if g]
            tiers.append(
                {
                    "n": n,
                    "boot_wall_ms": round(boot_wall_ms, 1),
                    "connect_wall_ms": round(connect_wall_ms, 1),
                    "proc_rss_mib": _proc_rss_mib(),
                    "threads": threading.active_count(),
                    "loop_threads": 1,  # SharedLoop: all N sessions on one loop thread
                    "guest_rss_total_mib": round(sum(guest_rss) / 2**20, 1) if guest_rss else None,
                    "guest_rss_per_sandbox_mib": (
                        round(sum(guest_rss) / len(guest_rss) / 2**20, 1) if guest_rss else None
                    ),
                }
            )
            print(
                f"n={n}: boot {boot_wall_ms:.0f} ms, rss {tiers[-1]['proc_rss_mib']} MiB, "
                f"threads {tiers[-1]['threads']}",
                flush=True,
            )
        finally:
            for env in envs:
                try:
                    env.close()
                except Exception:
                    pass
            loop.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=BOOT_WORKERS) as pool:
                list(pool.map(provider.destroy, handles))
    return {
        "ns": NS,
        "rounds_per_n": ROUNDS,
        "observe": "screenshot jpeg q80 max_long_edge=1024",
        "datapoints": {"tiers": tiers, "rounds": rounds, "observations": obs_rows},
    }


def plot(payload: dict) -> None:
    d = payload["datapoints"]
    ns = payload["ns"]
    fig, (ax1, ax2) = new_axes(2)

    obs = d["observations"]
    p50 = [summarize([o["ms"] for o in obs if o["n"] == n])["p50"] for n in ns]
    p95 = [summarize([o["ms"] for o in obs if o["n"] == n])["p90"] for n in ns]
    wall = [
        summarize([r["observe_wall_ms"] for r in d["rounds"] if r["n"] == n])["p50"] for n in ns
    ]
    click = [summarize([r["click_wall_ms"] for r in d["rounds"] if r["n"] == n])["p50"] for n in ns]
    ax1.plot(ns, wall, "o-", label="observe round wall (p50)")
    ax1.plot(ns, click, "s-", label="click round wall (p50)")
    ax1.plot(ns, p50, "^--", label="per-sandbox observe p50")
    ax1.plot(ns, p95, "v:", label="per-sandbox observe p90")
    ax1.set_xscale("log", base=2)
    ax1.set_xticks(ns, [str(n) for n in ns])
    ax1.set_xlabel("N sandboxes (one process, one SharedLoop thread)")
    ax1.set_ylabel("ms")
    ax1.set_title(f"Fan-out latency vs N — {payload['observe']}")
    ax1.legend(fontsize=8)

    rss = [t["proc_rss_mib"] for t in d["tiers"]]
    threads = [t["threads"] for t in d["tiers"]]
    guest = [t["guest_rss_per_sandbox_mib"] or 0 for t in d["tiers"]]
    ax2.plot(ns, rss, "o-", color="C0", label="host process RSS (MiB)")
    ax2.plot(ns, guest, "^--", color="C2", label="guest RSS / sandbox (MiB)")
    ax2.set_xscale("log", base=2)
    ax2.set_xticks(ns, [str(n) for n in ns])
    ax2.set_xlabel("N sandboxes")
    ax2.set_ylabel("MiB")
    twin = ax2.twinx()
    twin.plot(ns, threads, "s:", color="C1", label="host process threads")
    twin.set_ylabel("threads", color="C1")
    lines, labels = ax2.get_legend_handles_labels()
    l2, lb2 = twin.get_legend_handles_labels()
    ax2.legend(lines + l2, labels + lb2, fontsize=8, loc="upper left")
    ax2.set_title("Host/guest resource envelope vs N")
    save_plot(fig, "local_fanout")


def main() -> int:
    payload = run()
    d = payload["datapoints"]
    payload["summary"] = [
        {
            "n": n,
            "observe_ms": summarize([o["ms"] for o in d["observations"] if o["n"] == n]),
            "observe_wall_ms": summarize(
                [r["observe_wall_ms"] for r in d["rounds"] if r["n"] == n]
            ),
            "click_wall_ms": summarize([r["click_wall_ms"] for r in d["rounds"] if r["n"] == n]),
            **{
                k: t[k]
                for t in d["tiers"]
                if t["n"] == n
                for k in (
                    "boot_wall_ms",
                    "proc_rss_mib",
                    "threads",
                    "guest_rss_per_sandbox_mib",
                )
            },
        }
        for n in payload["ns"]
    ]
    write_result("local_fanout", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
