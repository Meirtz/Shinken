#!/usr/bin/env bash
# Boot a headless Linux desktop, then launch shinkend on the ACI port.
set -euo pipefail

: "${DISPLAY:=:0}"
: "${SCREEN_GEOMETRY:=1280x800x24}"
# In-container default binds all interfaces so the host port-mapping reaches it; the
# container is meant to be published to HOST loopback only (-p 127.0.0.1:8765:8765).
# Because that is a non-loopback bind, shinkend REQUIRES $SHINKEND_TOKEN (it refuses
# to start otherwise) — pass one with `-e SHINKEND_TOKEN=...`.
: "${SHINKEND_ADDR:=0.0.0.0:8765}"
export DISPLAY SHINKEND_ADDR

# Accessibility bus (AT-SPI) — needed by the structured observation track (M1).
export NO_AT_BRIDGE=0

# Clear stale X locks before starting Xvfb. A snapshot/fork (`docker commit` of a *live*
# container) bakes the running X server's lock files (/tmp/.X*-lock, /tmp/.X11-unix/X*)
# into the image; a fresh container re-running this script would then find a stale lock,
# fail to claim the display, and leave shinkend screenshotting a dead/degenerate display.
# Removing them makes the disk-tier fork (checkpoint→fork→resume, D5) boot a clean desktop.
rm -f /tmp/.X*-lock 2>/dev/null || true
rm -rf /tmp/.X11-unix/* 2>/dev/null || true

Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
for _ in $(seq 1 50); do xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break; sleep 0.1; done

openbox >/tmp/openbox.log 2>&1 &
for _ in $(seq 1 50); do xsetroot -display "$DISPLAY" -solid '#202020' >/dev/null 2>&1 && break; sleep 0.1; done
xterm -geometry 80x24+20+20 >/tmp/xterm.log 2>&1 &

echo "shinken: desktop ready on $DISPLAY; starting shinkend on $SHINKEND_ADDR"
exec shinkend
