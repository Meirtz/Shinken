"""S10 — fleet-level observation dedup: content-negotiated screenshots over a forked fleet.

The fork-native observation optimization the codec knobs can't give: N replicas
forked from ONE golden checkpoint show near-identical screens, yet a plain
observe-all ships the full frame N times per round. With content negotiation
(`screenshot` + `if_none_match`, runtime `frame_hash` over RAW pixels — codec-
independent) and ONE shared ``shinken.FrameCache`` across the fleet's sessions,
the fleet pays for each distinct screen ONCE per round; every other replica's
observe is answered by a ~200-byte ``not_modified``. Only a runtime that OWNS
fork can line this up: the replicas share pixel identity *by construction*.

Protocol, per fleet size N in ``SHINKEN_BENCH_DEDUP_NS`` (default 4,8,16):

1. boot ONE golden sandbox, checkpoint it (disk tier), resume N replicas
   concurrently (the S4 fan-out),
2. prime every replica with the SAME deterministic xterm content and verify the
   fleet's actual pixel identity (modal `frame_hash` share — an honesty metric:
   dedup can only ever win on replicas that REALLY render identical screens),
3. run ``SHINKEN_BENCH_DEDUP_ROUNDS`` observe-all rounds with dedup OFF
   (baseline bytes), then the same number with dedup ON (shared FrameCache),
4. mid-way through the ON phase, type DIFFERENT text into 2 replicas — the
   honest divergence curve: those replicas miss once (full frame), then re-dedup
   against their OWN new content while the clean replicas keep hitting.

Observes are issued sequentially within a round (hit-rate then has a clean
read: first-touch misses, everything else hits); bytes moved are summed from
the SDK's per-reply ``wire_len`` (real on-the-wire sizes, binary framing).
Replica resume failures on a contended daemon are recorded as honest
``infra_failure`` rows and the fleet proceeds with the survivors.

Emits benchmarks/results/fork_dedup.json and docs/assets/bench/fork_dedup.png.

Run:  python benchmarks/bench_fork_dedup.py
"""

from __future__ import annotations

import concurrent.futures
import os
import sys
import time
from collections import Counter

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

SUITE = "fork_dedup"
FANOUT = [int(n) for n in os.environ.get("SHINKEN_BENCH_DEDUP_NS", "4,8,16").split(",")]
ROUNDS = int(os.environ.get("SHINKEN_BENCH_DEDUP_ROUNDS", "6"))
DIVERGE = int(os.environ.get("SHINKEN_BENCH_DEDUP_DIVERGE", "2"))
FMT = {"format": "jpeg", "quality": 80}  # the realistic fleet observation codec

# The SAME deterministic content typed into every replica: a content-rich screen
# (the codec actually works) that is byte-identical across the fleet — no $RANDOM,
# no hostname (the provider pins it), no timestamps.
PRIME_CMD = (
    "clear; for i in $(seq 1 30); do "
    "printf '%4d lorem ipsum dolor sit amet, consectetur adipiscing elit\\n' $i; done"
)


def _boot_golden():
    """Boot the golden sandbox (one bounded retry on the known ~1% boot flake)."""
    from shinken.providers.base import ProviderError, SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(image=IMAGE, name_prefix="shinken-bench")
    for attempt in range(2):
        try:
            handle = provider.create(SandboxSpec(screen_geometry=GEOMETRY))
            break
        except ProviderError as exc:
            if attempt == 1:
                raise
            print(f"golden boot attempt {attempt} failed ({exc}); retrying", flush=True)
    return provider, handle


def _prime(env) -> str:
    """Type the deterministic content into a replica's xterm and wait for the paint
    to settle (two consecutive identical frame hashes). Returns the settled hash."""
    env.click(x=120, y=120)
    env.type_text(PRIME_CMD)
    env.key("Return")
    deadline = time.time() + 20.0
    last = None
    while time.time() < deadline:
        time.sleep(0.4)
        h = env.screenshot(**FMT).get("frame_hash")
        if h is not None and h == last:
            return h
        last = h
    return last or ""


def _resume_replica(provider, ckpt_id: str, idx: int, cache):
    """Resume + connect + prime one replica; infra failures are recorded honestly."""
    from shinken.providers.base import ProviderError

    try:
        t0 = now_ms()
        handle = provider.resume(ckpt_id)
        env = provider.connect(handle, frame_cache=cache)
        ready_ms = now_ms() - t0
        settled_hash = _prime(env)
        return {"replica": idx, "ready_ms": round(ready_ms, 1), "hash": settled_hash}, (
            handle,
            env,
        )
    except ProviderError as exc:
        return {"replica": idx, "infra_failure": str(exc)[:200]}, None


