"""S10 — fleet-level observation dedup: content-negotiated screenshots over a forked fleet.

The fork-native observation optimization the codec knobs can't give: N replicas
forked from ONE golden checkpoint show near-identical screens, yet a plain
observe-all ships the full frame N times per round. With content negotiation
(`screenshot` + `if_none_match`, runtime `frame_hash` over RAW pixels — codec-
independent) and ONE shared ``shinken.FrameCache`` across the fleet's sessions,
the fleet pays for each distinct screen ONCE per round; every other replica's
observe is answered by a ~200-byte ``not_modified``. Only a runtime that OWNS
fork can line this up: the replicas share pixel identity *by construction*.

Four measured modes per fleet size N in ``SHINKEN_BENCH_DEDUP_NS`` (default 4,8,16),
after booting ONE golden sandbox, checkpointing it (disk tier), resuming N replicas
concurrently (the S4 fan-out), and priming every replica with the SAME deterministic
xterm content:

1. **off** — ``SHINKEN_BENCH_DEDUP_ROUNDS`` sequential observe-all rounds with
   dedup OFF: the baseline bytes.
2. **static (sequential)** — the same number of rounds with dedup ON; mid-way,
   2 replicas type different text (the original divergence event). This is the
   STATIC-CEILING mode: sequential observes give hit-rate a clean read
   (first-touch misses, everything else hits), and an almost-identical fleet is
   the mechanism's best case — labeled as such in the JSON/report, never quoted
   as the trainer-shaped number.
3. **concurrent** — the real trainer shape: the whole fleet observes at once
   (``asyncio.gather`` across async sessions on one loop, fresh shared cache).
   First-touch RACES become misses — several replicas can capture-and-miss
   before the first full frame lands in the shared cache — and that loss is
   measured, not assumed away.
4. **policy-driven divergence** — after 2 warm-up rounds, EVERY replica takes a
   distinct scripted action path each round (not 2-of-N) and keeps diverging;
   the per-round hit rate is the honest decay curve to steady state (a fleet
   whose screens all change every step re-pays full frames every step).

Every replica's inherited state is verified at the configured
``SHINKEN_BENCH_STATE_VERIFY`` levels (default ``marker,pixels,fs`` — see
``_common.py``): golden-marker file read-back, settled ``frame_hash`` equality
against the golden's stored post-prime hash (pixel-level identity), and the
in-guest filesystem-delta digest vs the golden's at checkpoint. Fleet pixel
identity (modal ``frame_hash`` share) is additionally reported per phase — dedup
can only ever win on replicas that REALLY render identical screens.

Bytes moved are summed from the SDK's per-reply ``wire_len`` (real on-the-wire
sizes, binary framing). Replica resume failures on a contended daemon are
recorded as honest ``infra_failure`` rows and the fleet proceeds with survivors.

Emits benchmarks/results/fork_dedup.json and docs/assets/bench/fork_dedup.png.

Run:  python benchmarks/bench_fork_dedup.py
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from _common import (
    GEOMETRY,
    IMAGE,
    PALETTE,
    fs_digest,
    new_axes,
    now_ms,
    save_plot,
    settled_frame_hash,
    state_verify_levels,
    summarize,
    verify_replica_state,
    wait_for_desktop_window,
    write_result,
)

SUITE = "fork_dedup"
FANOUT = [int(n) for n in os.environ.get("SHINKEN_BENCH_DEDUP_NS", "4,8,16").split(",")]
ROUNDS = int(os.environ.get("SHINKEN_BENCH_DEDUP_ROUNDS", "6"))
DIVERGE = int(os.environ.get("SHINKEN_BENCH_DEDUP_DIVERGE", "2"))
POLICY_ROUNDS = int(os.environ.get("SHINKEN_BENCH_DEDUP_POLICY_ROUNDS", "8"))
POLICY_DIVERGE_AT = 2  # warm-up rounds before every replica starts acting
VERIFY_LEVELS = state_verify_levels()
FMT = {"format": "jpeg", "quality": 80}  # the realistic fleet observation codec
MARKER = "golden-state-bench-v1"
GUEST_FILE = "/tmp/shinken_bench_golden.txt"

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


def _prime(env) -> str | None:
    """Type the deterministic content into a replica's xterm and wait for the paint
    to settle (two consecutive identical frame hashes). Returns the settled hash.

    Waits for a FOCUSED desktop window first: a freshly resumed replica answers the
    ACI ~0.2 s after start, while the xterm maps (and takes keyboard focus) up to a
    few seconds later — typing into that window discards every keystroke silently,
    and the fleet then "settles" on the unprimed default desktop (measured; the
    identity metric caught it). The post-Return sleep keeps the settle detector
    from reading two pre-paint frames as 'settled'."""
    wait_for_desktop_window(env)
    env.click(x=120, y=120)
    env.type_text(PRIME_CMD)
    env.key("Return")
    time.sleep(0.5)
    return settled_frame_hash(env, fmt=FMT)


def _resume_replica(provider, ckpt_id: str, idx: int, cache, golden_state: dict):
    """Resume + connect + prime + state-verify one replica; infra failures are
    recorded honestly."""
    from shinken.providers.base import ProviderError

    try:
        t0 = now_ms()
        handle = provider.resume(ckpt_id)
        env = provider.connect(handle, frame_cache=cache)
        ready_ms = now_ms() - t0
        settled_hash = _prime(env)
        verify = verify_replica_state(
            env, VERIFY_LEVELS, golden_state, replica_frame_hash=settled_hash
        )
        row = {
            "replica": idx,
            "ready_ms": round(ready_ms, 1),
            "hash": settled_hash or "",
            "state_verify": verify,
        }
        return row, (handle, env)
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


def _identity(hashes: list[str]) -> dict:
    """The honesty metric: how identical IS the fleet right now? Dedup can only
    win on the replicas whose raw pixels truly match the modal screen."""
    clean = [h for h in hashes if h]
    modal, modal_count = Counter(clean).most_common(1)[0] if clean else ("", 0)
    return {
        "identical_screens": modal_count,
        "distinct_screens": len(set(clean)),
        "modal_hash": modal[:12],
    }


def _reprime(envs: list) -> list[str]:
    """Re-prime the whole fleet to the same deterministic content (concurrently —
    typing is slow) and return the settled hashes."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(envs))) as pool:
        return list(pool.map(lambda e: _prime(e) or "", envs))


