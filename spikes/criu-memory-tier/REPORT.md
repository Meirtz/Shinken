# Spike #3 — CRIU memory-tier checkpoint/restore of the desktop tree (REPORT)

> **PRODUCTIZED (2026-06):** this spike's positive result is now a built provider tier —
> `shinken.CriuDockerProvider` (`sdk/python/src/shinken/providers/criu.py`,
> `images/linux/Dockerfile.criu` + `start-criu.sh`), with a live smoke
> (`scripts/criu_smoke.py`: an in-process-memory marker survives restore, donor stays live)
> and measured numbers (`benchmarks/bench_fork.py` memory mode →
> [benchmarks §1b](../../docs/engineering/benchmarks.md)). One pitfall found beyond this
> report during productization: the stock `at-spi-bus-launcher` holds a glib child-watch
> **pidfd**, which CRIU 3.17 cannot dump — the tier runs a launcher-less single-bus a11y
> stack instead (see `images/linux/shinken-criu-bus.conf`). This report below is the
> original spike evidence, kept as-is.

**Decision it informs:** the **memory checkpoint tier** (`snapshot_kind="process"`) of the
runtime-state design ([D5](../../docs/design/tech-decisions.md),
[runtime-state](../../docs/user/runtime-state.md), [status](../../docs/engineering/status.md)
"Runtime-state memory + fast tier") · **Harness:** [`run.sh`](run.sh) (staged probes),
[`Dockerfile.criu`](Dockerfile.criu), [`desktop-tree.sh`](desktop-tree.sh),
[`ws_probe.py`](ws_probe.py) · **Raw output:** [`evidence.json`](evidence.json)

**Result: POSITIVE.** In-container CRIU dump/restore of the full desktop process tree
(Xvfb + openbox + xterm + shinkend, 19 tasks) **works end-to-end on Docker Desktop arm64**
(kernel 6.12.76-linuxkit, CRIU 3.17.1, aarch64), including the fork-shaped case (restore into a
*fresh* container). The memory-tier fork is **~300 ms end-to-end** vs **~7.6 s** for the
implemented disk-tier fork and **~7.8 s** cold boot — a **~25× latency cut**, with the CRIU
restore itself ~40 ms and the restored `shinkend` accepting WebSocket connections ~1 ms later.
The known dead end was confirmed before this spike and avoided: `docker checkpoint` is
unavailable on Docker Desktop (daemon `experimental=false`, LinuxKit ships no criu); everything
below is **in-container CRIU in a `--privileged` container**, which is a *latency-measurement
rig, not a security posture* (see Caveats).

## Probe matrix

