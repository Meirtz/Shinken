#!/usr/bin/env bash
# Spike #3 — CRIU memory-tier checkpoint/restore of the desktop process tree, in-container,
# on Docker Desktop (LinuxKit kernel). Staged probes; stops at the first hard wall and
# prints everything it learned as JSON.
#
#   probe 1   kernel config (CONFIG_CHECKPOINT_RESTORE & friends) + `criu check`
#   probe 2   trivial trees: counter loop / open pty pair / forked AF_UNIX socketpair
#   probe 3   the real target: Xvfb+openbox+xterm+shinkend under one setsid parent —
#             dump --tree, restore (a) in-place, (b) fresh container + staged runtime
#             files, (c) fresh container from a `docker commit` of the donor (the
#             paired disk+memory checkpoint — the fork shape)
#   probe 4   N-rep fork latency loop: docker run + criu restore + WS handshake +
#             first screenshot, against the disk-tier numbers in
#             benchmarks/results/fork_resume.json
#
# Usage (from repo root):  bash spikes/criu-memory-tier/run.sh [> evidence.json]
#   REPS=12          fork-latency repetitions (probe 4)
#   SKIP_BUILD=1     reuse existing shinken/sandbox-linux + shinken/sandbox-criu images
#
# Requires: Docker (containers run --privileged: CRIU needs CAP_SYS_ADMIN etc. — this is a
# latency-measurement rig, NOT a security posture; see REPORT.md). No network beyond builds.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPIKE="$REPO_ROOT/spikes/criu-memory-tier"
TOKEN="${SHINKEND_TOKEN:-spiketoken0123456789}"
REPS="${REPS:-12}"
VOL=criu-spike-ckpt
DONOR=criu-spike-donor

log() { echo "== $*" >&2; }
cleanup() {
  docker rm -f "$DONOR" criu-spike-fresh criu-spike-rep >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ -z "${SKIP_BUILD:-}" ]; then
  log "build base + criu spike images"
  docker build -f "$REPO_ROOT/images/linux/Dockerfile" -t shinken/sandbox-linux "$REPO_ROOT" >&2
  docker build -f "$SPIKE/Dockerfile.criu" -t shinken/sandbox-criu "$REPO_ROOT" >&2
fi

docker volume rm -f "$VOL" >/dev/null 2>&1 || true
cleanup

# ---------------------------------------------------------------- probe 1: kernel + criu check
log "probe 1: kernel config + criu check"
P1="$(docker run --rm --privileged shinken/sandbox-criu bash -c '
CFG=$(zcat /proc/config.gz 2>/dev/null | grep -E "CONFIG_(CHECKPOINT_RESTORE|NAMESPACES|PID_NS|MEM_SOFT_DIRTY|UNIX_DIAG|INET_DIAG|NETLINK_DIAG|MEMBARRIER|RSEQ|USERFAULTFD|PROC_CHILDREN)[=\" ]" | tr "\n" ";")
criu check >/tmp/chk.log 2>&1; BASIC=$?
criu check --extra >/tmp/chke.log 2>&1; EXTRA=$?
python3 - "$CFG" "$BASIC" "$EXTRA" <<PY
import json,sys
print(json.dumps({"kernel": open("/proc/sys/kernel/osrelease").read().strip(),
  "config": [c for c in sys.argv[1].split(";") if c],
  "criu_check_exit": int(sys.argv[2]), "criu_check_extra_exit": int(sys.argv[3]),
  "criu_check_tail": open("/tmp/chk.log").read().splitlines()[-1],
  "criu_check_extra_warnings": [l for l in open("/tmp/chke.log").read().splitlines() if "Warn" in l or "Error" in l]}))
PY')"
echo "$P1" | python3 -c 'import json,sys; json.load(sys.stdin)'  # validate

