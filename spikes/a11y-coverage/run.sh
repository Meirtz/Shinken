#!/usr/bin/env bash
# Spike #2 (E5) — accessibility-coverage + tree-diff-bandwidth sweep, reproducible runner.
#
# Builds the lean base image, then the spike image (chromium + GTK + Qt + Electron on top),
# boots one throwaway container (Xvfb + AT-SPI bus + shinkend), launches each surface, and
# runs the coverage harness (AT-SPI via scripts/a11y_coverage.py; Chromium/canvas/Electron
# via scripts/cdp_smoke.py) plus a tree-diff-vs-full-vs-screenshot bandwidth measurement
# and a canvas blind-spot probe (pixels change, structured diff sees nothing). Prints JSON
# chunks to stdout.
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
  -v "$REPO_ROOT/spikes/a11y-coverage:/opt/shinken/spike:ro" \
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

# --- canvas + Electron rows (the two surfaces the first sweep left unmeasured) -------------
# Launched AFTER the measurements above so the original five rows are produced under
# identical conditions to the first run of this spike.

echo "== launch canvas-UI chromium (CDP :9223) + Electron (CDP :9224) ==" >&2
# Kiosk so page coordinates == screen coordinates (the blind-spot probe clicks a drawn button).
# The page is passed as a bare container path (chromium turns it into a local-file URL itself).
dxd 'DISPLAY=:0 chromium --no-sandbox --no-first-run --disable-gpu --user-data-dir=/tmp/cprof2 \
  --remote-debugging-port=9223 --remote-allow-origins=* --kiosk \
  /opt/shinken/spike/canvas_app.html >/tmp/chromium_canvas.log 2>&1'
# Electron only publishes its renderer tree to AT-SPI when accessibility is forced on
# (--force-renderer-accessibility) — same switch family as Chromium; --no-sandbox is
# required inside the container.
dxd 'DISPLAY=:0 /opt/electron-app/node_modules/.bin/electron --no-sandbox --disable-gpu \
  --disable-dev-shm-usage --force-renderer-accessibility --remote-debugging-port=9224 \
  /opt/electron-app >/tmp/electron.log 2>&1'
sleep 10

echo "== AT-SPI desktop registry (who shows up on the bus at all?) ==" >&2
dx 'python3 - <<PY
import gi, json
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi
Atspi.init()
d = Atspi.get_desktop(0)
print(json.dumps({"atspi_desktop_apps": [d.get_child_at_index(i).get_name() for i in range(d.get_child_count())]}))
PY'

echo "== AT-SPI coverage (Electron shell+renderer) ==" >&2
dx 'PYTHONPATH=/opt/shinken/src python3 /opt/shinken/scripts/a11y_coverage.py electron'

echo "== CDP coverage (canvas-UI page) ==" >&2
dx 'PYTHONPATH=/opt/shinken/src SHINKEN_CDP_HTTP_URL=http://127.0.0.1:9223 \
      python3 /opt/shinken/scripts/cdp_smoke.py'

echo "== CDP coverage (Electron window) ==" >&2
dx 'PYTHONPATH=/opt/shinken/src SHINKEN_CDP_HTTP_URL=http://127.0.0.1:9224 \
      python3 /opt/shinken/scripts/cdp_smoke.py'

echo "== canvas blind spot: click a drawn button — pixels change, the tree does not ==" >&2
dx 'PYTHONPATH=/opt/shinken/src python3 - <<PY
import base64, hashlib, json, subprocess, time
import shinken
from shinken.a11y import diff_elements, diff_size, to_elements
from shinken.cdp import CdpSource
env = {"DISPLAY": ":0"}

def shot(sb):  # the SDK may hand back decoded bytes or base64 text
    png = sb.observe().get("png") or b""
    return base64.b64decode(png) if isinstance(png, str) else png

src = CdpSource(http_url="http://127.0.0.1:9223")
# Activate BEFORE the first capture so focus state is identical across both captures.
subprocess.run(["xdotool", "search", "--name", "Canvas UI", "windowactivate"], env=env, timeout=10)
time.sleep(0.5)
e1 = to_elements(src.tree(), source="cdp")
sb = shinken.connect("127.0.0.1:8765", token="'$TOKEN'")
png1 = shot(sb)
# Click the canvas-drawn "Sign in" button (center of R.signin in canvas_app.html).
subprocess.run(["xdotool", "mousemove", "120", "358", "click", "1"], env=env, timeout=10)
time.sleep(0.6)
png2 = shot(sb)
sb.close()
e2 = to_elements(src.tree(), source="cdp")
diff = diff_elements(e1, e2); sizes = diff_size(diff, e2)
print(json.dumps({"canvas_blind_spot": {
  "drawn_interactive_controls": 5,
  "click": "Sign in (canvas-drawn button @120,358)",
  "pixels_changed": hashlib.sha256(png1).hexdigest() != hashlib.sha256(png2).hexdigest(),
  "screenshot_png_bytes_before": len(png1), "screenshot_png_bytes_after": len(png2),
  "ax_nodes_before": len(e1), "ax_nodes_after": len(e2),
  "ax_diff": {"added": len(diff["added"]), "removed": len(diff["removed"]),
              "changed": len(diff["changed"]), "unchanged": diff["unchanged"]},
  "ax_diff_bytes": sizes["diff_bytes"], "ax_full_bytes": sizes["full_bytes"]}}, indent=2))
PY'

echo "== done; remove container with: docker rm -f $CONTAINER ==" >&2
