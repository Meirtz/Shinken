#!/usr/bin/env bash
# Entrypoint for the S7 head-to-head image: supervise the OSWorld Flask server in the
# background, then exec the standard Shinken entrypoint (Xvfb + openbox + xterm +
# shinkend). The OSWorld VM image runs its server under systemd (`python main.py`,
# osworld_server.service); a supervised background loop is the container equivalent.
set -euo pipefail

: "${DISPLAY:=:0}"
export DISPLAY

# OSWorld pins python3-xlib==0.15, which hard-fails when ~/.Xauthority is missing
# (its VM — a full Ubuntu desktop — always has one; Xvfb runs with -ac so an empty
# file is sufficient here).
touch "$HOME/.Xauthority"

# pyscreeze (pyautogui's capture backend) only takes its scrot/X11 path when the
# session advertises X11; OSWorld's Ubuntu desktop session sets this, a bare
# container does not.
export XDG_SESSION_TYPE=x11

(
  # main.py imports pyautogui (which opens $DISPLAY) and pyatspi (which wants a DBus
  # session bus — the OSWorld systemd unit exports DBUS_SESSION_BUS_ADDRESS) at module
  # load, so wait for the X server start.sh is about to boot, give the server its own
  # session bus, and restart it if it ever dies — same supervision systemd provides.
  for _ in $(seq 1 300); do
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && break
    sleep 0.1
  done
  cd /opt/osworld-server
  while true; do
    dbus-run-session -- python3 main.py >>/tmp/osworld-server.log 2>&1 || true
    echo "osworld server exited; restarting in 1s" >>/tmp/osworld-server.log
    sleep 1
  done
) &

exec /usr/local/bin/start.sh
