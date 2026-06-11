"""Regenerate every benchmark figure from the tracked result JSONs — no Docker, no
rerun. Each suite's ``plot(payload)`` is pure (figure from datapoints), and each
``benchmarks/results/<suite>.json`` carries the full datapoint payload, so figure
styling can evolve without re-measuring:

    python benchmarks/replot.py            # all suites with a result JSON
    python benchmarks/replot.py fork_resume client_scale
"""

from __future__ import annotations

import importlib
import json
import sys

from _common import RESULTS_DIR

# result-JSON name -> suite module that owns its plot()
SUITES = {
    "codec_ladder": "bench_codec_ladder",
    "delta_screencast": "bench_delta_screencast",
    "action_latency": "bench_action_latency",
    "fork_resume": "bench_fork",
    "fork_resume_pool": "bench_fork",
    "fork_resume_memory": "bench_fork",
    "fork_dedup": "bench_fork_dedup",
    "local_fanout": "bench_fanout",
    "client_scale": "bench_client_scale",
    "wire_ceiling": "bench_wire_ceiling",
    "guest_cpu": "bench_guest_cpu",
    "osworld_loop": "bench_osworld_loop",
    "boot_waterfall": "bench_boot_waterfall",
    "step_pipeline": "bench_step_pipeline",
    "baseline_cua": "bench_baseline_cua",
    "obs_quality": "bench_obs_quality",
}


def main(argv: list[str]) -> int:
    wanted = argv or [s for s in SUITES if (RESULTS_DIR / f"{s}.json").exists()]
    unknown = [s for s in wanted if s not in SUITES]
    if unknown:
        print(f"unknown suite(s): {', '.join(unknown)}; known: {', '.join(SUITES)}")
        return 2
    for suite in wanted:
        path = RESULTS_DIR / f"{suite}.json"
        if not path.exists():
            print(f"skip {suite}: {path} missing (run benchmarks/{SUITES[suite]}.py first)")
            continue
        payload = json.loads(path.read_text())
        importlib.import_module(SUITES[suite]).plot(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