def _observe_round(envs: list, dedup: bool) -> dict:
    """One observe-all round (sequential): wall, wire bytes moved, payload bytes
    represented, dedup hits."""
    t0 = now_ms()
    wire = payload = hits = 0
    for env in envs:
        shot = env.screenshot(dedup=dedup, **FMT)
        wire += shot.get("wire_len") or len(shot["bytes"])
        payload += len(shot["bytes"])
        hits += 1 if shot.get("deduped") else 0
    return {
        "wall_ms": round(now_ms() - t0, 1),
        "wire_bytes": wire,
        "payload_bytes": payload,
        "hits": hits,
        "observes": len(envs),
    }


def _run_fleet(provider, ckpt_id: str, n: int) -> dict:
    """One full fleet experiment at size n: resume, prime, OFF rounds, ON rounds
    with the mid-phase divergence event."""
    import shinken

    cache = shinken.FrameCache(max_entries=4 * n)
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(
            pool.map(lambda i: _resume_replica(provider, ckpt_id, i, cache), range(n))
        )
    rows = [r for r, _ in results]
    live = [(r, he) for r, he in results if he is not None]
    envs = [he[1] for _, he in live]
    handles = [he[0] for _, he in live]
    out: dict = {
        "n": n,
        "replicas": rows,
        "infra_failures": n - len(live),
        "rounds_off": [],
        "rounds_on": [],
    }
    try:
        # Honesty metric: how identical IS the fleet? Dedup can only win on the
        # replicas whose raw pixels truly match the modal screen.
        hashes = [r["hash"] for r, _ in live]
        modal, modal_count = Counter(hashes).most_common(1)[0] if hashes else ("", 0)
        out["identical_screens"] = modal_count
        out["distinct_screens"] = len(set(hashes))
        print(
            f"n={n}: fleet identity {modal_count}/{len(envs)} on modal hash {modal[:8]}…, "
            f"{out['infra_failures']} infra failure(s)",
            flush=True,
        )

        for r in range(ROUNDS):
            out["rounds_off"].append({"round": r, **_observe_round(envs, dedup=False)})
        diverge_at = ROUNDS // 2
        out["diverge_at"] = diverge_at
        out["diverged_replicas"] = min(DIVERGE, len(envs))
        for r in range(ROUNDS):
            if r == diverge_at:
                # The honest divergence event: 2 replicas stop matching the fleet.
                for i, env in enumerate(envs[: out["diverged_replicas"]]):
                    env.click(x=120, y=120)
                    env.type_text(f"echo replica-{i}-diverged")
                    env.key("Return")
                time.sleep(1.0)  # let the divergent paint land
            row = {
                "round": r,
                "diverged": r >= diverge_at,
                **_observe_round(envs, dedup=True),
            }
            out["rounds_on"].append(row)
            print(
                f"n={n} dedup round {r}{' (post-divergence)' if r >= diverge_at else ''}: "
                f"hits {row['hits']}/{row['observes']}, wire {row['wire_bytes'] / 1024:.1f} KiB",
                flush=True,
            )
        out["cache"] = {
            "hits": cache.hits,
            "misses": cache.misses,
            "hit_rate": cache.hit_rate,
        }
    finally:
        for env in envs:
            env.close()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(handles))
        ) as pool:
            list(pool.map(provider.destroy, handles))
    return out


def run() -> dict:
    provider, golden = _boot_golden()
    fleets: list[dict] = []
    try:
        env = provider.connect(golden)
        try:
            env.click(x=120, y=120)
            env.type_text("echo golden state reached")
            env.key("Return")
        finally:
            env.close()
        ckpt_id = provider.checkpoint(golden, name="bench-dedup")
        for n in FANOUT:
            fleets.append(_run_fleet(provider, ckpt_id, n))
    finally:
        provider.destroy(golden)
        provider.cleanup_snapshots()
    return {
        "rounds": ROUNDS,
        "fanout": FANOUT,
        "codec": FMT,
        "contended_host": os.environ.get("SHINKEN_BENCH_CONTENDED", "0") == "1",
        "datapoints": {"fleets": fleets},
    }


