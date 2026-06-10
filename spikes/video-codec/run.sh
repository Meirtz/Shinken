#!/usr/bin/env bash
# Spike — software video codec vs delta-PNG tiles for MOTION content, reproducible runner.
#
# Builds the rust harness (vcspike: faithful replication of shinkend's B2 dirty-tile
# encoder + full-frame JPEG + in-process openh264), generates three deterministic
# 1280x800 content classes (typing / scroll / photo-pan) at 10 and 30 fps, encodes each
# with every contender, and writes evidence.json. Raw frame sequences are deleted as it
# goes (peak ~1 GB scratch in runs/, which is git-ignored).
#
# Usage (from repo root):  bash spikes/video-codec/run.sh
# Requires: rust toolchain, python3 + numpy + Pillow, ffmpeg with libvpx + libx264.
# Runtime: ~10 minutes on an Apple-silicon laptop. No Docker, no network after the
# first `cargo build` (crates.io fetch).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== build vcspike (release) ==" >&2
cargo build --release --manifest-path "$HERE/harness/Cargo.toml" >&2

echo "== run matrix ==" >&2
python3 "$HERE/run_matrix.py" "$@"

echo "== done; results in $HERE/evidence.json, narrative in $HERE/REPORT.md ==" >&2
