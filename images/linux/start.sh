#!/usr/bin/env bash
# Launch shinkend FIRST, then boot the headless desktop behind it (S8 boot waterfall).
#
# shinkend owns X11 readiness: with SHINKEND_EXECUTOR=x11_xtest it retries $DISPLAY
# internally with short backoff, and the guest-side `ready` query reports
# {ready, x11_up, root_nonblack} honestly while Xvfb/openbox come up. That puts the
# ACI listener up within milliseconds of container start, instead of gating it behind
# shell poll loops at 100 ms granularity (which cost ~5 s of the measured cold boot).
set -euo pipefail

: "${DISPLAY:=:0}"
: "${SCREEN_GEOMETRY:=1280x800x24}"
# In-container default binds all interfaces so the host port-mapping reaches it; the
# container is meant to be published to HOST loopback only (-p 127.0.0.1:8765:8765).
# Because that is a non-loopback bind, shinkend REQUIRES $SHINKEND_TOKEN (it refuses
# to start otherwise) — pass one with `-e SHINKEND_TOKEN=...`.
: "${SHINKEND_ADDR:=0.0.0.0:8765}"
# Explicit X11 backend: `auto` probes the display ONCE at startup and would fall back
# to the virtual backend forever when shinkend starts before Xvfb; x11_xtest is lazy
# (retry-connect with backoff) and is what a desktop image always wants.
: "${SHINKEND_EXECUTOR:=x11_xtest}"
export DISPLAY SHINKEND_ADDR SHINKEND_EXECUTOR

# Accessibility bus (AT-SPI) — needed by the structured observation track (M1).
export NO_AT_BRIDGE=0

# Clear stale X locks before starting Xvfb. A snapshot/fork (`docker commit` of a *live*
# container) bakes the running X server's lock files (/tmp/.X*-lock, /tmp/.X11-unix/X*)
# into the image; a fresh container re-running this script would then find a stale lock,
# fail to claim the display, and leave shinkend screenshotting a dead/degenerate display.
# Removing them makes the disk-tier fork (checkpoint→fork→resume, D5) boot a clean desktop.
rm -f /tmp/.X*-lock 2>/dev/null || true
rm -rf /tmp/.X11-unix/* 2>/dev/null || true

# Desktop boot, CONCURRENT with shinkend: Xvfb starts immediately; openbox/xterm need a
# live display (they exit instantly without one), so a background subshell gates them on
# xdpyinfo at 50 ms granularity. Nothing here gates the ACI listener. The root paint
# (xsetroot — what flips the guest `ready` signal's root_nonblack) runs FIRST and is
# re-asserted a few times: a transiently failed first paint must heal itself, not leave
# the sandbox honestly-but-forever unready.
# -noreset: without it the X server REGENERATES whenever its last client disconnects —
# and the boot sequence is exactly short-lived clients (xdpyinfo probes, xsetroot). A
# regeneration wipes the root paint (the `ready` signal) and can kill clients that are
# mid-connect, leaving a black, WM-less desktop. Persistent clients (shinkend, xterm)
# normally mask this; -noreset removes the race entirely.
Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -ac -noreset +extension RANDR >/tmp/xvfb.log 2>&1 &
(
  up=0
  for _ in $(seq 1 600); do
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && { up=1; break; }
    sleep 0.05
  done
  if [ "$up" = 1 ]; then
    xsetroot -display "$DISPLAY" -solid '#202020' >/dev/null 2>&1 || true
    openbox >/tmp/openbox.log 2>&1 &
    xterm -geometry 80x24+20+20 >/tmp/xterm.log 2>&1 &
    # Re-assert the root paint at 1 Hz for 30 s: under a heavily contended host a WM
    # that initializes late can clear the root AFTER an early one-shot paint, leaving
    # the ready signal black forever. A painted root stays painted (-noreset), so the
    # re-paint is idempotent and free.
    for _ in $(seq 1 30); do
      sleep 1
      xsetroot -display "$DISPLAY" -solid '#202020' >/dev/null 2>&1 || true
    done
  else
    echo "desktop: $DISPLAY never answered xdpyinfo within 30s" >>/tmp/desktop.log
  fi
) >/tmp/desktop.log 2>&1 &

echo "shinken: starting shinkend on $SHINKEND_ADDR (desktop booting on $DISPLAY behind it)"
exec shinkend