def _concurrent_phase(handles: list, n: int) -> dict:
    """Mode 3 — the trainer shape: ROUNDS observe-all rounds where the WHOLE fleet
    observes concurrently (`asyncio.gather` over async sessions on one event loop)
    against a FRESH shared FrameCache. Round 0 exposes the first-touch race: with
    no winner yet, several concurrent observes can capture-and-miss before any
    full frame lands in the cache — the honest concurrent cost vs the sequential
    static ceiling's single first-touch miss."""
    import shinken

    async def inner() -> dict:
        cache = shinken.FrameCache(max_entries=4 * n)
        sessions = await asyncio.gather(
            *(shinken.aconnect(h.addr, token=h.token, frame_cache=cache) for h in handles)
        )
        rounds: list[dict] = []
        try:
            for r in range(ROUNDS):
                t0 = now_ms()
                shots = await asyncio.gather(*(s.screenshot(dedup=True, **FMT) for s in sessions))
                row = {
                    "round": r,
                    "wall_ms": round(now_ms() - t0, 1),
                    "wire_bytes": sum(sh.get("wire_len") or len(sh["bytes"]) for sh in shots),
                    "payload_bytes": sum(len(sh["bytes"]) for sh in shots),
                    "hits": sum(1 for sh in shots if sh.get("deduped")),
                    "observes": len(shots),
                }
                rounds.append(row)
                print(
                    f"n={n} concurrent round {r}: hits {row['hits']}/{row['observes']}, "
                    f"wire {row['wire_bytes'] / 1024:.1f} KiB",
                    flush=True,
                )
        finally:
            await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
        return {
            "rounds": rounds,
            "cache": {
                "hits": cache.hits,
                "misses": cache.misses,
                "hit_rate": cache.hit_rate,
            },
        }

    return asyncio.run(inner())


