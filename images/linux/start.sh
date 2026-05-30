#!/usr/bin/env bash
# Boot a headless Linux desktop, then launch shinkend on the ACI port.
set -euo pipefail

: "${DISPLAY:=:0}"
: "${SCREEN_GEOMETRY:=1280x800x24}"
: "${SHINKEND_ADDR:=0.0.0.0:8765}"
export DISPLAY SHINKEND_ADDR

# Accessibility bus (AT-SPI) — needed by the structured observation track (M1).
export NO_AT_BRIDGE=0

Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
for _ in $(seq 1 50); do xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break; sleep 0.1; done

openbox >/tmp/openbox.log 2>&1 &

echo "shinken: desktop ready on $DISPLAY; starting shinkend on $SHINKEND_ADDR"
exec shinkend