def plot(payload: dict) -> None:
    fleets = sorted(payload["datapoints"]["fleets"], key=lambda f: f["n"])
    fig, (ax1, ax2) = new_axes(2)

    # Panel 1: bytes moved per observe-all round, dedup off vs on (mean over rounds).
    xs = range(len(fleets))
    off = [
        sum(r["wire_bytes"] for r in f["rounds_off"])
        / max(1, len(f["rounds_off"]))
        / 1024
        for f in fleets
    ]
    on = [
        sum(r["wire_bytes"] for r in f["rounds_on"])
        / max(1, len(f["rounds_on"]))
        / 1024
        for f in fleets
    ]
    width = 0.38
    ax1.bar(
        [x - width / 2 for x in xs], off, width, color=PALETTE["png"], label="dedup off"
    )
    ax1.bar(
        [x + width / 2 for x in xs],
        on,
        width,
        color=PALETTE["delta"],
        label="dedup on (shared FrameCache)",
    )
    for x, (o, d) in enumerate(zip(off, on)):
        if d > 0:
            ax1.text(x, max(o, d) * 1.03, f"{o / d:.1f}×", ha="center", fontsize=10)
    ax1.set_xticks(list(xs))
    ax1.set_xticklabels([f"N={f['n']}" for f in fleets])
    ax1.set_ylabel("wire KiB per observe-all round (mean)")
    ax1.set_title("Forked-fleet observation: bytes per round")
    note = "dedup-on mean over ALL rounds,\nincl. first-touch misses + divergence;\na steady identical fleet rounds at\n~0.1 KiB per replica"
    ax1.text(
        0.02,
        0.80,
        note,
        transform=ax1.transAxes,
        ha="left",
        va="top",
        fontsize=8.5,
        color="#333333",
    )
    ax1.legend(loc="upper left", fontsize=9)

    # Panel 2: per-round hit rate across the ON phase — the divergence curve.
    for f, color in zip(fleets, (PALETTE["jpeg"], PALETTE["accent"], PALETTE["delta"])):
        rounds = f["rounds_on"]
        ax2.plot(
            [r["round"] for r in rounds],
            [r["hits"] / max(1, r["observes"]) for r in rounds],
            "o-",
            color=color,
            label=f"N={f['n']}",
        )
    if fleets:
        d_at = fleets[0].get("diverge_at")
        k = fleets[0].get("diverged_replicas")
        if d_at is not None:
            ax2.axvline(d_at, color="#888888", linestyle="--", linewidth=1)
            ax2.annotate(
                f"{k} replicas type\ndifferent text",
                xy=(d_at, 0.45),
                xytext=(d_at + 0.25, 0.30),
                fontsize=8.5,
                arrowprops=dict(arrowstyle="->", lw=0.8, color="#555555"),
            )
    ax2.set_ylim(-0.04, 1.06)
    ax2.set_xlabel("observe-all round (dedup on)")
    ax2.set_ylabel("dedup hit rate")
    ax2.set_title("Divergence: miss once, re-converge on own state")
    ax2.legend(loc="lower right", fontsize=9)
    save_plot(fig, SUITE)


def main() -> int:
    payload = run()
    fleets = payload["datapoints"]["fleets"]
    total_off = sum(r["wire_bytes"] for f in fleets for r in f["rounds_off"])
    total_on = sum(r["wire_bytes"] for f in fleets for r in f["rounds_on"])
    hits = sum(r["hits"] for f in fleets for r in f["rounds_on"])
    observes = sum(r["observes"] for f in fleets for r in f["rounds_on"])
    payload["summary"] = {
        "wire_bytes_off_total": total_off,
        "wire_bytes_on_total": total_on,
        "bytes_cut_factor": round(total_off / total_on, 2) if total_on else None,
        "dedup_hits": hits,
        "dedup_observes": observes,
        "hit_rate": round(hits / observes, 3) if observes else None,
        "round_wall_off_ms": summarize(
            [r["wall_ms"] for f in fleets for r in f["rounds_off"]]
        ),
        "round_wall_on_ms": summarize(
            [r["wall_ms"] for f in fleets for r in f["rounds_on"]]
        ),
        "identical_screens": {
            f["n"]: f"{f.get('identical_screens', 0)}/{f['n'] - f['infra_failures']}"
            for f in fleets
        },
        "infra_failures": sum(f["infra_failures"] for f in fleets),
    }
    write_result(SUITE, payload)
    plot(payload)
    s = payload["summary"]
    print(
        f"dedup: {s['bytes_cut_factor']}× bytes cut "
        f"({total_off / 1024:.0f} KiB → {total_on / 1024:.0f} KiB), "
        f"hit rate {s['hit_rate']}, {s['infra_failures']} infra failure(s)"
    )
    ok = observes > 0 and hits > 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
