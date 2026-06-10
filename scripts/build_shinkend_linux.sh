#!/usr/bin/env bash
# Build a Linux x86_64 `shinkend` binary for injection into an OSWorld VM (or any Linux
# sandbox) from any host with Docker — so the target-arch binary the alpha gate needs is a
# one-liner, not tribal knowledge. The image's build stage already compiles shinkend
# (images/linux/Dockerfile, FROM rust:1-slim-bookworm AS build); we reuse it and copy the
# binary out.
#
# Usage:  scripts/build_shinkend_linux.sh [OUT_PATH]
#   OUT_PATH defaults to dist/shinkend-linux-x86_64
# Then:   python scripts/osworld_single.py --backend shinken \
#           --inject-method osworld-exec --inject-controller-url http://VM:5000 \
#           --shinkend-binary dist/shinkend-linux-x86_64 --inject-remote-bin /tmp/shinkend ...
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$REPO_ROOT/dist/shinkend-linux-x86_64}"
TAG="shinken/shinkend-build:latest"

command -v docker >/dev/null 2>&1 || { echo "::error:: docker not found"; exit 1; }

mkdir -p "$(dirname "$OUT")"

# Build only the Dockerfile's `build` stage (compiles shinkend --release --locked), pinned
# to linux/amd64 so the binary matches an x86_64 guest even when building on arm64.
docker build \
  --platform linux/amd64 \
  --target build \
  -f "$REPO_ROOT/images/linux/Dockerfile" \
  -t "$TAG" \
  "$REPO_ROOT"

# Copy the compiled binary out of a throwaway container.
cid="$(docker create --platform linux/amd64 "$TAG")"
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
docker cp "$cid:/src/shinkend/target/release/shinkend" "$OUT"
chmod +x "$OUT"

echo "built linux/amd64 shinkend -> $OUT"
file "$OUT" 2>/dev/null || true
