#!/bin/sh
# Spike #3 (CRIU memory tier) — the dump target: ONE session-leader parent supervising the
# full Linux desktop stack (Xvfb + openbox + xterm + shinkend), mirroring images/linux/start.sh
# but shaped for `criu dump --tree $$`:
#   - launched via `setsid` so $$ is the session leader and the root of the dumped tree;
#   - all child stdio goes to /dev/null (CRIU restore re-opens regular files BY PATH, so a
#     fresh restore-target container must contain every open file — /dev/null always exists,
#     /tmp/xvfb.log from a donor container would not);
#   - cwd is / for the same reason;
#   - SKN_TREE_COMPONENTS allows stepwise scope reduction (drop xterm, openbox, ...) so a
#     restore failure can be attributed to a single component.
#
# Usage (inside the spike container, as root):
#   setsid /usr/local/bin/desktop-tree.sh </dev/null >/dev/null 2>&1 &
#   ... wait for /tmp/desktop-tree.pid ...
set -e
cd /
echo $$ > /tmp/desktop-tree.pid

: "${DISPLAY:=:0}"
: "${SCREEN_GEOMETRY:=1280x800x24}"
: "${SHINKEND_ADDR:=0.0.0.0:8765}"
: "${SKN_TREE_COMPONENTS:=xvfb openbox xterm shinkend}"
export DISPLAY SHINKEND_ADDR

has() { case " $SKN_TREE_COMPONENTS " in *" $1 "*) return 0;; *) return 1;; esac; }

# Same stale-lock hygiene as images/linux/start.sh (a fresh container is clean anyway).
rm -f /tmp/.X*-lock 2>/dev/null || true
rm -rf /tmp/.X11-unix/* 2>/dev/null || true

if has xvfb; then
  Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -ac +extension RANDR </dev/null >/dev/null 2>&1 &
  i=0
  until xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; do
    i=$((i+1)); [ "$i" -gt 50 ] && { echo "Xvfb failed" > /tmp/desktop-tree.err; exit 1; }
    sleep 0.1
  done
fi

if has openbox; then
  openbox </dev/null >/dev/null 2>&1 &
  i=0
  until xsetroot -display "$DISPLAY" -solid '#202020' >/dev/null 2>&1; do
    i=$((i+1)); [ "$i" -gt 50 ] && break
    sleep 0.1
  done
fi

if has xterm; then
  xterm -geometry 80x24+20+20 </dev/null >/dev/null 2>&1 &
fi

if has shinkend; then
  exec shinkend </dev/null >/dev/null 2>&1
else
  # Keep the tree root alive so there is something to dump.
  while true; do sleep 3600; done
fi