# ---------------------------------------------------------------- probe 2: trivial trees
log "probe 2: trivial trees (counter loop / pty / socketpair)"
P2="$(docker run --rm --init --privileged shinken/sandbox-criu bash -c '
mkdir -p /probe
cat > /probe/p_loop.sh <<"SH"
#!/bin/sh
echo $$ > /tmp/tree.pid
i=0; while true; do i=$((i+1)); echo $i > /tmp/state; sleep 1; done
SH
chmod +x /probe/p_loop.sh
cat > /probe/p_pty.py <<"PY"
import os, pty, time
m, s = pty.openpty()
open("/tmp/tree.pid","w").write(str(os.getpid()))
i = 0
while True:
    i += 1; os.write(m, b"ping%d\n" % i); time.sleep(0.05)
    open("/tmp/state","w").write("%d:%s" % (i, os.read(s, 64).decode().strip())); time.sleep(1)
PY
cat > /probe/p_sock.py <<"PY"
import os, socket, time
a, b = socket.socketpair()
if os.fork() == 0:
    a.close()
    while True:
        d = b.recv(64)
        if d: b.send(d.upper())
else:
    b.close(); open("/tmp/tree.pid","w").write(str(os.getpid())); i = 0
    while True:
        i += 1; a.send(b"msg%d" % i)
        open("/tmp/state","w").write("%d:%s" % (i, a.recv(64).decode())); time.sleep(1)
PY
results="["
for tgt in "/probe/p_loop.sh" "python3 /probe/p_pty.py" "python3 /probe/p_sock.py"; do
  rm -rf /ckpt2 /tmp/tree.pid /tmp/state; mkdir -p /ckpt2
  setsid $tgt </dev/null >/dev/null 2>&1 &
  for i in $(seq 1 50); do [ -f /tmp/state ] && break; sleep 0.1; done; sleep 1
  PID=$(cat /tmp/tree.pid)
  T0=$(date +%s%N)
  criu dump --tree "$PID" --images-dir /ckpt2 --shell-job >/dev/null 2>&1; D=$?
  T1=$(date +%s%N)
  criu restore --images-dir /ckpt2 --shell-job --restore-detached >/dev/null 2>&1; R=$?
  T2=$(date +%s%N)
  S1=$(cat /tmp/state 2>/dev/null); sleep 2.5; S2=$(cat /tmp/state 2>/dev/null)
  ALIVE=false; [ "$S1" != "$S2" ] && ALIVE=true
  results="$results{\"target\": \"$tgt\", \"dump_exit\": $D, \"dump_ms\": $(( (T1-T0)/1000000 )), \"restore_exit\": $R, \"restore_ms\": $(( (T2-T1)/1000000 )), \"resumed_execution\": $ALIVE},"
  kill -9 "$PID" $(pgrep -P "$PID" 2>/dev/null) >/dev/null 2>&1 || true
done
echo "${results%,}]"')"
echo "$P2" | python3 -c 'import json,sys; json.load(sys.stdin)'

# ---------------------------------------------------------------- probe 3: the desktop tree
log "probe 3: dump the full desktop tree (Xvfb+openbox+xterm+shinkend)"
docker run -d --init --privileged --name "$DONOR" \
  -v "$VOL:/ckpt" -v "$REPO_ROOT/sdk/python/src:/opt/shinken/src:ro" \
  shinken/sandbox-criu >/dev/null
docker cp "$SPIKE/ws_probe.py" "$DONOR:/usr/local/bin/ws_probe.py" >/dev/null

