#!/usr/bin/env bash
# CRIU memory-tier container entry (images/linux/Dockerfile.criu) — the productized
# shape of the positive CRIU spike (spikes/criu-memory-tier/): the whole desktop runs
# under ONE supervised session so `criu dump --tree` can checkpoint it live, while the
# container's MAIN process stays an idle sleep so neither a dump nor a restore can take
# the container down. Two roles in one file:
#
#   start-criu.sh        (image CMD)   LAUNCHER: park the PID counter, detach the
#                                      desktop tree as one orphaned session leader,
#                                      then idle as the container's main process.
#   start-criu.sh tree   (internal)    SUPERVISOR: the dumpable tree itself — mirrors
#                                      images/linux/start.sh (a11y bus, X hygiene,
#                                      boot-flake fixes), shaped for
#                                      `criu dump --tree $(cat /tmp/shinken-tree.pid)`.
#
# CRIU shaping (every line of this is a documented spike pitfall):
#   - the launcher parks /proc/sys/kernel/ns_last_pid at $SHINKEN_CRIU_PID_FLOOR before
#     the tree boots, so every PID/TID in the tree lands ABOVE the early PIDs a fresh
#     restore-target container occupies (tini, the idle sleep, exec helpers) — CRIU
#     restores EXACT pids and any squatter fails the restore (pitfall 2);
#   - `setsid --fork` orphans the supervisor to PID 1 (tini via `docker run --init`),
#     which reaps whatever a dump kills or a restore detaches (pitfall 1);
#   - child stdio goes to /dev/null and cwd is / — CRIU reopens regular files BY PATH
#     at restore, and /dev/null exists in every restore target while a donor's
#     /tmp/*.log may not (pitfalls 3+4; the docker-commit pairing makes this belt and
#     braces rather than load-bearing);
#   - restore targets boot with the image CMD OVERRIDDEN to `sleep infinity` (the
#     provider does this), so nothing here races the restored tree for the display,
#     the port, or the PIDs.
#
# Runs as root in a --privileged container (in-container CRIU needs CAP_SYS_ADMIN) —
# a latency/state-fidelity tier, NOT an isolation posture (see Dockerfile.criu).
set -euo pipefail

if [ "${1:-}" != "tree" ]; then
  # ---- LAUNCHER (container main process) ----
  echo "${SHINKEN_CRIU_PID_FLOOR:-300}" > /proc/sys/kernel/ns_last_pid 2>/dev/null || true
  setsid --fork "$0" tree </dev/null >/dev/null 2>&1
  exec sleep infinity
fi

# ---- SUPERVISOR — the dumpable desktop tree ----
cd /
echo $$ > /tmp/shinken-tree.pid

: "${DISPLAY:=:0}"
: "${SCREEN_GEOMETRY:=1280x800x24}"
: "${SHINKEND_ADDR:=0.0.0.0:8765}"
: "${SHINKEND_EXECUTOR:=x11_xtest}"
export DISPLAY SHINKEND_ADDR SHINKEND_EXECUTOR

# Accessibility bus (AT-SPI) — the CRIU-dumpable variant of start.sh's a11y stack.
# ONE dbus-daemon serves as BOTH the session bus and the a11y bus, registryd runs
# directly, and AT_SPI_BUS_ADDRESS (which libatspi, toolkit bridges and shinkend all
# check FIRST) replaces org.a11y.Bus discovery — because the stock at-spi-bus-launcher
# holds a glib child-watch PIDFD on its spawned bus daemon and CRIU 3.17 cannot dump
# pidfds (measured wall in the tier bring-up; see shinken-criu-bus.conf for the
# matching no-inotify-watches bus config). Everything stays inside this session, so
# all sockets are in-tree AF_UNIX state CRIU carries; structured observation was
# live-verified across dump/restore (zenity tree on a restored replica).
# SHINKEN_CRIU_A11Y=off is the escape hatch that drops the stack from the tree.
if [ "${SHINKEN_CRIU_A11Y:-on}" = "on" ]; then
  export NO_AT_BRIDGE=0
  export DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/shinken-session-bus"
  export AT_SPI_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS"
  rm -f /tmp/shinken-session-bus 2>/dev/null || true
  dbus-daemon --config-file=/usr/local/share/shinken/criu-bus.conf --nofork --nopidfile \
    </dev/null >/dev/null 2>&1 &
  (
    for _ in $(seq 1 100); do
      [ -S /tmp/shinken-session-bus ] && break
      sleep 0.05
    done
    /usr/libexec/at-spi2-registryd </dev/null >/dev/null 2>&1 || true
  ) </dev/null >/dev/null 2>&1 &
fi

# X hygiene, exactly as start.sh: a committed donor image bakes the running X server's
# socket/locks into the rootfs; a FRESH desktop boot (this path) must clear them, while
# a criu restore (which never runs this script) rebinds them itself.
rm -f /tmp/.X*-lock 2>/dev/null || true
rm -rf /tmp/.X11-unix/* 2>/dev/null || true
mkdir -p /tmp/.X11-unix 2>/dev/null || true
chmod 1777 /tmp/.X11-unix 2>/dev/null || true

Xvfb "$DISPLAY" -screen 0 "$SCREEN_GEOMETRY" -ac -noreset +extension RANDR \
  </dev/null >/dev/null 2>&1 &
(
  up=0
  for _ in $(seq 1 600); do
    xdpyinfo -display "$DISPLAY" >/dev/null 2>&1 && { up=1; break; }
    sleep 0.05
  done
  if [ "$up" = 1 ]; then
    # Root paint + WM + focused xterm + repaint/focus convergence — the same
    # boot-flake fixes as start.sh (see its comments), output CRIU-safe.
    xsetroot -display "$DISPLAY" -solid '#202020' || true
    openbox </dev/null >/dev/null 2>&1 &
    for _ in $(seq 1 200); do
      xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null | grep -q 'window id' && break
      sleep 0.05
    done
    xterm -geometry 80x24+20+20 </dev/null >/dev/null 2>&1 &
    (
      for _ in $(seq 1 150); do
        xdotool getactivewindow >/dev/null 2>&1 && exit 0
        xdotool search --class xterm windowactivate >/dev/null 2>&1 || true
        sleep 0.2
      done
    ) </dev/null >/dev/null 2>&1 &
    for _ in $(seq 1 30); do
      sleep 1
      xsetroot -display "$DISPLAY" -solid '#202020' || true
    done
  fi
) </dev/null >/dev/null 2>&1 &

# The tree root becomes shinkend (the spike's proven dump shape: probe 3).
exec shinkend </dev/null >/dev/null 2>&1