def _policy_phase(provider, handles: list, n: int) -> dict:
    """Mode 4 — policy-driven divergence: after POLICY_DIVERGE_AT warm-up rounds,
    EVERY replica takes a distinct scripted action path each round (replica- and
    round-unique text into its xterm) and keeps diverging. The per-round hit rate
    is the decay curve to steady state: once every screen changes every round,
    every observe is an honest miss and the fleet re-pays full frames — dedup's
    value collapses exactly where it should. Fresh sessions + fresh shared cache
    so the curve starts from the same first-touch state as the other modes;
    observes stay sequential within a round for a clean hit-rate read."""
    import shinken

    cache = shinken.FrameCache(max_entries=4 * n)
    envs = [provider.connect(h, frame_cache=cache) for h in handles]
    rounds: list[dict] = []
    try:
        for r in range(POLICY_ROUNDS):
            acting = r >= POLICY_DIVERGE_AT
            if acting:
                for i, env in enumerate(envs):
                    env.click(x=120, y=120)
                    env.type_text(f"policy replica {i} round {r} path {i * 31 + r}")
                    env.key("Return")
                time.sleep(1.0)  # let the divergent paints land
            row = {"round": r, "diverged": acting, **_observe_round(envs, dedup=True)}
            rounds.append(row)
            print(
                f"n={n} policy round {r}{' (all-diverge)' if acting else ''}: "
                f"hits {row['hits']}/{row['observes']}, "
                f"wire {row['wire_bytes'] / 1024:.1f} KiB",
                flush=True,
            )
    finally:
        for env in envs:
            env.close()
    return {
        "rounds": rounds,
        "diverge_at": POLICY_DIVERGE_AT,
        "cache": {
            "hits": cache.hits,
            "misses": cache.misses,
            "hit_rate": cache.hit_rate,
        },
    }


def _run_fleet(provider, ckpt_id: str, n: int, golden_state: dict) -> dict:
    """One full fleet experiment at size n: resume + prime + state-verify, then the
    four measured modes (off, sequential static ceiling, concurrent, policy
    divergence)."""
    import shinken

    cache = shinken.FrameCache(max_entries=4 * n)
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(
            pool.map(
                lambda i: _resume_replica(provider, ckpt_id, i, cache, golden_state),
                range(n),
            )
        )
    rows = [r for r, _ in results]
    live = [(r, he) for r, he in results if he is not None]
    envs = [he[1] for _, he in live]
    handles = [he[0] for _, he in live]
    out: dict = {
        "n": n,
        "replicas": rows,
        "infra_failures": n - len(live),
        "state_verify_levels": list(VERIFY_LEVELS),
        "rounds_off": [],
        "rounds_static": [],
    }
    try:
        out["identity"] = _identity([r["hash"] for r, _ in live])
        print(
            f"n={n}: fleet identity {out['identity']['identical_screens']}/{len(envs)} "
            f"on modal hash {out['identity']['modal_hash']}…, "
            f"{out['infra_failures']} infra failure(s)",
            flush=True,
        )

        # Mode 1: dedup off — the baseline bytes.
        for r in range(ROUNDS):
            out["rounds_off"].append({"round": r, **_observe_round(envs, dedup=False)})

        # Mode 2: sequential static ceiling (with the original 2-of-N divergence
        # event mid-way). Labeled: best case, not the trainer-shaped number.
        diverge_at = ROUNDS // 2
        out["static_diverge_at"] = diverge_at
        out["static_diverged_replicas"] = min(DIVERGE, len(envs))
        for r in range(ROUNDS):
            if r == diverge_at:
                for i, env in enumerate(envs[: out["static_diverged_replicas"]]):
                    env.click(x=120, y=120)
                    env.type_text(f"echo replica-{i}-diverged")
                    env.key("Return")
                time.sleep(1.0)  # let the divergent paint land
            row = {
                "round": r,
                "diverged": r >= diverge_at,
                **_observe_round(envs, dedup=True),
            }
            out["rounds_static"].append(row)
            print(
                f"n={n} static round {r}{' (post-divergence)' if r >= diverge_at else ''}: "
                f"hits {row['hits']}/{row['observes']}, wire {row['wire_bytes'] / 1024:.1f} KiB",
                flush=True,
            )
        out["static_cache"] = {
            "hits": cache.hits,
            "misses": cache.misses,
            "hit_rate": cache.hit_rate,
        }

        # Re-converge the fleet (the 2 diverged replicas re-prime too), then the
        # trainer-shaped modes, each from a fresh shared cache.
        out["reprime_identity"] = _identity(_reprime(envs))
        out["concurrent"] = _concurrent_phase(handles, len(handles))
        out["policy"] = _policy_phase(provider, handles, len(handles))
    finally:
        for env in envs:
            env.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(handles))) as pool:
            list(pool.map(provider.destroy, handles))
    return out


