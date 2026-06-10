"""S12 — first-party baseline vs trycua/cua local paths (measured, fairness-matched).

Both stacks run AS SHIPPED, sequentially on the same host, warm-ups discarded
(same discipline as the OSWorld-loop measurements). Three legs:

- ``shinken``     — this repo's ``DockerLocalProvider`` + ``shinken/sandbox-linux``,
                    screen geometry matched to cua-xfce's 1024x768 default.
- ``cua_docker``  — ``cua-sandbox`` (pinned, see CUA_SANDBOX_PIN) ``DockerRuntime`` +
                    ``trycua/cua-xfce:latest`` — cua's shipped local Linux desktop
                    path ("Local Container" in their docs). Shares the Docker
                    daemon with the shinken leg; legs never run concurrently.
- ``cua_lume``    — ``cua-sandbox`` ``LumeRuntime`` + ``ghcr.io/trycua/macos-tahoe-cua``
                    (a macOS VM on Apple's Virtualization.framework — no Docker
                    in the path, CPU contention only). Optional: requires the
                    ``lume`` CLI serving on :7777 and the multi-GiB base image
                    already pulled; skipped (and recorded as skipped) otherwise.

Identical cell definitions per leg:

- boot → usable   = request new sandbox → connected → first full screenshot
- step            = click(x, y) + full screenshot (the act+observe agent step)
- obs bytes/frame = decoded screenshot payload bytes at each stack's default codec
- state verbs     = whatever runtime-state verbs the stack ships LOCALLY, timed;
                    verbs that do not exist locally are recorded as absent —
                    that absence is a finding, not an omission. (cua's
                    ``Sandbox.snapshot()`` is exercised so the local
                    ``NotImplementedError`` is captured verbatim.)

Run (needs Docker, the shinken image, and ``pip install cua-sandbox==0.1.16``;
the lume leg additionally needs ``lume serve`` and the pulled base image)::

    python benchmarks/bench_baseline_cua.py

Partial reruns merge: ``SHINKEN_BENCH_CUA_LEGS=cua_lume`` re-measures only that
leg and carries the others over from the existing results JSON (each leg keeps
its own ``measured_utc``).

Emits benchmarks/results/baseline_cua.json and docs/assets/bench/baseline_cua.png.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Telemetry off BEFORE any cua import (their default is on).
os.environ.setdefault("CUA_TELEMETRY_ENABLED", "false")

from _common import (  # noqa: E402
    REPO_ROOT,
    new_axes,
    now_ms,
    save_plot,
    summarize,
    write_result,
)

CUA_SANDBOX_PIN = "0.1.16"
CUA_XFCE_IMAGE = "trycua/cua-xfce:latest"
CUA_MACOS_IMAGE = "ghcr.io/trycua/macos-tahoe-cua:latest"
MATCHED_GEOMETRY = "1024x768x24"  # cua-xfce ships VNC_RESOLUTION=1024x768

BOOTS = int(os.environ.get("SHINKEN_BENCH_CUA_BOOTS", "8"))
STEPS = int(os.environ.get("SHINKEN_BENCH_CUA_STEPS", "60"))
STEP_WARMUP = 5
PAUSE_REPS = int(os.environ.get("SHINKEN_BENCH_CUA_PAUSE_REPS", "20"))
LUME_CLONES = int(os.environ.get("SHINKEN_BENCH_CUA_LUME_CLONES", "10"))
LUME_BOOTS = int(os.environ.get("SHINKEN_BENCH_CUA_LUME_BOOTS", "8"))
LUME_CKPTS = int(os.environ.get("SHINKEN_BENCH_CUA_LUME_CKPTS", "3"))
LUME_STEPS = int(os.environ.get("SHINKEN_BENCH_CUA_LUME_STEPS", "30"))
LUME_URL = "http://localhost:7777"
SETTLE_S = 10.0  # desktop settle before the step/obs cells (both stacks; XFCE keeps
# painting wallpaper/panel for seconds after the API reports ready)

MARKER = "golden-state-bench-cua-v1"
GUEST_FILE = "/tmp/shinken_bench_golden.txt"
NAME_PREFIX = "shinken-cuabench"  # distinct from the other suites' "shinken-bench"


def _sweep_containers(spare: str | None = None) -> None:
    """Remove leftovers from THIS suite only (a timed-out create can orphan a
    container); never touches other suites' or other tools' containers.
    ``spare`` protects one live container (the golden sandbox) by id/name prefix."""
    for prefix in (NAME_PREFIX, "cua-bench"):
        out = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name={prefix}"],
            capture_output=True,
            text=True,
        )
        ids = [i for i in out.stdout.split() if not (spare and spare.startswith(i))]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True)


def _attempt(fn, what: str, flakes: list[dict], spare: str | None = None):
    """Run one measurement rep with a single retry. A rep that fails its stack's
    own readiness gate (e.g. a 45 s desktop timeout under background host load)
    is recorded in ``flakes`` — visible in the JSON, never silently dropped —
    and retried once; the retry's timing is the sample. A second failure raises."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — recorded, then one retry
        flakes.append({"what": what, "error": f"{type(exc).__name__}: {exc}"[:300]})
        print(f"flake on {what}; retrying once: {exc}", flush=True)
        _sweep_containers(spare=spare)
        return fn()