P3_DONOR="$(docker exec "$DONOR" bash -c '
setsid env SHINKEND_TOKEN='"$TOKEN"' /usr/local/bin/desktop-tree.sh </dev/null >/dev/null 2>&1 &
for i in $(seq 1 100); do [ -f /tmp/desktop-tree.pid ] && break; sleep 0.1; done; sleep 3
PID=$(cat /tmp/desktop-tree.pid)
PRE=$(PYTHONPATH=/opt/shinken/src SHINKEND_TOKEN='"$TOKEN"' python3 /usr/local/bin/ws_probe.py)
rm -rf /ckpt/desktop && mkdir -p /ckpt/desktop
T0=$(date +%s%N)
criu dump --tree "$PID" --images-dir /ckpt/desktop --tcp-close --shell-job >/tmp/dump.log 2>&1; D=$?
T1=$(date +%s%N)
# stage the donor runtime files a from-base-image restore target needs (probe 3b)
mkdir -p /ckpt/runtime-files/root/.cache/openbox
cp /root/.cache/openbox/openbox.log /ckpt/runtime-files/root/.cache/openbox/ 2>/dev/null || true
T2=$(date +%s%N)
criu restore --images-dir /ckpt/desktop --tcp-close --shell-job --restore-detached >/tmp/restore.log 2>&1; R=$?
T3=$(date +%s%N)
POST=$(PYTHONPATH=/opt/shinken/src SHINKEND_TOKEN='"$TOKEN"' python3 /usr/local/bin/ws_probe.py)
SZ=$(du -sk /ckpt/desktop | cut -f1)
echo "{\"tree_pid\": $PID, \"pre_dump_probe\": $PRE, \"dump_exit\": $D, \"dump_ms\": $(( (T1-T0)/1000000 )), \"image_kb\": $SZ, \"restore_inplace_exit\": $R, \"restore_inplace_ms\": $(( (T3-T2)/1000000 )), \"post_restore_probe\": $POST}"')"
echo "$P3_DONOR" | python3 -c 'import json,sys; json.load(sys.stdin)'

restore_fresh() {  # $1=image  $2=stage_runtime_files(0|1)
  docker rm -f criu-spike-fresh >/dev/null 2>&1 || true
  docker run -d --init --privileged --name criu-spike-fresh \
    -v "$VOL:/ckpt" -v "$REPO_ROOT/sdk/python/src:/opt/shinken/src:ro" "$1" >/dev/null
  docker cp "$SPIKE/ws_probe.py" criu-spike-fresh:/usr/local/bin/ws_probe.py >/dev/null
  docker exec criu-spike-fresh bash -c '
if [ "'"$2"'" = 1 ]; then
  mkdir -p -m1777 /tmp/.X11-unix
  mkdir -p /root/.cache/openbox
  cp -a /ckpt/runtime-files/root/.cache/openbox/openbox.log /root/.cache/openbox/ 2>/dev/null || true
fi
echo 500 > /proc/sys/kernel/ns_last_pid   # park helper PIDs above the dumped tree range
T0=$(date +%s%N)
criu restore --images-dir /ckpt/desktop --tcp-close --shell-job --restore-detached >/tmp/restore.log 2>&1; R=$?
T1=$(date +%s%N)
PROBE=$(PYTHONPATH=/opt/shinken/src SHINKEND_TOKEN='"$TOKEN"' python3 /usr/local/bin/ws_probe.py)
ERRS=$(grep -cE "Error" /tmp/restore.log || true)
echo "{\"restore_exit\": $R, \"restore_ms\": $(( (T1-T0)/1000000 )), \"probe\": $PROBE}"'
  docker rm -f criu-spike-fresh >/dev/null 2>&1 || true
}

log "probe 3b: restore in FRESH container (base image + staged runtime files)"
P3_FRESH="$(restore_fresh shinken/sandbox-criu 1)"
echo "$P3_FRESH" | python3 -c 'import json,sys; json.load(sys.stdin)'

log "probe 3c: docker commit donor -> restore in container from committed image"
TC0=$(python3 -c 'import time; print(time.time())')
docker commit "$DONOR" shinken/sandbox-criu-golden >/dev/null
TC1=$(python3 -c 'import time; print(time.time())')
P3_GOLDEN="$(restore_fresh shinken/sandbox-criu-golden 0)"
echo "$P3_GOLDEN" | python3 -c 'import json,sys; json.load(sys.stdin)'