def run() -> dict:
    provider, golden = _boot_golden()
    fleets: list[dict] = []
    golden_state: dict = {"marker_file": GUEST_FILE, "marker": MARKER}
    try:
        env = provider.connect(golden)
        try:
            # The golden state the replicas must inherit: a marker file + typed
            # screen content. The fs digest is taken at the checkpoint point (the
            # exact file state a fork materializes); the pixels reference is the
            # golden's OWN settled hash after the same deterministic priming the
            # replicas get (the disk tier re-boots the desktop, so pixel identity
            # is re-established through the shared protocol, not preserved).
            src = Path(tempfile.mkdtemp()) / "golden.txt"
            src.write_text(MARKER)
            env.put_file(str(src), GUEST_FILE)
            wait_for_desktop_window(env)  # typing into an unfocused boot is discarded
            env.click(x=120, y=120)
            env.type_text("echo golden state reached")
            env.key("Return")
            if "fs" in VERIFY_LEVELS:
                golden_state["fs_digest"] = fs_digest(env)
            ckpt_id = provider.checkpoint(golden, name="bench-dedup")
            if "pixels" in VERIFY_LEVELS:
                golden_state["frame_hash"] = _prime(env)
            print(f"golden_state: {golden_state}", flush=True)
        finally:
            env.close()
        for n in FANOUT:
            fleets.append(_run_fleet(provider, ckpt_id, n, golden_state))
    finally:
        provider.destroy(golden)
        provider.cleanup_snapshots()
    return {
        "rounds": ROUNDS,
        "policy_rounds": POLICY_ROUNDS,
        "fanout": FANOUT,
        "codec": FMT,
        "state_verify": list(VERIFY_LEVELS),
        "golden_state": golden_state,
        "contended_host": os.environ.get("SHINKEN_BENCH_CONTENDED", "0") == "1",
        "datapoints": {"fleets": fleets},
    }


def _mean_wire_kib(rounds: list[dict]) -> float:
    return sum(r["wire_bytes"] for r in rounds) / max(1, len(rounds)) / 1024