async def _attempt_async(fn, what: str, flakes: list[dict]):
    """Async twin of :func:`_attempt`."""
    try:
        return await fn()
    except Exception as exc:  # noqa: BLE001 — recorded, then one retry
        flakes.append({"what": what, "error": f"{type(exc).__name__}: {exc}"[:300]})
        print(f"flake on {what}; retrying once: {exc}", flush=True)
        _sweep_containers()
        return await fn()


# ──────────────────────────────────────────────────────────────────────────────
# Leg 1 — shinken (DockerLocalProvider + shinken/sandbox-linux, matched geometry)
# ──────────────────────────────────────────────────────────────────────────────


def leg_shinken() -> dict:
    from shinken.providers.base import SandboxSpec
    from shinken.providers.docker import DockerLocalProvider

    leg: dict = {"geometry": MATCHED_GEOMETRY}
    flakes: list[dict] = []

    def _boot_once() -> dict:
        provider = DockerLocalProvider(
            image=os.environ.get("SHINKEN_BENCH_IMAGE", "shinken/sandbox-linux"),
            name_prefix=NAME_PREFIX,
        )
        t0 = now_ms()
        handle = provider.create(SandboxSpec(screen_geometry=MATCHED_GEOMETRY))
        create_ms = now_ms() - t0
        try:
            t0 = now_ms()
            env = provider.connect(handle)
            connect_ms = now_ms() - t0
            try:
                t0 = now_ms()
                env.screenshot()  # protocol default: PNG, native size
                first_obs_ms = now_ms() - t0
            finally:
                env.close()
        finally:
            provider.destroy(handle)
        return {
            "create_ms": round(create_ms, 1),
            "connect_ms": round(connect_ms, 1),
            "first_obs_ms": round(first_obs_ms, 1),
            "total_ms": round(create_ms + connect_ms + first_obs_ms, 1),
        }

    # boot → usable (warm-up + BOOTS reps)
    boots: list[dict] = []
    for rep in range(BOOTS + 1):
        row = _attempt(_boot_once, f"shinken boot rep {rep}", flakes)
        if rep == 0:
            continue  # warm-up discarded
        boots.append({"rep": rep, **row})
    leg["boot"] = boots
    print(f"shinken boots: {[b['total_ms'] for b in boots]}", flush=True)

    # one warm sandbox for step loop + obs bytes + state verbs
    provider = DockerLocalProvider(
        image=os.environ.get("SHINKEN_BENCH_IMAGE", "shinken/sandbox-linux"),
        name_prefix=NAME_PREFIX,
    )
    handle = provider.create(SandboxSpec(screen_geometry=MATCHED_GEOMETRY))
    env = provider.connect(handle)
    try:
        import time

        time.sleep(SETTLE_S)  # same settle as the cua legs: a fully painted desktop

        steps: list[dict] = []
        for rep in range(STEPS + STEP_WARMUP):
            x = 100 if rep % 2 == 0 else 220
            t0 = now_ms()
            env.click(x=x, y=140)
            click_ms = now_ms() - t0
            t0 = now_ms()
            shot = env.screenshot()
            shot_ms = now_ms() - t0
            if rep < STEP_WARMUP:
                continue
            steps.append(
                {
                    "rep": rep,
                    "click_ms": round(click_ms, 3),
                    "screenshot_ms": round(shot_ms, 3),
                    "step_ms": round(click_ms + shot_ms, 3),
                    "bytes": len(shot["bytes"]),
                }
            )
        leg["step"] = steps
        leg["obs_default"] = {"codec": "png (protocol default)", "n": len(steps)}

        # JPEG q80 reference cell (our opt-in bandwidth lever) — bytes only
        jpeg_bytes = []
        for _ in range(10):
            shot = env.screenshot(format="jpeg", quality=80)
            jpeg_bytes.append(len(shot["bytes"]))
        leg["obs_jpeg80_bytes"] = summarize([float(b) for b in jpeg_bytes])

        # state verbs: checkpoint a LIVE sandbox, then fork → usable (verified)
        src = Path(tempfile.mkdtemp()) / "golden.txt"
        src.write_text(MARKER)
        env.put_file(str(src), GUEST_FILE)

        checkpoints = []
        ckpt_id = ""
        for rep in range(BOOTS):
            t0 = now_ms()
            ckpt_id = provider.checkpoint(handle, name=f"bench-cua-{rep}")
            checkpoints.append(round(now_ms() - t0, 1))
        leg["checkpoint_ms"] = checkpoints
        print(f"shinken checkpoints: {checkpoints}", flush=True)

        def _fork_once() -> dict:
            t0 = now_ms()
            fhandle = provider.resume(ckpt_id)
            resume_ms = now_ms() - t0
            try:
                t0 = now_ms()
                fenv = provider.connect(fhandle)
                connect_ms = now_ms() - t0
                try:
                    t0 = now_ms()
                    fenv.screenshot()
                    first_obs_ms = now_ms() - t0
                    out = Path(tempfile.mkdtemp()) / "got.txt"
                    fenv.get_file(GUEST_FILE, str(out))
                    inherited = out.read_text().strip() == MARKER
                finally:
                    fenv.close()
            finally:
                provider.destroy(fhandle)
            return {
                "resume_ms": round(resume_ms, 1),
                "connect_ms": round(connect_ms, 1),
                "first_obs_ms": round(first_obs_ms, 1),
                "total_ms": round(resume_ms + connect_ms + first_obs_ms, 1),
                "inherited_golden_state": inherited,
            }

        forks: list[dict] = []
        for rep in range(BOOTS):
            row = _attempt(
                _fork_once, f"shinken fork rep {rep}", flakes, spare=handle.sandbox_id
            )
            forks.append({"rep": rep, **row})
        leg["fork_to_usable"] = forks
        print(f"shinken forks: {[f['total_ms'] for f in forks]}", flush=True)
    finally:
        env.close()
        provider.destroy(handle)
    if flakes:
        leg["flaked_reps_retried"] = flakes
    return leg


