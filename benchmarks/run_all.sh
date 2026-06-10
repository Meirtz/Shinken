#!/usr/bin/env bash
# Run every local benchmark suite serially (serially on purpose: concurrent suites
# would contend for the Docker daemon and skew each other's latency numbers).
#
# Requires: Docker + the shinken/sandbox-linux image built FROM THIS CHECKOUT
# (an image built from an older checkout may lack runtime features a suite
# measures, e.g. the JPEG codec or delta screencast):
#
#   docker build -f images/linux/Dockerfile -t shinken/sandbox-linux .
#
# Outputs: benchmarks/results/*.json + docs/assets/bench/*.png.
# Total runtime is roughly 15-25 minutes on a laptop-class machine.
set -euo pipefail
cd "$(dirname "$0")/.."

for suite in codec_ladder delta_screencast action_latency fork fanout client_scale; do
  echo "=== bench_${suite} ==="
  python3 "benchmarks/bench_${suite}.py"
done
echo "all suites done"