def plot(payload: dict) -> None:
    fleets = sorted(payload["datapoints"]["fleets"], key=lambda f: f["n"])
    fig, (ax1, ax2, ax3) = new_axes(3)
    colors = (PALETTE["jpeg"], PALETTE["accent"], PALETTE["delta"])

    # Panel 1: bytes moved per observe-all round — off, static ceiling, and the
    # fully diverged policy steady state (rounds where every replica acted).
    xs = range(len(fleets))
    off = [_mean_wire_kib(f["rounds_off"]) for f in fleets]
    static = [_mean_wire_kib(f["rounds_static"]) for f in fleets]
    policy = [_mean_wire_kib([r for r in f["policy"]["rounds"] if r["diverged"]]) for f in fleets]
    width = 0.27
    ax1.bar([x - width for x in xs], off, width, color=PALETTE["png"], label="dedup off")
    ax1.bar(
        xs,
        static,
        width,
        color=PALETTE["delta"],
        label="dedup on, static fleet (ceiling)",
    )
    ax1.bar(
        [x + width for x in xs],
        policy,
        width,
        color=PALETTE["neutral"],
        label="dedup on, every replica diverging",
    )
    for x, (o, s) in enumerate(zip(off, static, strict=False)):
        if s > 0:
            ax1.text(x, max(o, s) * 1.03, f"{o / s:.1f}×", ha="center", fontsize=9)
    ax1.set_xticks(list(xs))
    ax1.set_xticklabels([f"N={f['n']}" for f in fleets])
    ax1.set_ylabel("wire KiB per observe-all round (mean)")
    ax1.set_title("Bytes per round: ceiling vs full divergence")
    ax1.legend(loc="upper left", fontsize=8)

    # Panel 2: the sequential static-ceiling hit-rate curve (2-of-N divergence
    # event) — the mechanism's best case, kept and labeled.
    for f, color in zip(fleets, colors, strict=False):
        rounds = f["rounds_static"]
        ax2.plot(
            [r["round"] for r in rounds],
            [r["hits"] / max(1, r["observes"]) for r in rounds],
            "o-",
            color=color,
            label=f"N={f['n']}",
        )
    if fleets:
        d_at = fleets[0].get("static_diverge_at")
        k = fleets[0].get("static_diverged_replicas")
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
    ax2.set_xlabel("observe-all round (dedup on, sequential)")
    ax2.set_ylabel("dedup hit rate")
    ax2.set_title("Static ceiling: 2-of-N divergence, sequential")
    ax2.legend(loc="lower right", fontsize=9)

    # Panel 3: the trainer-shaped modes — concurrent observes (first-touch races)
    # and policy-driven full divergence (decay to steady state).
    for f, color in zip(fleets, colors, strict=False):
        conc = f["concurrent"]["rounds"]
        ax3.plot(
            [r["round"] for r in conc],
            [r["hits"] / max(1, r["observes"]) for r in conc],
            "s--",
            color=color,
            alpha=0.6,
            label=f"N={f['n']} concurrent",
        )
        pol = f["policy"]["rounds"]
        ax3.plot(
            [r["round"] for r in pol],
            [r["hits"] / max(1, r["observes"]) for r in pol],
            "o-",
            color=color,
            label=f"N={f['n']} policy-diverge",
        )
    if fleets:
        d_at = fleets[0]["policy"].get("diverge_at")
        if d_at is not None:
            ax3.axvline(d_at, color="#888888", linestyle="--", linewidth=1)
            ax3.annotate(
                "every replica acts\n(distinct path), every round",
                xy=(d_at, 0.5),
                xytext=(d_at + 0.3, 0.62),
                fontsize=8.5,
                arrowprops=dict(arrowstyle="->", lw=0.8, color="#555555"),
            )
    ax3.set_ylim(-0.04, 1.06)
    ax3.set_xlabel("observe-all round (dedup on)")
    ax3.set_ylabel("dedup hit rate")
    ax3.set_title("Trainer shape: concurrent races + full divergence")
    ax3.legend(loc="center right", fontsize=7.5)
    save_plot(fig, SUITE)


def _verify_counts(fleets: list[dict]) -> dict:
    """Per-level verified/measurable counts across all live replicas."""
    out: dict = {}
    for level in VERIFY_LEVELS:
        passed = total = 0
        for f in fleets:
            for r in f["replicas"]:
                if "infra_failure" in r:
                    continue
                v = (r.get("state_verify") or {}).get(level)
                if v is not None:
                    total += 1
                    passed += 1 if v else 0
        out[level] = f"{passed}/{total}"
    return out