# ──────────────────────────────────────────────────────────────────────────────
# Leg 2 — cua local Linux container (DockerRuntime + trycua/cua-xfce:latest)
# ──────────────────────────────────────────────────────────────────────────────


async def _cua_boot_once(Image, Sandbox, name: str) -> dict:
    t0 = now_ms()
    sb = await Sandbox.create(
        Image.linux("ubuntu", "24.04", kind="container"),
        name=name,
        local=True,
        telemetry_enabled=False,
    )
    create_ms = now_ms() - t0
    try:
        t0 = now_ms()
        shot = await sb.screenshot()  # their default: PNG
        first_obs_ms = now_ms() - t0
    finally:
        await sb.destroy()
    return {
        "create_connect_ms": round(create_ms, 1),
        "first_obs_ms": round(first_obs_ms, 1),
        "total_ms": round(create_ms + first_obs_ms, 1),
        "first_obs_bytes": len(shot),
    }


async def leg_cua_docker_async() -> dict:
    from cua_sandbox import Image, Sandbox

    leg: dict = {"image": CUA_XFCE_IMAGE}
    flakes: list[dict] = []

    boots = []
    for rep in range(BOOTS + 1):
        row = await _attempt_async(
            lambda rep=rep: _cua_boot_once(Image, Sandbox, f"cua-bench-boot-{rep}"),
            f"cua boot rep {rep}",
            flakes,
        )
        if rep == 0:
            continue  # warm-up discarded
        boots.append({"rep": rep, **row})
    leg["boot"] = boots
    print(f"cua docker boots: {[b['total_ms'] for b in boots]}", flush=True)

    sb = await Sandbox.create(
        Image.linux("ubuntu", "24.04", kind="container"),
        name="cua-bench-warm",
        local=True,
        telemetry_enabled=False,
    )
    try:
        leg["dimensions"] = list(await sb.get_dimensions())
        await asyncio.sleep(SETTLE_S)  # same settle as the shinken leg

        steps = []
        for rep in range(STEPS + STEP_WARMUP):
            x = 100 if rep % 2 == 0 else 220
            t0 = now_ms()
            await sb.mouse.click(x, 140)
            click_ms = now_ms() - t0
            t0 = now_ms()
            shot = await sb.screenshot()
            shot_ms = now_ms() - t0
            if rep < STEP_WARMUP:
                continue
            steps.append(
                {
                    "rep": rep,
                    "click_ms": round(click_ms, 3),
                    "screenshot_ms": round(shot_ms, 3),
                    "step_ms": round(click_ms + shot_ms, 3),
                    "bytes": len(shot),
                }
            )
        leg["step"] = steps
        leg["obs_default"] = {"codec": "png (SDK default)", "n": len(steps)}

        # JPEG q80 reference cell. Their SDK exposes format/quality, but whether the
        # server honors it depends on the image's bundled computer-server — record
        # whatever actually happens (with trycua/cua-xfce@sha256:3bf85… the server
        # returns PNG regardless and the SDK's magic-byte guard raises ValueError).
        try:
            jpeg_bytes = []
            for _ in range(10):
                shot = await sb.screenshot(format="jpeg", quality=80)
                jpeg_bytes.append(len(shot))
            leg["obs_jpeg80_bytes"] = summarize([float(b) for b in jpeg_bytes])
        except Exception as exc:  # noqa: BLE001 — as-shipped behaviour is the datapoint
            leg["obs_jpeg80"] = {
                "supported": False,
                "raises": f"{type(exc).__name__}: {exc}",
            }
        print(
            f"cua jpeg lever: {leg.get('obs_jpeg80_bytes') or leg.get('obs_jpeg80')}",
            flush=True,
        )

        # snapshot() — exercised so the local behaviour is recorded verbatim
        try:
            await sb.snapshot(name="bench-snapshot")
            leg["snapshot_local"] = {"supported": True}
        except Exception as exc:  # noqa: BLE001 — record whatever it raises
            leg["snapshot_local"] = {
                "supported": False,
                "raises": f"{type(exc).__name__}: {exc}",
            }
        print(f"cua snapshot(): {leg['snapshot_local']}", flush=True)

        # suspend/resume = docker pause/unpause (their only local state verbs
        # on this path — no copy is taken, so this is NOT a checkpoint).
        pauses, resumes = [], []
        for _ in range(PAUSE_REPS):
            t0 = now_ms()
            await Sandbox.suspend("cua-bench-warm", local=True)
            pauses.append(round(now_ms() - t0, 1))
            t0 = now_ms()
            sb = await Sandbox.resume("cua-bench-warm", local=True)
            resumes.append(round(now_ms() - t0, 1))
        leg["suspend_ms"] = pauses
        leg["resume_ms"] = resumes
        print(f"cua pause p50≈{sorted(pauses)[len(pauses) // 2]} ms", flush=True)
    finally:
        await sb.destroy()
    if flakes:
        leg["flaked_reps_retried"] = flakes
    return leg