# ---------------------------------------------------------------- probe 4: fork latency loop
log "probe 4: $REPS-rep fork latency loop (docker run + criu restore + WS + screenshot)"
P4="$(REPS="$REPS" TOKEN="$TOKEN" VOL="$VOL" SDK="$REPO_ROOT/sdk/python/src" python3 - <<'PY'
import json, os, subprocess, time, statistics
REPS, TOKEN, VOL, SDK = int(os.environ["REPS"]), os.environ["TOKEN"], os.environ["VOL"], os.environ["SDK"]
def run(cmd): return subprocess.run(cmd, capture_output=True, text=True)
reps = []
for i in range(REPS):
    run(["docker", "rm", "-f", "criu-spike-rep"])
    t0 = time.monotonic()
    r = run(["docker", "run", "-d", "--init", "--privileged", "--name", "criu-spike-rep",
             "-v", f"{VOL}:/ckpt", "-v", f"{SDK}:/opt/shinken/src:ro", "shinken/sandbox-criu-golden"])
    assert r.returncode == 0, r.stderr
    t1 = time.monotonic()
    script = f'''
echo 500 > /proc/sys/kernel/ns_last_pid
T0=$(date +%s%N)
criu restore --images-dir /ckpt/desktop --tcp-close --shell-job --restore-detached >/tmp/r.log 2>&1; R=$?
T1=$(date +%s%N)
PROBE=$(PYTHONPATH=/opt/shinken/src SHINKEND_TOKEN={TOKEN} python3 /usr/local/bin/ws_probe.py)
echo "{{\\"restore_exit\\": $R, \\"restore_ms\\": $(( (T1-T0)/1000000 )), \\"probe\\": $PROBE}}"
'''
    r = run(["docker", "exec", "criu-spike-rep", "bash", "-c", script])
    t2 = time.monotonic()
    inner = json.loads(r.stdout.strip().splitlines()[-1])
    reps.append({"rep": i, "create_ms": round((t1-t0)*1000, 1),
                 "restore_ms": inner["restore_ms"], "restore_exit": inner["restore_exit"],
                 "ws_ready_ms": inner["probe"].get("ws_ready_ms"),
                 "screenshot_ms": inner["probe"].get("screenshot_ms"),
                 "png_bytes": inner["probe"].get("png_bytes"),
                 "ok": inner["probe"].get("ok", False),
                 "total_ms": round((t2-t0)*1000, 1)})
    run(["docker", "rm", "-f", "criu-spike-rep"])
ok = [r for r in reps if r["ok"] and r["restore_exit"] == 0]
def st(k):
    v = sorted(r[k] for r in ok)
    return {"n": len(v), "min": v[0], "p50": round(statistics.median(v), 1),
            "mean": round(statistics.fmean(v), 1), "max": v[-1]} if v else {"n": 0}
print(json.dumps({"datapoints": reps, "ok_reps": len(ok),
                  "summary": {k: st(k) for k in ("create_ms", "restore_ms", "ws_ready_ms",
                                                 "screenshot_ms", "total_ms")}}))
PY
)"

# ---------------------------------------------------------------- assemble evidence
log "assemble evidence JSON (stdout)"
META="$(docker run --rm shinken/sandbox-criu bash -c \
  'echo "{\"criu\": \"$(criu --version | head -1 | cut -d" " -f2)\", \"glibc\": \"$(ldd --version | head -1 | grep -oE "[0-9.]+$")\", \"kernel\": \"$(uname -r)\", \"arch\": \"$(uname -m)\"}"')"
P1="$P1" P2="$P2" P3_DONOR="$P3_DONOR" P3_FRESH="$P3_FRESH" P3_GOLDEN="$P3_GOLDEN" P4="$P4" \
META="$META" COMMIT_S="$(python3 -c "print(round($TC1-$TC0,2))")" python3 - <<'PY'
import json, os, subprocess, datetime
e = lambda k: json.loads(os.environ[k])
ids = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
                     capture_output=True, text=True).stdout
images = {l.split()[0]: l.split()[1] for l in ids.splitlines() if "sandbox-criu" in l or "sandbox-linux:" in l}
print(json.dumps({
    "spike": "criu-memory-tier",
    "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "meta": e("META") | {"docker_commit_golden_s": float(os.environ["COMMIT_S"]), "images": images},
    "probe1_kernel_criu_check": e("P1"),
    "probe2_trivial_trees": e("P2"),
    "probe3_desktop_tree": {"donor_dump_and_inplace_restore": e("P3_DONOR"),
                             "fresh_container_staged_files": e("P3_FRESH"),
                             "fresh_container_committed_image": e("P3_GOLDEN")},
    "probe4_fork_latency": e("P4"),
}, indent=1))
PY

log "done (containers removed; volume $VOL and golden image kept for reruns)"
