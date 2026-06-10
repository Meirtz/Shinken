#!/usr/bin/env bash
# Spike #2 (E5) — accessibility-coverage + tree-diff-bandwidth sweep, reproducible runner.
#
# Builds the lean base image, then the spike image (chromium + GTK + Qt on top), boots one
# throwaway container (Xvfb + AT-SPI bus + shinkend), launches each surface, and runs the
# coverage harness (AT-SPI via scripts/a11y_coverage.py; Chromium via scripts/cdp_smoke.py)
# plus a tree-diff-vs-full-vs-screenshot bandwidth measurement. Prints JSON to stdout.
#
# Usage (from repo root):  bash spikes/a11y-coverage/run.sh
# Requires: Docker. No model endpoint, no network beyond the image build.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOKEN="${SHINKEND_TOKEN:-spiketoken0123456789}"
CONTAINER="${CONTAINER:-a11y-spike}"
# Resolved INSIDE the container below (the multiarch triplet is the *Linux* name —
# aarch64-linux-gnu / x86_64-linux-gnu — not the host's `uname -m`, which on a macOS
# Docker host reports `arm64` and would miss the path).

echo "== build base + spike images ==" >&2
docker build -f "$REPO_ROOT/images/linux/Dockerfile"      -t shinken/sandbox-linux "$REPO_ROOT" >&2
docker build -f "$REPO_ROOT/images/linux/Dockerfile.a11y" -t shinken/sandbox-a11y  "$REPO_ROOT" >&2

echo "== boot throwaway spike container ==" >&2
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e SHINKEND_TOKEN="$TOKEN" \
  -v "$REPO_ROOT/sdk/python/src:/opt/shinken/src:ro" \
  -v "$REPO_ROOT/scripts:/opt/shinken/scripts:ro" \
  shinken/sandbox-a11y >/dev/null
sleep 4

dx() { docker exec "$CONTAINER" sh -c "$1"; }
dxd() { docker exec -d "$CONTAINER" sh -c "$1"; }

echo "== launch surfaces (GTK / Qt / GTK-dialog) ==" >&2
QT_CALC="$(dx 'ls /usr/lib/*-linux-gnu/qt5/examples/widgets/widgets/calculator/calculator 2>/dev/null | head -1')"
dxd 'DISPLAY=:0 gnome-text-editor >/tmp/gte.log 2>&1'
dxd "DISPLAY=:0 QT_ACCESSIBILITY=1 QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1 $QT_CALC >/tmp/calc.log 2>&1"
dxd 'DISPLAY=:0 zenity --info --text="Shinken a11y spike" --title="zenity-info" >/tmp/zen.log 2>&1'
# Chromium with CDP (the page-content path). --no-sandbox is required inside the container.
dxd 'DISPLAY=:0 chromium --no-sandbox --no-first-run --disable-gpu --user-data-dir=/tmp/cprof \
  --remote-debugging-port=9222 --remote-allow-origins=* \
  "data:text/html,<html><head><title>Form</title></head><body><h1>Account</h1>\
<form><label for=u>Username</label><input id=u name=u> <label for=p>Password</label>\
<input id=p type=password name=p> <label><input type=checkbox name=r checked> Remember me</label>\
<button type=submit>Sign in</button> <button type=reset>Clear</button></form></body></html>" \
  >/tmp/chromium.log 2>&1'
# Qt's AT-SPI registration is lazy/timing-sensitive — it can take several seconds after
# window-map before the bridge publishes the widget tree. Give the surfaces time to settle.
sleep 10

echo "== AT-SPI coverage (zenity / GTK / Qt / xterm / Chromium) ==" >&2
dx 'PYTHONPATH=/opt/shinken/src python3 /opt/shinken/scripts/a11y_coverage.py \
      zenity gnome-text-editor calculator xterm Chromium'

echo "== CDP coverage (Chromium page content) ==" >&2
dx 'PYTHONPATH=/opt/shinken/src SHINKEN_CDP_HTTP_URL=http://127.0.0.1:9222 \
      python3 /opt/shinken/scripts/cdp_smoke.py'

echo "== tree-diff bandwidth (type into GTK editor) + screenshot byte baseline ==" >&2
dx 'PYTHONPATH=/opt/shinken/src python3 - <<PY
import json, time, subprocess, base64
import shinken
from shinken.a11y import AtspiSource, to_elements, diff_elements, diff_size
src=AtspiSource(app_name="gnome-text-editor")
e1=to_elements(src.tree(), source="atspi")
subprocess.run(["xdotool","search","--name","Text Editor","windowactivate"],env={"DISPLAY":":0"},timeout=10)
time.sleep(0.3)
subprocess.run(["xdotool","type","--delay","20","Hello Shinken spike"],env={"DISPLAY":":0"},timeout=10)
time.sleep(0.6)
e2=to_elements(src.tree(), source="atspi")
diff=diff_elements(e1,e2); sizes=diff_size(diff,e2)
sb=shinken.connect("127.0.0.1:8765", token="'$TOKEN'")
obs=sb.observe(); png=obs.get("png") or ""
shot=len(base64.b64decode(png)) if isinstance(png,str) else len(png)
sb.close()
print(json.dumps({"diff":{"surface":"gnome-text-editor-type",
  "diff_bytes":sizes["diff_bytes"],"full_bytes":sizes["full_bytes"],"ratio":sizes["ratio"],
  "added":len(diff["added"]),"removed":len(diff["removed"]),"changed":len(diff["changed"]),"unchanged":diff["unchanged"]},
  "screenshot_png_bytes":shot},indent=2))
PY'

echo "== done; remove container with: docker rm -f $CONTAINER ==" >&2