# ──────────────────────────────────────────────────────────────────────────────
# Leg 3 — cua local macOS VM (LumeRuntime + macos-tahoe-cua, Virtualization.fw)
# ──────────────────────────────────────────────────────────────────────────────


async def _lume_available() -> str | None:
    """Return None if the lume leg can run, else the skip reason."""
    import hashlib

    import httpx

    if shutil.which("lume") is None and not os.path.isfile(
        os.path.expanduser("~/.local/bin/lume")
    ):
        return "lume CLI not installed"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{LUME_URL}/lume/vms")
            if resp.status_code != 200:
                return f"lume serve not healthy (HTTP {resp.status_code})"
    except Exception as exc:  # noqa: BLE001
        return f"lume serve not reachable on :7777 ({type(exc).__name__})"
    base = "cua-base-" + hashlib.sha256(CUA_MACOS_IMAGE.encode()).hexdigest()[:12]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{LUME_URL}/lume/vms/{base}")
            if resp.status_code != 200:
                return f"base image not pulled ({base}; ~23 GiB one-time `lume pull`)"
    except Exception as exc:  # noqa: BLE001
        return f"base VM lookup failed ({type(exc).__name__})"
    return None


async def _lume_clone_probe() -> dict | None:
    """Measure lume's local fork primitive (the stopped-VM clone behind their
    create-from-base path) WITHOUT the ~23 GiB image: create an empty Linux VM
    via the lume CLI, materialize a few GiB into its disk.img so the clone has
    real allocated blocks, then time clones through the same API endpoint their
    ``LumeRuntime.fork`` calls. APFS clonefile is a metadata-CoW operation, so
    this is the mechanism's cost; it is NOT a desktop-workload measurement."""
    import httpx

    name = "shinken-cuabench-lume-probe"
    disk = Path.home() / ".lume" / name / "disk.img"
    materialize_gib = 4
    try:
        subprocess.run(["lume", "delete", name, "--force"], capture_output=True)
        out = subprocess.run(
            [
                "lume",
                "create",
                name,
                "--os",
                "linux",
                "--cpu",
                "2",
                "--memory",
                "2GB",
                "--disk-size",
                "12GB",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if out.returncode != 0 or not disk.exists():
            return None
        with open(disk, "r+b") as f:  # materialize blocks inside the sparse image
            chunk = os.urandom(1 << 24)
            for _ in range(materialize_gib * (1 << 30) // len(chunk)):
                f.write(chunk)
        clone_ms: list[float] = []
        async with httpx.AsyncClient(timeout=120) as client:
            for i in range(LUME_CLONES):
                clone = f"{name}-clone-{i}"
                t0 = now_ms()
                resp = await client.post(
                    f"{LUME_URL}/lume/vms/clone", json={"name": name, "newName": clone}
                )
                if resp.status_code >= 400:
                    return None
                clone_ms.append(round(now_ms() - t0, 1))
                subprocess.run(
                    ["lume", "delete", clone, "--force"], capture_output=True
                )
        return {
            "what": "stopped-VM clone via the lume API (same endpoint as LumeRuntime.fork)",
            "vm_disk_gib": 12,
            "materialized_gib": materialize_gib,
            "clone_ms": clone_ms,
            "note": "mechanism probe on an empty Linux VM — not a desktop workload",
        }
    except Exception:  # noqa: BLE001 — the probe is best-effort
        return None
    finally:
        subprocess.run(["lume", "delete", name, "--force"], capture_output=True)


async def leg_cua_lume_async() -> dict:
    import hashlib

    from cua_sandbox import Image, Sandbox
    from cua_sandbox.runtime.lume import LumeRuntime

    skip = await _lume_available()
    if skip:
        print(f"cua lume leg SKIPPED: {skip}", flush=True)
        leg = {"skipped": skip}
        if "not pulled" in skip or "lookup failed" in skip:
            probe = (
                await _lume_clone_probe()
            )  # lume itself works — measure the primitive
            if probe:
                leg["clone_probe"] = probe
                print(f"lume clone probe: {probe['clone_ms']}", flush=True)
        return leg

    base_name = "cua-base-" + hashlib.sha256(CUA_MACOS_IMAGE.encode()).hexdigest()[:12]
    leg: dict = {"image": CUA_MACOS_IMAGE, "base_vm": base_name}
    rt = LumeRuntime()

    # local fork primitive: clone a STOPPED base VM (APFS clonefile). The clone
    # is born stopped — using it still costs a full VM boot (no memory state).
    clones = []
    for i in range(LUME_CLONES):
        name = f"cua-bench-clone-{i}"
        t0 = now_ms()
        await rt.fork(base_name, name)
        clones.append(round(now_ms() - t0, 1))
        subprocess.run(["lume", "delete", name, "--force"], capture_output=True)
    leg["clone_stopped_ms"] = clones
    print(f"lume clones: {clones}", flush=True)

    # boot → usable: their LumeRuntime.start = fork(base) + run + wait-for-ip +
    # computer-server ready; + first screenshot. destroy() deletes the VM.
    boots = []
    for rep in range(LUME_BOOTS + 1):
        name = f"cua-bench-vm-{rep}"
        t0 = now_ms()
        sb = await Sandbox.create(
            Image.macos("26"), name=name, local=True, telemetry_enabled=False
        )
        create_ms = now_ms() - t0
        try:
            t0 = now_ms()
            shot = await sb.screenshot()
            first_obs_ms = now_ms() - t0
        finally:
            await sb.destroy()
        if rep == 0:
            continue  # warm-up discarded
        boots.append(
            {
                "rep": rep,
                "create_connect_ms": round(create_ms, 1),
                "first_obs_ms": round(first_obs_ms, 1),
                "total_ms": round(create_ms + first_obs_ms, 1),
                "first_obs_bytes": len(shot),
            }
        )
        print(f"lume boot {rep}: {boots[-1]['total_ms']} ms", flush=True)
    leg["boot"] = boots

    # one warm VM: step loop + their checkpoint() (= stop → clone → restart;
    # disruptive, memory state is lost — recorded as shipped).
    sb = await Sandbox.create(
        Image.macos("26"), name="cua-bench-vm-warm", local=True, telemetry_enabled=False
    )
    try:
        leg["dimensions"] = list(await sb.get_dimensions())
        await asyncio.sleep(SETTLE_S)  # same settle as the other legs
        steps = []
        for rep in range(LUME_STEPS + STEP_WARMUP):
            x = 100 if rep % 2 == 0 else 220
            t0 = now_ms()
            await sb.mouse.click(x, 140)
            click_ms = now_ms() - t0
            t0 = now_ms()
            shot = await sb.screenshot()
            shot_ms = now_ms() - t0
            if rep < STEP_WARMUP:
                continue
            steps.append(
                {
                    "rep": rep,
                    "click_ms": round(click_ms, 3),
                    "screenshot_ms": round(shot_ms, 3),
                    "step_ms": round(click_ms + shot_ms, 3),
                    "bytes": len(shot),
                }
            )
        leg["step"] = steps
        leg["obs_default"] = {"codec": "png (SDK default)", "n": len(steps)}

        ckpts = []
        for i in range(LUME_CKPTS):
            t0 = now_ms()
            await rt.checkpoint("cua-bench-vm-warm", f"bench-ckpt-{i}")
            ckpts.append(round(now_ms() - t0, 1))
            subprocess.run(
                ["lume", "delete", f"cua-ckpt-bench-ckpt-{i}", "--force"],
                capture_output=True,
            )
            print(f"lume checkpoint {i}: {ckpts[-1]} ms", flush=True)
        leg["checkpoint_running_ms"] = ckpts
    finally:
        await sb.destroy()
    return leg


# ──────────────────────────────────────────────────────────────────────────────
# meta / summary / figure
# ──────────────────────────────────────────────────────────────────────────────


def _versions() -> dict:
    import importlib.metadata

    v: dict = {"cua_sandbox_pin": CUA_SANDBOX_PIN}
    try:
        v["cua_sandbox"] = importlib.metadata.version("cua-sandbox")
    except Exception:  # noqa: BLE001
        v["cua_sandbox"] = "not installed"
    for key, args in {
        "lume": ["lume", "--version"],
        "cua_xfce_digest": [
            "docker",
            "image",
            "inspect",
            CUA_XFCE_IMAGE,
            "--format",
            "{{index .RepoDigests 0}}",
        ],
    }.items():
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=15)
            v[key] = out.stdout.strip() if out.returncode == 0 else "unavailable"
        except Exception:  # noqa: BLE001
            v[key] = "unavailable"
    v["cua_macos_image"] = CUA_MACOS_IMAGE
    return v


def _summary(legs: dict) -> dict:
    out: dict = {}
    for name, leg in legs.items():
        if "skipped" in leg:
            out[name] = {"skipped": leg["skipped"]}
            if leg.get("clone_probe"):
                out[name]["clone_probe_ms"] = summarize(
                    [float(x) for x in leg["clone_probe"]["clone_ms"]]
                )
            continue
        s: dict = {}
        if leg.get("boot"):
            s["boot_to_usable_ms"] = summarize([b["total_ms"] for b in leg["boot"]])
        if leg.get("step"):
            s["step_ms"] = summarize([p["step_ms"] for p in leg["step"]])
            s["click_ms"] = summarize([p["click_ms"] for p in leg["step"]])
            s["screenshot_ms"] = summarize([p["screenshot_ms"] for p in leg["step"]])
            s["obs_bytes_default"] = summarize([float(p["bytes"]) for p in leg["step"]])
        for k in (
            "checkpoint_ms",
            "suspend_ms",
            "resume_ms",
            "clone_stopped_ms",
            "checkpoint_running_ms",
        ):
            if leg.get(k):
                s[k] = summarize([float(x) for x in leg[k]])
        if leg.get("fork_to_usable"):
            s["fork_to_usable_ms"] = summarize(
                [f["total_ms"] for f in leg["fork_to_usable"]]
            )
            s["forks_verified"] = all(
                f["inherited_golden_state"] for f in leg["fork_to_usable"]
            )
        out[name] = s
    return out


def _p50(stats: dict | None) -> float | None:
    return stats.get("p50") if stats else None


def plot(summary: dict) -> None:
    fig, axes = new_axes(3, width=4.9)

    palette = {"shinken": "#2a7de1", "cua_docker": "#e8833a", "cua_lume": "#7a52c7"}
    labels = {
        "shinken": "Shinken\n(Docker, Linux)",
        "cua_docker": "cua container\n(Docker, Linux)",
        "cua_lume": "cua lume VM\n(Vz.fw, macOS)",
    }

    # panel 1 — boot → usable
    ax = axes[0]
    names = [
        n
        for n in ("shinken", "cua_docker", "cua_lume")
        if "boot_to_usable_ms" in summary.get(n, {})
    ]
    vals = [summary[n]["boot_to_usable_ms"]["p50"] / 1000.0 for n in names]
    bars = ax.bar(
        [labels[n] for n in names], vals, color=[palette[n] for n in names], width=0.55
    )
    for b, v in zip(bars, vals):
        ax.annotate(
            f"{v:.1f} s",
            (b.get_x() + b.get_width() / 2, v),
            ha="center",
            va="bottom",
            fontsize=9,
        )
    ax.set_ylabel("seconds (p50)")
    ax.set_title("boot → usable\n(create + connect + first screenshot)")

    # panel 2 — act+observe step
    ax = axes[1]
    names = [
        n
        for n in ("shinken", "cua_docker", "cua_lume")
        if "step_ms" in summary.get(n, {})
    ]
    click = [summary[n]["click_ms"]["p50"] for n in names]
    shot = [summary[n]["screenshot_ms"]["p50"] for n in names]
    xs = list(range(len(names)))
    ax.bar(xs, click, color=[palette[n] for n in names], width=0.55, label="click")
    ax.bar(
        xs,
        shot,
        bottom=click,
        color=[palette[n] for n in names],
        width=0.55,
        alpha=0.45,
        label="screenshot",
    )
    for x, c, s in zip(xs, click, shot):
        ax.annotate(f"{c + s:.0f} ms", (x, c + s), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(xs, [labels[n] for n in names])
    ax.set_ylabel("ms (p50)")
    ax.set_title("act + observe step\n(click + full screenshot, solid=click)")

    # panel 3 — local runtime-state verbs (log scale)
    ax = axes[2]
    lume = summary.get("cua_lume", {})
    rows: list[tuple[str, float | None, str, str]] = [
        (
            "checkpoint (live)",
            _p50(summary.get("shinken", {}).get("checkpoint_ms")),
            "shinken",
            "shinken",
        ),
        (
            "fork → usable",
            _p50(summary.get("shinken", {}).get("fork_to_usable_ms")),
            "shinken",
            "shinken",
        ),
        ("snapshot/fork", None, "cua_docker", "cua container"),
        (
            "pause+unpause",
            (
                (_p50(summary.get("cua_docker", {}).get("suspend_ms")) or 0)
                + (_p50(summary.get("cua_docker", {}).get("resume_ms")) or 0)
            )
            or None,
            "cua_docker",
            "cua container",
        ),
    ]
    if lume.get("clone_stopped_ms"):
        rows.append(
            ("clone (stopped)", _p50(lume["clone_stopped_ms"]), "cua_lume", "cua lume")
        )
    elif lume.get("clone_probe_ms"):
        rows.append(
            (
                "clone (stopped,\nmechanism probe)",
                _p50(lume["clone_probe_ms"]),
                "cua_lume",
                "cua lume",
            )
        )
    if lume.get("checkpoint_running_ms"):
        rows.append(
            (
                "checkpoint (stop+\nclone+restart)",
                _p50(lume["checkpoint_running_ms"]),
                "cua_lume",
                "cua lume",
            )
        )
    xs, heights, colors, ticklabels = [], [], [], []
    for i, (verb, val, legname, stack) in enumerate(rows):
        xs.append(i)
        ticklabels.append(f"{verb}\n[{stack}]")
        colors.append(palette[legname])
        heights.append(val if val else 0.0)
    ax.bar(xs, [max(h, 0.001) for h in heights], color=colors, width=0.6)
    ax.set_yscale("log")
    ax.set_ylim(bottom=1.0)
    for x, h in zip(xs, heights):
        if h:
            txt = f"{h / 1000.0:.2f} s" if h >= 1000 else f"{h:.0f} ms"
            ax.annotate(txt, (x, max(h, 1.0)), ha="center", va="bottom", fontsize=8)
        else:
            ax.annotate(
                "not shipped\nlocally",
                (x, 1.4),
                ha="center",
                va="bottom",
                fontsize=8,
                color="#a33",
            )
    ax.set_xticks(xs)
    ax.set_xticklabels(ticklabels, fontsize=7, rotation=28, ha="right")
    ax.set_ylabel("ms (p50, log)")
    ax.set_title("local runtime-state verbs\n(what each stack ships locally)")

    save_plot(fig, "baseline_cua")


def main() -> int:
    try:
        import cua_sandbox  # noqa: F401
    except ImportError:
        print(
            "cua-sandbox is not installed; this suite measures the third-party "
            f"baseline as shipped. Run:  pip install cua-sandbox=={CUA_SANDBOX_PIN}",
            file=sys.stderr,
        )
        return 2

    # Leg selection (SHINKEN_BENCH_CUA_LEGS="shinken,cua_docker,cua_lume"): legs
    # not selected are carried over from a previous results JSON when present, so
    # a slow leg (the lume image is a ~23 GiB one-time pull) can be measured in a
    # separate, clean window. Each leg records its own measurement timestamp.
    import json
    from datetime import datetime, timezone

    selected = [
        s.strip()
        for s in os.environ.get(
            "SHINKEN_BENCH_CUA_LEGS", "shinken,cua_docker,cua_lume"
        ).split(",")
        if s.strip()
    ]
    previous: dict = {}
    prev_path = REPO_ROOT / "benchmarks" / "results" / "baseline_cua.json"
    if prev_path.exists():
        try:
            previous = json.loads(prev_path.read_text()).get("legs", {})
        except Exception:  # noqa: BLE001
            previous = {}

    runners = {
        "shinken": leg_shinken,
        "cua_docker": lambda: asyncio.run(leg_cua_docker_async()),
        "cua_lume": lambda: asyncio.run(leg_cua_lume_async()),
    }
    legs: dict = {}
    _sweep_containers()
    try:
        for name, runner in runners.items():
            if name in selected:
                legs[name] = runner()
                legs[name]["measured_utc"] = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            elif name in previous:
                legs[name] = previous[name]  # carried over, keeps its timestamp
    finally:
        _sweep_containers()

    summary = _summary(legs)
    write_result(
        "baseline_cua",
        {
            "versions": _versions(),
            "fairness": {
                "ordering": "legs run sequentially (shinken → cua_docker → cua_lume), never concurrent",
                "warmups_discarded": {"boot": 1, "step": STEP_WARMUP},
                "geometry": f"shinken matched to cua-xfce default ({MATCHED_GEOMETRY})",
                "docker_sharing": "shinken and cua_docker legs share one Docker daemon (sequentially)",
                "lume_leg": "macOS VM on Virtualization.framework — no Docker in the path; CPU contention only",
                "codecs": "each stack at its shipped default screenshot codec (PNG for both)",
            },
            "summary": summary,
            "legs": legs,
        },
    )
    plot(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
