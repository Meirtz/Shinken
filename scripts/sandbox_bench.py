"""Low-concurrency sandbox provider benchmark."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from shinken.providers import DockerLocalProvider, ExternalProvider, SandboxProvider, SandboxSpec


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def _provider(args: argparse.Namespace) -> SandboxProvider:
    if args.provider == "docker":
        return DockerLocalProvider(
            image=args.image,
            docker_bin=args.docker_bin,
            name_prefix=args.name_prefix,
            startup_timeout=args.startup_timeout,
        )
    return ExternalProvider(addr=args.addr, token=args.token)


def _spec(args: argparse.Namespace) -> SandboxSpec:
    return SandboxSpec(
        image=args.image,
        memory=args.memory,
        cpus=args.cpus,
        pids_limit=args.pids_limit,
        shm_size=args.shm_size,
        screen_geometry=args.screen_geometry,
    )


def _one_run(args: argparse.Namespace, run_id: int) -> dict[str, Any]:
    provider = _provider(args)
    spec = _spec(args)
    handle = None
    result: dict[str, Any] = {
        "run_id": run_id,
        "provider": provider.capabilities.name,
        "reset_strategy": provider.capabilities.reset_strategy,
    }
    try:
        t0 = time.perf_counter()
        handle = provider.create(spec)
        result["create_ms"] = _ms(t0)
        result["sandbox_id"] = handle.sandbox_id

        t0 = time.perf_counter()
        health = provider.health(handle)
        result["ready_ms"] = _ms(t0)
        result["health"] = asdict(health)
        if not health.ready:
            result["ok"] = False
            result["error"] = health.detail
            return result

        t0 = time.perf_counter()
        env = provider.connect(handle)
        result["connect_ms"] = _ms(t0)
        try:
            result["ping_ms"] = round(env.ping() * 1000.0, 3)

            t0 = time.perf_counter()
            shot = env.screenshot()
            result["screenshot_ms"] = _ms(t0)
            result["screenshot_bytes"] = len(shot["png"])
            result["screenshot_size"] = {"w": shot["w"], "h": shot["h"]}

            t0 = time.perf_counter()
            env.click(x=10, y=10)
            result["click_ms"] = _ms(t0)

            t0 = time.perf_counter()
            with env.screencast(
                fps=args.fps,
                timeout=args.frame_timeout,
                limit=1,
                max_long_edge=args.max_long_edge,
            ) as stream:
                frames = list(stream)
            result["screencast_first_frame_ms"] = _ms(t0)
            result["screencast_frames"] = len(frames)
            result["screencast_bytes"] = sum(len(frame["png"]) for frame in frames)
        finally:
            env.close()

        result["ok"] = True
        return result
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
        return result
    finally:
        if handle is not None and args.provider == "docker":
            t0 = time.perf_counter()
            try:
                provider.destroy(handle)
                result["destroy_ms"] = _ms(t0)
            except Exception as exc:
                result["destroy_error"] = str(exc)


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in results if row.get("ok")]
    summary: dict[str, Any] = {
        "runs": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
    }
    for key in [
        "create_ms",
        "ready_ms",
        "connect_ms",
        "ping_ms",
        "screenshot_ms",
        "click_ms",
        "screencast_first_frame_ms",
        "screenshot_bytes",
        "screencast_bytes",
        "destroy_ms",
    ]:
        values = [row[key] for row in ok if isinstance(row.get(key), int | float)]
        if values:
            summary[key] = {
                "min": round(min(values), 3),
                "mean": round(statistics.fmean(values), 3),
                "max": round(max(values), 3),
            }
    return summary


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Sandbox Benchmark",
        "",
        f"- Provider: `{report['provider']}`",
        f"- Concurrency: `{report['concurrency']}`",
        f"- Runs: `{report['summary']['runs']}`",
        f"- OK: `{report['summary']['ok']}`",
        f"- Failed: `{report['summary']['failed']}`",
        "",
        "| Metric | Min | Mean | Max |",
        "|---|---:|---:|---:|",
    ]
    for key, value in report["summary"].items():
        if isinstance(value, dict):
            lines.append(f"| `{key}` | {value['min']} | {value['mean']} | {value['max']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["docker", "external"], default="external")
    parser.add_argument("--addr", default="127.0.0.1:8765")
    parser.add_argument("--token")
    parser.add_argument("--image", default="shinken/sandbox-linux")
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--name-prefix", default="shinken-bench")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--frame-timeout", type=float, default=5.0)
    parser.add_argument("--fps", type=float, default=5.0)
    parser.add_argument("--max-long-edge", type=int, default=640)
    parser.add_argument("--memory")
    parser.add_argument("--cpus", type=float)
    parser.add_argument("--pids-limit", type=int)
    parser.add_argument("--shm-size")
    parser.add_argument("--screen-geometry", default="1280x800x24")
    parser.add_argument(
        "--cleanup-orphans",
        action="store_true",
        help="remove containers matching --name-prefix before running a Docker benchmark",
    )
    parser.add_argument("--output", default="sandbox-bench.json")
    parser.add_argument("--markdown-output")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args(argv)

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    if args.provider == "docker" and args.cleanup_orphans:
        removed = _provider(args).cleanup_orphans()  # type: ignore[attr-defined]
        if removed:
            print(f"removed {removed} orphaned sandbox container(s)")

    total = args.concurrency * args.iterations
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(_one_run, args, i) for i in range(total)]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    results.sort(key=lambda row: row["run_id"])

    report = {
        "provider": args.provider,
        "concurrency": args.concurrency,
        "iterations": args.iterations,
        "generated_at": time.time(),
        "summary": _summary(results),
        "results": results,
    }
    Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.markdown_output:
        Path(args.markdown_output).write_text(_markdown(report))
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
