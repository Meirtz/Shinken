#!/usr/bin/env bash
# Run every local benchmark suite serially (serially on purpose: concurrent suites
# would contend for the Docker daemon and skew each other's latency numbers).
#
# Requires: Docker + the shinken/sandbox-linux image built FROM THIS CHECKOUT
# (an image built from an older checkout may lack runtime features a suite
# measures, e.g. the JPEG codec, delta screencast, binary frames, or the guest
# ready query):
#
#   docker build -f images/linux/Dockerfile -t shinken/sandbox-linux .
#
# Every suite in the main loop (including step_pipeline, whose WAN emulation is a
# local asyncio proxy) needs only that standard image — nothing else to build or
# install.
#
# Outputs: benchmarks/results/*.json + docs/assets/bench/*.png.
# Total runtime is roughly 25-45 minutes on a laptop-class machine.
set -euo pipefail
cd "$(dirname "$0")/.."

for suite in codec_ladder delta_screencast action_latency fork fanout client_scale wire_ceiling guest_cpu boot_waterfall step_pipeline; do
  echo "=== bench_${suite} ==="
  python3 "benchmarks/bench_${suite}.py"
done

# fork suite, warm-pool mode (S4b): same script, pool-accelerated graft path
echo "=== bench_fork (pool mode) ==="
SHINKEN_BENCH_FORK_MODE=pool python3 benchmarks/bench_fork.py

# S10 (fleet-level observation dedup) needs a runtime + SDK that speak frame_dedup —
# the image must be built FROM THIS CHECKOUT (like every suite); guard on the SDK
# capability surface so an older environment skips instead of crashing mid-run.
if python3 -c "import sys; sys.path.insert(0, 'sdk/python/src'); import shinken; shinken.FrameCache" 2>/dev/null; then
  echo "=== bench_fork_dedup ==="
  python3 benchmarks/bench_fork_dedup.py
else
  echo "=== bench_fork_dedup skipped (SDK lacks FrameCache — rebuild from this checkout) ==="
fi

# S7 (head-to-head vs OSWorld's guest server) needs the dual-server image variant:
#   docker build -f images/linux/Dockerfile.osworld -t shinken/sandbox-linux-osworld .
# It is skipped when that image is absent so the core suites stay self-contained.
OSWORLD_IMAGE="${SHINKEN_BENCH_OSWORLD_IMAGE:-shinken/sandbox-linux-osworld}"
if docker image inspect "$OSWORLD_IMAGE" >/dev/null 2>&1; then
  echo "=== bench_osworld_loop ==="
  python3 benchmarks/bench_osworld_loop.py
else
  echo "=== bench_osworld_loop skipped ($OSWORLD_IMAGE image not built) ==="
fi

# S12 (first-party baseline vs trycua/cua) measures a THIRD-PARTY stack as shipped,
# so it needs `pip install cua-sandbox==0.1.16` + their images — opt in explicitly
# (the suite itself also exits with a clear message when cua-sandbox is missing):
#   SHINKEN_BENCH_CUA=1 bash benchmarks/run_all.sh   (or run the suite directly)
if [ "${SHINKEN_BENCH_CUA:-0}" = "1" ]; then
  echo "=== bench_baseline_cua ==="
  python3 benchmarks/bench_baseline_cua.py
else
  echo "=== bench_baseline_cua skipped (set SHINKEN_BENCH_CUA=1; needs the cua venv/image) ==="
fi
echo "all suites done"