| # | Probe | Dump | Restore | Resumed execution? | Notes |
|---|---|---|---|---|---|
| 1 | Kernel + `criu check` | — | — | — | `CONFIG_CHECKPOINT_RESTORE=y`; `criu check` → "Looks good." (exit 0). Gaps: `MEM_SOFT_DIRTY` **unset** (no incremental/pre-dump), `USERFAULTFD` **unset** (no lazy restore), veth-pair creation unsupported (netns-restore only; irrelevant in-container) |
| 2a | `sh` counter loop (setsid tree) | ✅ ~38 ms | ✅ ~32 ms | ✅ counter advances | needed `--init` on the container (see PID-reaping pitfall) |
| 2b | python holding an **open pty pair** (master+slave, live round-trip) | ✅ ~39 ms | ✅ ~34 ms | ✅ | |
| 2c | forked child + **AF_UNIX socketpair** ping-pong | ✅ ~47 ms | ✅ ~35 ms | ✅ | |
| 3 | **Full desktop tree**: `shinkend` (exec'd tree root, 13 tokio threads) + Xvfb + openbox + xterm (+ bash on `pts/0`) | ✅ 49–75 ms, **~34 MB** images | — | — | `--tree <root> --tcp-close --shell-job`; survives 5 dump/restore cycles |
| 3a | Restore **in-place** (same container) | — | ✅ 34–43 ms | ✅ WS 1.2 ms, screenshot identical bytes, `xdotool` finds windows | original PIDs reclaimed |
| 3b | Restore in **fresh container, base image** | — | ✅ ~43 ms | ✅ | needs 3 preps: `/tmp/.X11-unix` dir, donor's `openbox.log` (exact bytes), `ns_last_pid` parking — see Pitfalls |
| 3c | Restore in **fresh container from `docker commit` of donor** (paired disk+memory checkpoint) | — | ✅ ~42 ms | ✅ | **zero file staging** — the committed layer carries every by-path-reopened file |
| 3d | `--leave-running` dump → restore in fresh container | ✅ | ✅ 43 ms | ✅ **donor and replica live in parallel**, both serving screenshots | true fork semantics; manual addendum, commands below |
| 4 | **12-rep fork latency loop** (3c shape) | — | 12/12 ✅ | 12/12 ✅ | table below |

What CRIU 3.17.1 handled inside the desktop dump with no flags beyond
`--tcp-close --shell-job`: the X11 listening sockets in **both** namespaces (filesystem
`/tmp/.X11-unix/X0` **and** abstract `@/tmp/.X11-unix/X0`), the established X client connections
(openbox, xterm, shinkend → Xvfb), xterm's pty pair + the bash session on `pts/0`, shinkend's
listening TCP socket on `0.0.0.0:8765` plus a TIME_WAIT remnant of the pre-dump probe
(`--tcp-close`), 13 tokio worker threads, and glibc-2.36 `rseq` registration on every thread
(kernel 6.12 has `PTRACE_GET_RSEQ_CONFIGURATION`). Probe 3d additionally showed
**checkpoint-anywhere**: a dump taken *mid-desktop-boot* (tree root still `desktop-tree.sh`,
Xvfb up, shinkend not yet exec'd) restored correctly and *finished booting* in the fresh
container (~2.1 s to WS-ready, screenshot of the partial desktop — 16 617 vs 27 381 PNG bytes).

## Latency (probe 4, N=12, all successful)

Fork shape per rep: `docker run` (fresh container, committed golden image) → `criu restore` →
SDK WebSocket handshake → first screenshot. Same host class as
[`benchmarks/results/fork_resume.json`](../../benchmarks/results/fork_resume.json)
(Apple M4 Pro, Docker Desktop 29.4.3, arm64).

| Stage | min | p50 | mean | max |
|---|---:|---:|---:|---:|
| `docker run` (container create+start) | 101.8 | 111.5 | 114.8 | 163.1 |
| `criu restore` (19 tasks, ~34 MB images) | 31 | **40.5** | 39.5 | 47 |
| WS handshake ready after restore | 1.1 | **1.2** | 1.2 | 1.6 |
| first screenshot | 8.0 | 8.4 | 8.4 | 9.0 |
| **end-to-end total** | 276.7 | **300.2** | 306.8 | 367.8 |

(The gap between the stage sums and the total is `docker exec` + interpreter startup overhead in
the measurement rig itself, not the restore path.)

Against the disk tier (same benchmark machine, `fork_resume.json`):

| Path | p50 total | vs memory tier |
|---|---:|---:|
| Cold boot (disk tier, implemented) | 7 808 ms | 26× slower |
| Disk-tier fork (`docker commit` + boot, implemented #209) | 7 573 ms | **25× slower** |
| **Memory-tier fork (this spike)** | **300 ms** | — |
| …of which CRIU restore→usable (restore+WS+screenshot) | **~50 ms** | |

One-time golden-checkpoint costs (amortized over N forks): `criu dump` 49–75 ms + `docker commit`
~0.9 s. The dominant per-fork cost is now **`docker run` (~110 ms), not the restore (~40 ms)** —
a pre-warmed container pool would put the floor near 50 ms even on this stack.

## Pitfalls found (each cost a failed restore; all are operational, none are kernel walls)

1. **PID-1 reaping** — `criu dump` kills the dumped tree by default; if the container's PID 1
   doesn't reap (e.g. plain `sleep infinity`), the dead root stays a zombie holding its PID and
   in-place restore fails `Can't fork for <pid>: File exists`. Fix: run restore-capable
   containers with `--init`.
2. **PID collisions in the restore target** — CRIU restores exact PIDs (via `clone3(set_tid)`);
   any helper process occupying a tree PID (even the `grep` in your own pipeline) fails the
   restore. Fix: park `ns_last_pid` above the dumped range before doing anything else in the
   target container. A production fork-provisioner that boots an idle container and restores
   immediately has a near-empty PID space anyway.
3. **CRIU reopens regular files BY PATH** — and validates size: a fresh container from the base
   image was missing (a) `/tmp/.X11-unix/` (bind of the X socket fails) and (b) openbox's
   runtime log `/root/.cache/openbox/openbox.log` (`has bad size 0 (expect 85)`). The
   *systemic* fix is probe 3c: pair the memory image with the donor's **filesystem** state
   (`docker commit`, i.e. exactly what the implemented disk tier already produces) — then zero
   staging is needed. The memory tier is an **add-on to** the disk tier, not a replacement,
   which matches the D5 design (checkpoint = disk snapshot + memory image).
4. **Child stdio must point at paths that exist everywhere** — the spike's tree supervisor
   (`desktop-tree.sh`) sends child stdio to `/dev/null` instead of `/tmp/*.log` precisely so
   the dump stays portable; with the 3c pairing this matters less, but it keeps the images
   restorable from the base image too.

## Caveats (read before quoting the numbers)

- **`--privileged` is a measurement rig, not an isolation story.** In-container CRIU needs
  CAP_SYS_ADMIN (or CAP_CHECKPOINT_RESTORE + friends on newer stacks). These numbers quantify
  the *latency win of resuming from a memory image*; the production isolation boundary in the
  design remains the **microVM tier** (D1), where the equivalent operation is a VM memory
  snapshot (Firecracker/QEMU/CRIU-in-guest), and the sub-second CoW fast tier stays a separate,
  ungated Phase-1 spike. Nothing here claims container-grade CRIU is shippable as a tenant
  boundary.
- **Single host class** — Apple M4 Pro / Docker Desktop / LinuxKit 6.12 / aarch64. The probes
  are rerunnable (`run.sh`) on other hosts; x86_64 and bare-metal Linux were not measured here.
- **Kernel feature gaps bound the roadmap, not this result:** no `MEM_SOFT_DIRTY` on the
  LinuxKit kernel → no incremental dumps (`criu pre-dump`) — every dump is a full ~34 MB write;
  no `USERFAULTFD` → no lazy/post-copy restore. Both are config (not arch) gaps and both have
  `criu check` receipts in `evidence.json`.
- **App surface** — the dumped desktop is the v0.0.1 reference stack (Xvfb/openbox/xterm/
  shinkend). Heavier apps (Chromium with its sandbox layers, GPU buffers) will exercise CRIU
  features this spike did not (e.g. seccomp filters dump fine in CRIU but Chromium's broker
  sockets to processes *outside* the tree would not) — measure before generalizing.
- The dump is quiesced-at-a-point: established WS *agent* connections die at dump (`--tcp-close`)
  by design; the SDK's documented screencast `resume_stream` reconnect semantics are exactly the
  client-side story for surviving a fork.

## Decision status

- The **memory tier is viable on the dev-machine substrate** (the same Docker Desktop class the
  disk tier runs on today) — the blocker is *engineering integration* (a provider that owns the
  golden-pair lifecycle: `criu dump` + `docker commit`, idle-target boot, `ns_last_pid` parking,
  restore, health gate), not kernel or aarch64 support.
- First-party numbers now exist for the fork-latency ladder: **cold boot 7.8 s → disk fork
  7.6 s → memory fork 0.30 s (restore itself 0.04 s)**. The remaining designed tier (sub-second
  CoW microVM fork) sits between 0.04 s and "sub-ms" and keeps its own spike.
- `docker checkpoint` (the daemon-integrated route) stays **dead** on Docker Desktop:
  `experimental=false` and no criu binary in LinuxKit — verified before this spike; the
  in-container route above does not depend on it.

## Reproduce

```bash
# from repo root; Docker required, ~5 min, no network beyond the image builds
bash spikes/criu-memory-tier/run.sh > spikes/criu-memory-tier/evidence.json
# REPS=20 SKIP_BUILD=1 bash spikes/criu-memory-tier/run.sh   # more fork reps, reuse images
```

Probe 3d (`--leave-running` true-fork addendum), by hand:

```bash
docker run -d --init --privileged --name donor -v criu-spike-ckpt:/ckpt \
  -v "$PWD/sdk/python/src:/opt/shinken/src:ro" shinken/sandbox-criu
docker cp spikes/criu-memory-tier/ws_probe.py donor:/usr/local/bin/
docker exec donor bash -c 'setsid env SHINKEND_TOKEN=t /usr/local/bin/desktop-tree.sh \
  </dev/null >/dev/null 2>&1 & sleep 8; criu dump --tree $(cat /tmp/desktop-tree.pid) \
  --images-dir /ckpt/lr --tcp-close --shell-job --leave-running'
docker commit donor golden && docker run -d --init --privileged --name fork1 \
  -v criu-spike-ckpt:/ckpt -v "$PWD/sdk/python/src:/opt/shinken/src:ro" golden
docker exec fork1 bash -c 'echo 500 > /proc/sys/kernel/ns_last_pid && criu restore \
  --images-dir /ckpt/lr --tcp-close --shell-job --restore-detached'
# donor and fork1 now both serve WS + screenshots on their own 8765
```
