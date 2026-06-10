"""S6 — client-plane scale: one process, up to 1024 concurrent sessions.

The N=64/256/1024 client-side gates from
docs/engineering/many-sandbox-concurrency.md, measured. Real local sandboxes are
host-RAM-bound well below 1024 (S5 measures that envelope), so this suite isolates
the CLIENT plane — the half of "manage 1024 sandboxes" that is substrate-agnostic —
against mock ``shinkend`` servers running in SEPARATE PROCESSES (4 server workers;
out-of-process so the client's GIL/loop never shares cycles with the servers it is
measuring against). The mocks speak the real ACI handshake + action/observation
frames over real loopback WebSockets through the real SDK; the only synthetic part
is the frame payload, sized to three operating points measured by S1/S5 and B1:

- 13 KiB  ~= JPEG q50 @512   (model-input-res lever, S1)
- 48 KiB  ~= JPEG q80 @1024  (the S5 fan-out operating point)
- 87 KiB  ~= JPEG q80 @1080p (the B1 remote full-res point)

Three measurement blocks:

1. **async fan-out** (the canonical concurrent path: ``aconnect`` + ``asyncio.gather``
   on ONE event loop, ``ping_jitter`` engaged at N>=256): for N in {16, 64, 256, 1024}
   x payload ladder, ROUNDS synchronized observe-all rounds — per-observation latency,
   round wall, aggregate decoded throughput, client RSS/threads/CPU-cores.
2. **sustained window**: N=1024 @ 48 KiB, continuous rounds for SUSTAIN_S seconds —
   sustained frames/s and decoded Mbps from one client process.
3. **thread-model contrast**: the sync facade spends one OS thread per session
   (measured at N in {64, 256}; 1024 sync threads is exactly the anti-pattern this
   plot exists to retire) vs ``SharedLoop`` holding N in {256, 1024} on one thread.

Emits benchmarks/results/client_scale.json and
docs/assets/bench/client_scale.png.

Run:  python benchmarks/bench_client_scale.py
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import multiprocessing as mp
import os
import resource
import subprocess
import sys
import threading

from _common import (
    image_bytes,
    PALETTE,
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)

NS = [int(n) for n in os.environ.get("SHINKEN_BENCH_NS", "16,64,256,1024").split(",")]
ROUNDS = int(os.environ.get("SHINKEN_BENCH_ROUNDS", "10"))
SUSTAIN_S = float(os.environ.get("SHINKEN_BENCH_SUSTAIN_S", "20"))
SERVER_WORKERS = 4
CONNECT_PARALLELISM = 256
PING_JITTER_FROM_N = 256  # docstring recommendation: jitter for fleets of 256+

# payload ladder: decoded bytes per frame, keyed by (quality, max_long_edge) the
# client sends — the mock picks the template that matches the request.
PAYLOADS = {
    (50, 512): ("q50@512", 13 * 1024),
    (80, 1024): ("q80@1024", 48 * 1024),
    (80, None): ("q80@full", 87 * 1024),
}


def _raise_nofile(target: int = 16384) -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(target, hard), hard))


# ---------------------------------------------------------------- mock servers
# Top-level (spawn-picklable). Each worker owns one asyncio loop serving k ports.


def _server_worker(q: "mp.Queue[list[str]]", n_ports: int) -> None:
    _raise_nofile()
    from websockets.asyncio.server import serve

    welcome = json.dumps(
        {
            "type": "welcome",
            "v": 0,
            "server": {"platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click", "screenshot"],
                "targets": ["point_px"],
                "observation_types": ["screenshot"],
                "max_long_edge": 2576,
            },
        }
    )
    # Pre-render one observation template per payload size: PRE + call_id + POST,
    # so the per-request server cost is one string concat, not a 100 KiB json.dumps.
    templates = {}
    for (q_, edge), (label, nbytes) in PAYLOADS.items():
        ref = base64.b64encode(os.urandom(nbytes)).decode()
        head = '{"type":"observation","obs_id":"o","cause":"'
        tail = (
            '","image":{"ref":"' + ref + '","w":1280,"h":800,'
            '"scope":"screen","format":"jpeg"}}'
        )
        templates[(q_, edge)] = (head, tail)

    async def handler(ws):
        async for raw in ws:
            m = json.loads(raw)
            t = m.get("type")
            if t == "hello":
                await ws.send(welcome)
            elif t == "action":
                cid = m.get("call_id", "")
                action = m.get("action") or {}
                if action.get("verb") == "screenshot":
                    # SDK act() flattens kwargs into the action object itself
                    key = (action.get("quality"), action.get("max_long_edge"))
                    head, tail = templates.get(key) or templates[(80, 1024)]
                    await ws.send(head + cid + tail)
                else:
                    await ws.send(
                        json.dumps({"type": "ack", "call_id": cid, "ok": True})
                    )

    async def boot() -> None:
        import socket

        addrs = []
        for _ in range(n_ports):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            await serve(handler, "127.0.0.1", port, max_size=2**21)
            addrs.append(f"127.0.0.1:{port}")
        q.put(addrs)
        await asyncio.Event().wait()  # serve until terminated

    asyncio.run(boot())


def _start_servers(total: int) -> tuple[list[str], list[mp.Process]]:
    ctx = mp.get_context("spawn")
    q: mp.Queue = ctx.Queue()
    per = total // SERVER_WORKERS
    procs = [
        ctx.Process(target=_server_worker, args=(q, per), daemon=True)
        for _ in range(SERVER_WORKERS)
    ]
    for p in procs:
        p.start()
    addrs: list[str] = []
    for _ in procs:
        addrs.extend(q.get(timeout=60))
    return addrs, procs


# ---------------------------------------------------------------- measurement


def _proc_rss_mib() -> float:
    out = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True
    )
    try:
        return round(int(out.stdout.strip()) / 1024.0, 1)
    except ValueError:
        return 0.0


def _cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


async def _observe(session, quality: int, edge: int | None) -> tuple[float, int]:
    kwargs = {"format": "jpeg", "quality": quality}
    if edge is not None:
        kwargs["max_long_edge"] = edge
    t0 = now_ms()
    reply = await session.act("screenshot", **kwargs)
    ms = now_ms() - t0
    raw = image_bytes(reply)
    return ms, len(raw)


async def _connect_all(addrs: list[str]):
    import shinken

    jitter = 8.0 if len(addrs) >= PING_JITTER_FROM_N else 0.0
    sem = asyncio.Semaphore(CONNECT_PARALLELISM)

    async def one(a: str):
        async with sem:
            return await shinken.aconnect(a, ping_jitter=jitter)

    t0 = now_ms()
    sessions = await asyncio.gather(*(one(a) for a in addrs))
    return sessions, now_ms() - t0


async def _async_block(addrs: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    tiers: list[dict] = []
    rounds: list[dict] = []
    lat_samples: list[dict] = []
    for n in NS:
        gc.collect()
        sessions, connect_wall = await _connect_all(addrs[:n])
        try:
            for (q_, edge), (label, nbytes) in PAYLOADS.items():
                cpu0, t_tier = _cpu_s(), now_ms()
                lats: list[float] = []
                total_bytes = 0
                for rnd in range(ROUNDS):
                    t0 = now_ms()
                    results = await asyncio.gather(
                        *(_observe(s, q_, edge) for s in sessions)
                    )
                    wall = now_ms() - t0
                    lats.extend(ms for ms, _ in results)
                    rbytes = sum(b for _, b in results)
                    total_bytes += rbytes
                    rounds.append(
                        {
                            "n": n,
                            "payload": label,
                            "round": rnd,
                            "wall_ms": round(wall, 1),
                            "mbytes": round(rbytes / 2**20, 2),
                        }
                    )
                tier_wall_s = (now_ms() - t_tier) / 1000.0
                cpu_cores = (_cpu_s() - cpu0) / tier_wall_s if tier_wall_s > 0 else 0.0
                tiers.append(
                    {
                        "n": n,
                        "payload": label,
                        "payload_kib": round(nbytes / 1024, 1),
                        "connect_wall_ms": round(connect_wall, 1),
                        "observe_ms": summarize(lats),
                        "agg_frames_per_s": round(len(lats) / tier_wall_s, 1),
                        "agg_decoded_mbps": round(
                            total_bytes * 8 / 2**20 / tier_wall_s, 1
                        ),
                        "proc_rss_mib": _proc_rss_mib(),
                        "threads": threading.active_count(),
                        "client_cpu_cores": round(cpu_cores, 2),
                    }
                )
                # reservoir for CDF plotting: every k-th latency, capped at 300
                step = max(1, len(lats) // 300)
                lat_samples.append(
                    {
                        "n": n,
                        "payload": label,
                        "ms": [round(v, 3) for v in lats[::step]][:300],
                    }
                )
                print(
                    f"async n={n:4d} {label:9s}: round p50 "
                    f"{summarize([r['wall_ms'] for r in rounds if r['n'] == n and r['payload'] == label])['p50']:7.1f} ms, "
                    f"{tiers[-1]['agg_decoded_mbps']:7.1f} Mbps decoded, "
                    f"rss {tiers[-1]['proc_rss_mib']:6.1f} MiB, "
                    f"threads {tiers[-1]['threads']}",
                    flush=True,
                )
        finally:
            await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)
    return tiers, rounds, lat_samples


async def _sustained_block(addrs: list[str]) -> dict:
    n = NS[-1]
    q_, edge = 80, 1024
    label, nbytes = PAYLOADS[(q_, edge)]
    sessions, _ = await _connect_all(addrs[:n])
    try:
        lats: list[float] = []
        walls: list[float] = []
        total_bytes = 0
        cpu0, t0 = _cpu_s(), now_ms()
        while (now_ms() - t0) / 1000.0 < SUSTAIN_S:
            tr = now_ms()
            results = await asyncio.gather(*(_observe(s, q_, edge) for s in sessions))
            walls.append(now_ms() - tr)
            lats.extend(ms for ms, _ in results)
            total_bytes += sum(b for _, b in results)
        wall_s = (now_ms() - t0) / 1000.0
        cpu_cores = (_cpu_s() - cpu0) / wall_s
        out = {
            "n": n,
            "payload": label,
            "window_s": round(wall_s, 1),
            "rounds": len(walls),
            "frames": len(lats),
            "sustained_frames_per_s": round(len(lats) / wall_s, 1),
            "sustained_decoded_mbps": round(total_bytes * 8 / 2**20 / wall_s, 1),
            "round_wall_ms": summarize(walls),
            "observe_ms": summarize(lats),
            "client_cpu_cores": round(cpu_cores, 2),
            "proc_rss_mib": _proc_rss_mib(),
            "threads": threading.active_count(),
        }
        print(
            f"sustained n={n} {label}: {out['sustained_frames_per_s']} frames/s, "
            f"{out['sustained_decoded_mbps']} Mbps decoded over {out['window_s']} s "
            f"({out['client_cpu_cores']} cores)",
            flush=True,
        )
        return out
    finally:
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)


def _thread_model_block(addrs: list[str]) -> list[dict]:
    import shinken

    rows: list[dict] = []

    def loop_threads() -> int:
        return sum(
            1
            for t in threading.enumerate()
            if t.name == "shinken-loop" and t.is_alive()
        )

    shared_ns = [n for n in (256, 1024) if n <= len(addrs)] or [len(addrs)]
    sync_ns = [n for n in (64, 256) if n <= len(addrs)] or [len(addrs)]
    for n in shared_ns:
        gc.collect()
        with shinken.SharedLoop() as sh:
            envs = [shinken.connect(a, loop=sh) for a in addrs[:n]]
            t0 = now_ms()
            for e in envs:
                e.act("screenshot", format="jpeg", quality=50, max_long_edge=512)
            wall = now_ms() - t0
            rows.append(
                {
                    "mode": "shared_loop",
                    "n": n,
                    "loop_threads": loop_threads(),
                    "serial_observe_all_ms": round(wall, 1),
                    "proc_rss_mib": _proc_rss_mib(),
                }
            )
            for e in envs:
                e.close()
        print(f"shared_loop n={n}: threads={rows[-1]['loop_threads']}", flush=True)
    for n in sync_ns:
        gc.collect()
        base = loop_threads()
        envs = [shinken.connect(a) for a in addrs[:n]]
        t0 = now_ms()
        for e in envs:
            e.act("screenshot", format="jpeg", quality=50, max_long_edge=512)
        wall = now_ms() - t0
        rows.append(
            {
                "mode": "sync_facade",
                "n": n,
                "loop_threads": loop_threads() - base,
                "serial_observe_all_ms": round(wall, 1),
                "proc_rss_mib": _proc_rss_mib(),
            }
        )
        for e in envs:
            e.close()
        print(f"sync_facade n={n}: threads={rows[-1]['loop_threads']}", flush=True)
    return rows


def run() -> dict:
    _raise_nofile()
    addrs, procs = _start_servers(NS[-1])
    try:
        tiers, rounds, lat_samples = asyncio.run(_async_block(addrs))
        sustained = asyncio.run(_sustained_block(addrs))
        thread_model = _thread_model_block(addrs)
    finally:
        for p in procs:
            p.terminate()
    observations = sum(t["observe_ms"]["n"] for t in tiers) + sustained["frames"]
    return {
        "ns": NS,
        "rounds_per_tier": ROUNDS,
        "payloads_kib": {
            label: round(b / 1024, 1) for (_, _), (label, b) in PAYLOADS.items()
        },
        "server_workers": SERVER_WORKERS,
        "observations_measured": observations,
        "datapoints": {
            "tiers": tiers,
            "rounds": rounds,
            "latency_samples": lat_samples,
            "sustained": sustained,
            "thread_model": thread_model,
        },
    }


def plot(payload: dict) -> None:
    from matplotlib.ticker import FixedLocator, NullLocator, ScalarFormatter

    def log_axis(ax, which: str, ticks: list[int], base: int = 2) -> None:
        """Log scale whose labeled ticks sit exactly under the plotted data."""
        axis = ax.xaxis if which == "x" else ax.yaxis
        (ax.set_xscale if which == "x" else ax.set_yscale)("log", base=base)
        axis.set_major_locator(FixedLocator(ticks))
        fmt = ScalarFormatter()
        fmt.set_scientific(False)
        axis.set_major_formatter(fmt)
        axis.set_minor_locator(NullLocator())

    d = payload["datapoints"]
    ns = payload["ns"]
    labels = list(payload["payloads_kib"])
    # All three payloads are JPEG operating points: shades of the jpeg blue,
    # disambiguated by marker (light->dark as payload grows).
    shades = ["#7fb3d5", PALETTE["jpeg"], "#1a5276"]
    markers = ["o", "s", "^"]
    pstyle = {lbl: (shades[i], markers[i]) for i, lbl in enumerate(labels)}

    fig, axes = new_axes(2, nrows=2)
    (ax_a, ax_b), (ax_c, ax_d) = axes

    # (a) observe-all round wall vs N, per payload — log-log, min-max band
    for label in labels:
        color, marker = pstyle[label]
        per_n = [
            sorted(
                r["wall_ms"]
                for r in d["rounds"]
                if r["n"] == n and r["payload"] == label
            )
            for n in ns
        ]
        p50s = [w[len(w) // 2] for w in per_n]
        ax_a.fill_between(
            ns,
            [w[0] for w in per_n],
            [w[-1] for w in per_n],
            color=color,
            alpha=0.18,
            lw=0,
        )
        ax_a.plot(
            ns,
            p50s,
            marker=marker,
            color=color,
            label=f"{label} ({payload['payloads_kib'][label]:g} KiB)",
        )
    log_axis(ax_a, "x", ns)
    log_axis(ax_a, "y", [4, 16, 64, 256, 1024], base=4)
    ax_a.set_xlabel("concurrent sessions N (one process, one event loop)")
    ax_a.set_ylabel("observe-all round wall (ms, log)")
    ax_a.set_title("Observe-all round wall vs N")
    ax_a.legend(loc="upper left")
    ax_a.text(
        0.97,
        0.04,
        f"band = min–max of {payload['rounds_per_tier']} rounds",
        transform=ax_a.transAxes,
        ha="right",
        va="bottom",
        fontsize=10,
        color=PALETTE["neutral"],
    )

    # (b) aggregate decoded throughput vs N, y from 0, sustained point annotated
    for label in labels:
        color, marker = pstyle[label]
        tput = [
            next(
                t["agg_decoded_mbps"]
                for t in d["tiers"]
                if t["n"] == n and t["payload"] == label
            )
            for n in ns
        ]
        ax_b.plot(
            ns,
            tput,
            marker=marker,
            color=color,
            label=f"{label} ({payload['payloads_kib'][label]:g} KiB)",
        )
    s = d["sustained"]
    ax_b.plot(
        [s["n"]],
        [s["sustained_decoded_mbps"]],
        "*",
        color=PALETTE["accent"],
        markersize=15,
        zorder=5,
    )
    ax_b.annotate(
        f"{s['sustained_decoded_mbps']:.0f} Mbps sustained {s['window_s']:.0f} s",
        xy=(s["n"], s["sustained_decoded_mbps"]),
        xytext=(0.42, 0.55),
        textcoords="axes fraction",
        ha="center",
        fontsize=10,
        color=PALETTE["accent"],
        arrowprops=dict(arrowstyle="->", color=PALETTE["accent"], lw=1.0),
    )
    log_axis(ax_b, "x", ns)
    ax_b.set_ylim(bottom=0)
    ax_b.set_xlabel("concurrent sessions N")
    ax_b.set_ylabel("aggregate decoded throughput (Mbps)")
    ax_b.set_title("Client-plane ingest vs N")
    ax_b.legend(loc="upper left")

    # (c) thread cost of holding N sessions
    tm = d["thread_model"]
    async_ns = sorted({t["n"] for t in d["tiers"]})
    ax_c.plot(
        async_ns,
        [1] * len(async_ns),
        "--",
        marker="^",
        color=PALETTE["async"],
        label="async core (1 loop thread)",
    )
    for mode, color, marker, lbl in (
        ("shared_loop", PALETTE["shared"], "s", "SharedLoop (1 thread total)"),
        ("sync_facade", PALETTE["sync"], "o", "sync facade (1 thread/session)"),
    ):
        xs = [r["n"] for r in tm if r["mode"] == mode]
        ys = [r["loop_threads"] for r in tm if r["mode"] == mode]
        ax_c.plot(xs, ys, marker=marker, color=color, label=lbl)
    sync_pts = sorted(
        (r["n"], r["loop_threads"]) for r in tm if r["mode"] == "sync_facade"
    )
    if sync_pts:
        ax_c.annotate(
            "sync facade intentionally not run at 1024\n"
            "(1024 OS threads is the anti-pattern\nthis design retires)",
            xy=sync_pts[-1],
            xytext=(0.97, 0.30),
            textcoords="axes fraction",
            ha="right",
            va="center",
            fontsize=10,
            color=PALETTE["sync"],
            arrowprops=dict(arrowstyle="->", color=PALETTE["sync"], lw=1.0),
        )
    log_axis(ax_c, "x", async_ns)
    log_axis(ax_c, "y", [1, 4, 16, 64, 256, 1024], base=4)
    ax_c.set_xlabel("sessions N held by one client process")
    ax_c.set_ylabel("client OS threads (log)")
    ax_c.set_title("Thread cost of holding N sessions")
    ax_c.legend(loc="upper left")

    # (d) observe latency CDFs at N=1024, one per payload — distribution evidence
    n_max = ns[-1]
    for label in labels:
        color, marker = pstyle[label]
        samples = next(
            (
                sorted(ls["ms"])
                for ls in d["latency_samples"]
                if ls["n"] == n_max and ls["payload"] == label
            ),
            None,
        )
        if not samples:
            continue
        frac = [(i + 1) / len(samples) for i in range(len(samples))]
        ax_d.plot(
            samples,
            frac,
            color=color,
            marker=marker,
            markevery=max(1, len(samples) // 10),
            markersize=5,
            label=f"{label} ({payload['payloads_kib'][label]:g} KiB)",
        )
    ax_d.set_xlim(left=0)
    ax_d.set_ylim(0, 1.02)
    ax_d.set_xlabel(f"per-observation latency at N={n_max} (ms)")
    ax_d.set_ylabel("fraction of observations ≤ x")
    ax_d.set_title(f"Observe latency CDF at N={n_max}")
    ax_d.legend(loc="lower right")

    fig.text(
        0.5,
        -0.005,
        "sessions terminate on out-of-process synthetic ACI peers — real handshake, "
        "WebSocket transport, and SDK; synthetic frame payloads sized to measured codec "
        "operating points (client plane only)",
        ha="center",
        va="top",
        fontsize=10,
        color=PALETTE["neutral"],
        style="italic",
    )
    save_plot(fig, "client_scale")


def main() -> int:
    payload = run()
    write_result("client_scale", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