def main() -> int:
    payload = run()
    fleets = payload["datapoints"]["fleets"]
    total_off = sum(r["wire_bytes"] for f in fleets for r in f["rounds_off"])
    total_static = sum(r["wire_bytes"] for f in fleets for r in f["rounds_static"])
    static_hits = sum(r["hits"] for f in fleets for r in f["rounds_static"])
    static_obs = sum(r["observes"] for f in fleets for r in f["rounds_static"])
    conc_rounds = [r for f in fleets for r in f["concurrent"]["rounds"]]
    conc_r0 = [r for f in fleets for r in f["concurrent"]["rounds"] if r["round"] == 0]
    conc_steady = [r for r in conc_rounds if r["round"] >= 1]
    pol_diverged = [r for f in fleets for r in f["policy"]["rounds"] if r["diverged"]]
    pol_tail = [
        r
        for f in fleets
        for r in f["policy"]["rounds"][-2:]  # steady state: the last 2 rounds
    ]

    def hit_rate(rounds: list[dict]) -> float | None:
        obs = sum(r["observes"] for r in rounds)
        return round(sum(r["hits"] for r in rounds) / obs, 3) if obs else None

    payload["summary"] = {
        "static_ceiling": {
            "note": "sequential observes over an almost-identical fleet — best case",
            "wire_bytes_off_total": total_off,
            "wire_bytes_on_total": total_static,
            "bytes_cut_factor": round(total_off / total_static, 2) if total_static else None,
            "hit_rate": round(static_hits / static_obs, 3) if static_obs else None,
            "round_wall_off_ms": summarize([r["wall_ms"] for f in fleets for r in f["rounds_off"]]),
            "round_wall_on_ms": summarize(
                [r["wall_ms"] for f in fleets for r in f["rounds_static"]]
            ),
        },
        "concurrent": {
            "note": "whole fleet observes at once (asyncio.gather) — first-touch races",
            "round0_hit_rate": hit_rate(conc_r0),
            "round0_misses": sum(r["observes"] - r["hits"] for r in conc_r0),
            "round0_observes": sum(r["observes"] for r in conc_r0),
            "steady_hit_rate": hit_rate(conc_steady),
            "bytes_cut_factor_steady": round(
                (total_off / max(1, len([r for f in fleets for r in f["rounds_off"]])))
                / max(
                    1,
                    sum(r["wire_bytes"] for r in conc_steady) / max(1, len(conc_steady)),
                ),
                2,
            ),
        },
        "policy_divergence": {
            "note": "every replica takes a distinct action path each round",
            "hit_rate_curves": {
                f["n"]: [round(r["hits"] / max(1, r["observes"]), 3) for r in f["policy"]["rounds"]]
                for f in fleets
            },
            "diverged_hit_rate": hit_rate(pol_diverged),
            "steady_hit_rate": hit_rate(pol_tail),
            "diverged_bytes_vs_off": round(
                (sum(r["wire_bytes"] for r in pol_diverged) / max(1, len(pol_diverged)))
                / max(
                    1,
                    total_off / max(1, len([r for f in fleets for r in f["rounds_off"]])),
                ),
                3,
            ),
        },
        "state_verify": _verify_counts(fleets),
        "identity": {
            f["n"]: f"{f['identity']['identical_screens']}/{f['n'] - f['infra_failures']}"
            for f in fleets
        },
        "infra_failures": sum(f["infra_failures"] for f in fleets),
    }
    write_result(SUITE, payload)
    plot(payload)
    s = payload["summary"]
    print(
        f"static ceiling: {s['static_ceiling']['bytes_cut_factor']}× bytes cut, "
        f"hit rate {s['static_ceiling']['hit_rate']}; "
        f"concurrent round0 {s['concurrent']['round0_hit_rate']} → steady "
        f"{s['concurrent']['steady_hit_rate']}; "
        f"policy diverged hit rate {s['policy_divergence']['diverged_hit_rate']} "
        f"(steady {s['policy_divergence']['steady_hit_rate']}); "
        f"state_verify {s['state_verify']}; "
        f"{s['infra_failures']} infra failure(s)"
    )
    marker = s["state_verify"].get("marker", "")
    marker_ok = "marker" not in VERIFY_LEVELS or (
        "/" in marker and marker.split("/")[0] == marker.split("/")[1] != "0"
    )
    ok = static_obs > 0 and static_hits > 0 and bool(conc_rounds) and marker_ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
