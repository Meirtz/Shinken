"""S9 — boot waterfall: where the seconds between ``docker run`` and a usable
sandbox actually go.

The suite instruments ONE cold boot into externally observable milestones, each
probed at 10–20 ms granularity (vs the SDK's historical 200 ms readiness poll),
so the gap between "the guest is actually usable" and "the SDK says it is
usable" is measurable instead of folded into one opaque create() number:

- ``run_accepted``      — ``docker run -d`` returned (daemon accepted the job),
- ``container_started`` — the daemon's ``.State.StartedAt`` timestamp relative
                          to when the run was issued (daemon-side clock),
- ``tcp_accept``        — first successful TCP connect to the published port.
                          CAVEAT: on Docker Desktop a userland proxy owns the
                          host port and may accept before shinkend listens, so
                          this is a lower bound, not proof the guest is up,
- ``ws_handshake``      — first successful ACI handshake (``hello``→``welcome``),
- ``ready``             — the guest-side readiness signal: ``query ready``
                          answered ``ready: true`` (X11 connected + sampled
                          root pixels non-black, computed inside the guest in
                          microseconds). On a runtime without the query the
                          probe falls back to replicating the legacy SDK loop
                          (fresh WS + full-PNG pull + ``docker stats`` every
                          200 ms) and records ``ready_mode`` accordingly,
- ``first_nonblack``    — first frame the CLIENT can verify non-black (a small
                          ``max_long_edge=64`` PNG, so decode noise stays ~0).

A second, separate series times the plain SDK path (``provider.create`` +
``connect`` + first observation) so the figure can mark where ``create()``
returns relative to the guest milestones.

Rep counts: ``SHINKEN_BENCH_BOOT_REPS`` (default 6). Set
``SHINKEN_BENCH_CONTENDED=1`` when other Docker work shares the host; the flag
is recorded in the JSON so contended runs are never mistaken for clean ones.

Emits benchmarks/results/boot_waterfall.json and
docs/assets/bench/boot_waterfall.png. If a tracked
``benchmarks/results/boot_waterfall_baseline.json`` exists (a pre-optimization
run preserved on purpose), the figure renders before vs after.

Run:  python benchmarks/bench_boot_waterfall.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from datetime import datetime, timezone

from _common import (
    image_bytes,
    GEOMETRY,
    IMAGE,
    PALETTE,
    RESULTS_DIR,
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)

REPS = int(os.environ.get("SHINKEN_BENCH_BOOT_REPS", "6"))
CONTENDED = os.environ.get("SHINKEN_BENCH_CONTENDED", "0") == "1"
DEADLINE_S = 90.0

# (json key, figure label) in boot order. tcp_accept is recorded in the JSON but kept
# off the stacked bar (Docker Desktop's port proxy can accept before the guest exists,
# which would render a misleading negative-length segment).
MILESTONES = [
    ("run_accepted_ms", "docker run accepted"),
    ("container_started_ms", "container StartedAt"),
    ("ws_handshake_ms", "WS handshake OK"),
    ("ready_ms", "guest ready signal"),
    ("first_nonblack_ms", "first non-black frame"),
]


def _inspect_started_at_epoch(docker_bin: str, cid: str) -> float | None:
    """``.State.StartedAt`` as a unix epoch (daemon clock), or None if unparseable."""
    import subprocess

    try:
        out = subprocess.run(
            [docker_bin, "inspect", "-f", "{{.State.StartedAt}}", cid],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    raw = out.stdout.strip()
    if out.returncode != 0 or not raw:
        return None
    # RFC3339 with nanoseconds, e.g. 2026-06-11T03:14:15.926535897Z — trim to micros.
    try:
        head, _, frac = raw.partition(".")
        frac = frac.rstrip("Z")[:6].ljust(6, "0")
        dt = datetime.strptime(f"{head}.{frac}", "%Y-%m-%dT%H:%M:%S.%f")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _probe_tcp(host: str, port: int, t0: float, deadline: float) -> float | None:
    """First successful TCP connect to the published port, probed every 10 ms."""
    while now_ms() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return now_ms() - t0
        except OSError:
            time.sleep(0.01)
    return None


def _probe_ws(provider, handle, t0: float, deadline: float):
    """First successful ACI handshake (full ``connect``), retried every 20 ms.
    Returns (live env, elapsed ms)."""
    last: Exception | None = None
    while now_ms() < deadline:
        try:
            env = provider.connect(handle)
            return env, now_ms() - t0
        except Exception as exc:  # not yet listening / mid-upgrade
            last = exc
            time.sleep(0.02)
    raise TimeoutError(f"no ACI handshake within {DEADLINE_S}s: {last}")


def _probe_ready(provider, handle, env, t0: float, deadline: float):
    """Time to the guest-side ready signal. Prefers ``query ready`` (10 ms cadence on
    the ONE live connection); falls back to replicating the legacy provider loop
    (``health()`` — fresh WS + full PNG + ``docker stats`` — every 200 ms) when the
    runtime predates the query. Returns (elapsed_ms|None, mode, polls)."""
    polls = 0
    while now_ms() < deadline:
        try:
            value = env.query("ready")
        except RuntimeError as exc:
            if "unknown query" not in str(exc):
                raise
            # Old runtime: replicate the legacy SDK readiness loop, faithfully.
            while now_ms() < deadline:
                polls += 1
                if provider.health(handle).ready:
                    return now_ms() - t0, "legacy_health_poll", polls
                time.sleep(0.2)
            return None, "legacy_health_poll", polls
        polls += 1
        if isinstance(value, dict) and value.get("ready"):
            return now_ms() - t0, "guest_query", polls
        time.sleep(0.01)
    return None, "guest_query", polls


def _probe_first_nonblack(env, t0: float, deadline: float) -> float | None:
    """First frame the client can verify non-black, at 20 ms cadence. Uses a
    ``max_long_edge=64`` PNG so the pure-python decode adds ~0 to the measurement."""
    from shinken.providers.docker import _png_has_non_black_pixel

    while now_ms() < deadline:
        obs = env.act("screenshot", scope="screen", max_long_edge=64)
        raw = image_bytes(obs)
        if _png_has_non_black_pixel(raw):
            return now_ms() - t0
        time.sleep(0.02)
    return None


def _instrumented_boot(rep: int) -> dict:
    from shinken.providers.base import SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(image=IMAGE, name_prefix="shinken-bench")
    # Bypass the provider's own readiness gate — this suite probes the milestones
    # itself, at finer granularity than create() exposes.
    orig_wait = DockerLocalProvider._wait_ready
    DockerLocalProvider._wait_ready = lambda _self, _handle: None
    t_wall = time.time()
    t0 = now_ms()
    try:
        handle = provider.create(SandboxSpec(screen_geometry=GEOMETRY))
    finally:
        DockerLocalProvider._wait_ready = orig_wait
    row: dict = {"rep": rep, "run_accepted_ms": round(now_ms() - t0, 1)}
    deadline = t0 + DEADLINE_S * 1000.0
    env = None
    try:
        cid = str(handle.metadata.get("container_id") or handle.sandbox_id)
        started = _inspect_started_at_epoch(provider.docker_bin, cid)
        row["container_started_ms"] = (
            round((started - t_wall) * 1000.0, 1) if started else None
        )
        host, port = handle.addr.rsplit(":", 1)
        tcp = _probe_tcp(host, int(port), t0, deadline)
        row["tcp_accept_ms"] = round(tcp, 1) if tcp is not None else None
        env, ws_ms = _probe_ws(provider, handle, t0, deadline)
        row["ws_handshake_ms"] = round(ws_ms, 1)
        ready_ms, mode, polls = _probe_ready(provider, handle, env, t0, deadline)
        row["ready_ms"] = round(ready_ms, 1) if ready_ms is not None else None
        row["ready_mode"] = mode
        row["ready_polls"] = polls
        nb = _probe_first_nonblack(env, t0, deadline)
        row["first_nonblack_ms"] = round(nb, 1) if nb is not None else None
    finally:
        if env is not None:
            env.close()
        provider.destroy(handle)
    return row


def _sdk_create_once(rep: int) -> dict:
    """The plain SDK path, untouched: provider.create() (with its own readiness
    gate) + connect + first observation — what a consumer actually pays."""
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


def run() -> dict:
    instrumented = []
    for rep in range(REPS):
        row = _instrumented_boot(rep)
        instrumented.append(row)
        print(
            f"boot {rep}: ws={row['ws_handshake_ms']} ms, ready={row['ready_ms']} ms "
            f"({row['ready_mode']}), nonblack={row['first_nonblack_ms']} ms",
            flush=True,
        )
    sdk = []
    for rep in range(REPS):
        row = _sdk_create_once(rep)
        sdk.append(row)
        print(f"sdk create {rep}: create={row['create_ms']} ms", flush=True)
    return {
        "reps": REPS,
        "contended_host": CONTENDED,
        "datapoints": {"instrumented": instrumented, "sdk_create": sdk},
    }


def _milestone_p50s(rows: list[dict]) -> dict[str, float]:
    out = {}
    for key, _ in MILESTONES:
        vals = [r[key] for r in rows if r.get(key) is not None]
        if vals:
            out[key] = summarize(vals)["p50"]
    return out


def plot(payload: dict) -> None:
    runs = [("this run", payload)]
    baseline_path = RESULTS_DIR / "boot_waterfall_baseline.json"
    if baseline_path.exists():
        base = json.loads(baseline_path.read_text())
        if base.get("meta", {}).get("generated_utc") != payload.get("meta", {}).get(
            "generated_utc"
        ):
            runs = [
                ("after (push-based readiness)", payload),
                ("before (200 ms poll)", base),
            ]

    fig, ax = new_axes(1, width=9.2, height=1.6 + 1.1 * len(runs))
    seg_colors = [
        "#9aa0a6",
        PALETTE["neutral"],
        PALETTE["jpeg"],
        PALETTE["delta"],
        PALETTE["accent"],
    ]
    labels_done = set()
    ends = []
    for y, (name, pl) in enumerate(runs):
        rows = pl["datapoints"]["instrumented"]
        p50 = _milestone_p50s(rows)
        prev = 0.0
        for (key, label), color in zip(MILESTONES, seg_colors):
            if key not in p50:
                continue
            t = max(p50[key], prev)  # milestones are cumulative; clamp clock jitter
            ax.barh(
                y,
                t - prev,
                left=prev,
                height=0.5,
                color=color,
                edgecolor="white",
                linewidth=0.6,
                label=label if label not in labels_done else None,
                zorder=2,
            )
            labels_done.add(label)
            prev = t
        sdk_p50 = summarize([r["create_ms"] for r in pl["datapoints"]["sdk_create"]])[
            "p50"
        ]
        ax.plot(
            [sdk_p50],
            [y],
            marker="D",
            markersize=8,
            color="#111111",
            zorder=4,
            label="provider.create() returns (p50)" if y == 0 else None,
        )
        ends.append((y, prev, sdk_p50))
    xmax = max(max(e, s) for _, e, s in ends) * 1.06
    for y, end, sdk_p50 in ends:
        ax.text(
            min(end + xmax * 0.012, xmax * 0.55),
            y - 0.34,
            f"guest usable {end / 1000.0:.2f} s · create() returns {sdk_p50 / 1000.0:.2f} s",
            fontsize=9,
            va="bottom",
        )
    ax.set_xlim(0, xmax)
    ax.set_yticks(range(len(runs)))
    ax.set_yticklabels([n for n, _ in runs], fontsize=10)
    ax.set_ylim(len(runs) - 0.4, -0.75)  # inverted, tight rows
    ax.set_xlabel("ms since `docker run` issued (p50 of cumulative milestones)")
    note = "contended host" if payload.get("contended_host") else "quiet host"
    ax.set_title(f"Cold-boot waterfall — docker run $\\to$ usable sandbox ({note})")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), fontsize=8.5, ncols=3)
    save_plot(fig, "boot_waterfall")


def main() -> int:
    payload = run()
    d = payload["datapoints"]
    payload["summary"] = {
        "milestones_p50_ms": _milestone_p50s(d["instrumented"]),
        "ready_mode": d["instrumented"][-1]["ready_mode"]
        if d["instrumented"]
        else None,
        "sdk_create_ms": summarize([r["create_ms"] for r in d["sdk_create"]]),
        "sdk_total_ms": summarize([r["total_ms"] for r in d["sdk_create"]]),
    }
    write_result("boot_waterfall", payload)
    plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
