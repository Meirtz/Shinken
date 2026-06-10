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
# Outputs: benchmarks/results/*.json + docs/assets/bench/*.png.
# Total runtime is roughly 25-40 minutes on a laptop-class machine.
set -euo pipefail
cd "$(dirname "$0")/.."

for suite in codec_ladder delta_screencast action_latency fork fanout client_scale wire_ceiling guest_cpu boot_waterfall; do
  echo "=== bench_${suite} ==="
  python3 "benchmarks/bench_${suite}.py"
done

# fork suite, warm-pool mode (S4b): same script, pool-accelerated graft path
echo "=== bench_fork (pool mode) ==="
SHINKEN_BENCH_FORK_MODE=pool python3 benchmarks/bench_fork.py

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
echo "all suites done"
