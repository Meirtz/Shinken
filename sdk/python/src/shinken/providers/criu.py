"""CRIU memory-tier Docker provider — checkpoint/restore with LIVE process+memory state.

``CriuDockerProvider`` is the productized form of the positive CRIU spike
(``spikes/criu-memory-tier/``): an opt-in tier over :class:`DockerLocalProvider`
whose checkpoint carries the **running process tree** (open apps, mid-task
processes, in-memory program state), not just the filesystem. The mechanics are
exactly the spike's proven probes:

- ``snapshot()`` = ``criu dump --leave-running`` of the supervised desktop tree
  (the donor KEEPS RUNNING — a true checkpoint, probe 3d) **paired with**
  ``docker commit`` of the donor (probe 3c: the committed rootfs is what CRIU's
  by-path file reopens resolve against at restore — zero file staging);
- ``restore()``/``fork()`` = a fresh container from the committed image booted
  IDLE (``sleep infinity``), the PID counter parked above the dumped range
  (``ns_last_pid``, spike pitfall 2), then ``criu restore --restore-detached``
  brings the tree back — same PIDs, same memory, same X11 clients — and the
  normal readiness gate confirms ``shinkend`` answers.

**What survives a restore (process+memory+fs):** every process in the desktop
tree (Xvfb, openbox, xterm, ``shinkend`` and anything it spawned via
``launch_app``/``exec``), their heaps/threads/FDs/ptys, the X11 server with all
its clients and window state, and the donor's filesystem as of the commit.
**What does not:** established TCP connections are closed at dump
(``--tcp-close``) — an agent's WebSocket session to the donor/replica must
reconnect (the SDK's documented ``resume_stream`` semantics are exactly that
client-side story), and replicas get fresh host port mappings.

⚠ **PRIVILEGED CONTAINERS.** In-container CRIU needs CAP_SYS_ADMIN and friends,
so every container this provider runs — donors AND replicas — runs
``--privileged``, and the desktop tree runs as root. This tier buys
**checkpoint/fork latency and state fidelity on commodity Docker** (spike: fork
end-to-end p50 ~0.3 s vs ~7.6 s disk fork); it is **not an isolation posture**
— the production isolation answer remains the microVM tier (D1/D5). The
capability descriptor says so out loud (``requires_privileged=True``,
``snapshot_kind="process"``). Linux/Docker only; requires the
``shinken/sandbox-linux-criu`` image (``images/linux/Dockerfile.criu``).
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import replace
from typing import Any

from .base import ProviderCapabilities, ProviderError, SandboxHandle, SandboxSpec
from .docker import DockerLocalProvider, _run


class CriuDockerProvider(DockerLocalProvider):
    """Docker provider whose snapshots carry the live process tree (CRIU memory tier)."""

    capabilities = ProviderCapabilities(
        name="docker-criu",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=True,
        supports_fork=True,
        supports_gpu=False,
        supports_vsock=False,
        supports_egress_policy=False,
        supports_checkpoint=True,
        supports_resume=True,
        reset_strategy="recreate",
        isolation="container",
        transport="tcp_ws",
        display="x11",
        snapshot_kind="process",
        tier="local",
        requires_privileged=True,
        notes=(
            "privileged: true — every container (donor and replica) runs --privileged "
            "because in-container CRIU needs CAP_SYS_ADMIN; the desktop tree runs as root. "
            "This tier is a latency/state-fidelity feature, NOT an isolation posture — "
            "the production isolation boundary stays the microVM tier (D1/D5).",
            "Checkpoints carry PROCESS+MEMORY+FS state; established TCP connections are "
            "closed at dump (--tcp-close) — agent sessions reconnect (resume_stream).",
            "Linux/Docker only; needs the shinken/sandbox-linux-criu image.",
        ),
    )

    #: In-container mount point of the shared CRIU images volume.
    _CKPT_MOUNT = "/ckpt"
    #: Where the supervised desktop tree records its root PID (images/linux/start-criu.sh).
    _TREE_PIDFILE = "/tmp/shinken-tree.pid"

    def __init__(
        self,
        image: str = "shinken/sandbox-linux-criu",
        *,
        images_volume: str | None = None,
        pid_floor: int = 300,
        name_prefix: str = "shinken-criu",
        **kwargs: Any,
    ) -> None:
        for key in ("warm_pool_size", "warm_pool_spec", "warm_pool_claim_timeout"):
            if kwargs.pop(key, None):
                raise ProviderError(
                    "the warm-pool graft is files-only — incompatible with the CRIU "
                    "memory tier (restores need an IDLE container, not a live desktop)"
                )
        super().__init__(image=image, name_prefix=name_prefix, **kwargs)
        # One Docker named volume shared by every container this provider runs: the
        # donor dumps its CRIU images into /ckpt/<id>, replicas restore from it. The
        # default is a FIXED name (snapshot dirs inside are uuid-namespaced) so
        # throwaway provider instances don't strew volumes across the host; pass a
        # distinct ``images_volume`` per provider if concurrent provider instances
        # must not share cleanup_snapshots() blast radius.
        self.images_volume = images_volume or "shinken-criu-ckpt"
        # Donor containers park ns_last_pid here BEFORE booting the desktop tree, so
        # every PID/TID in the dumped tree lands above the early PIDs a fresh restore
        # target occupies (tini, the idle sleep, exec helpers) — CRIU restores EXACT
        # pids and any squatter fails the restore (spike pitfall 2).
        self.pid_floor = int(pid_floor)
        self._volume_created = False
        # snapshot tag -> {images_dir, token, park}; the spec registry stays in the
        # base class's self._snapshots.
        self._mem: dict[str, dict[str, Any]] = {}

    # --- container shaping --------------------------------------------------------

    def _tier_run_args(self, spec: SandboxSpec) -> list[str]:
        self._ensure_volume()
        return [
            "--privileged",  # in-container CRIU needs CAP_SYS_ADMIN — loud and on purpose
            "--init",  # PID 1 must reap the tree criu dump kills/orphans (spike pitfall 1)
            "-v",
            f"{self.images_volume}:{self._CKPT_MOUNT}",
            "-e",
            f"SHINKEN_CRIU_PID_FLOOR={self.pid_floor}",
        ]

    def _ensure_volume(self) -> None:
        if self._volume_created:
            return
        _run(
            [
                self.docker_bin,
                "volume",
                "create",
                "--label",
                "shinken.provider=docker-criu",
                self.images_volume,
            ]
        )
        self._volume_created = True

    # --- runtime-state primitives (memory tier) ------------------------------------

    def snapshot(self, handle: SandboxHandle, name: str | None = None) -> str:
        """Memory checkpoint (snapshot_kind=process): ``criu dump --leave-running`` of
        the supervised desktop tree — the donor keeps running, a TRUE checkpoint —
        paired with ``docker commit`` of the donor's filesystem (the rootfs CRIU's
        by-path file reopens resolve against at restore; spike probe 3c). Also records
        the donor's ``ns_last_pid`` so restore targets can park their PID counter
        above the dumped range. The donor's established TCP connections may be reset
        by the dump (``--tcp-close``); reconnect sessions after checkpointing."""
        cid = self._container_of(handle)
        self._ensure_volume()  # idempotent; the donor was created with it mounted
        snap = name or uuid.uuid4().hex[:12]
        tag = f"shinken-memsnap:{snap}"
        images_dir = f"{self._CKPT_MOUNT}/{snap}"
        script = (
            "set -e\n"
            f"mkdir -p {images_dir}\n"
            f'criu dump --tree "$(cat {self._TREE_PIDFILE})" --images-dir {images_dir} '
            "--tcp-close --shell-job --leave-running "
            # Belt-and-braces for inotify watches on overlayfs (whose file handles
            # CRIU cannot open): let irmap's path scan cover the dbus service dirs.
            # The image's bus config holds zero watches by design, but an app the
            # session launched later may have added its own.
            "--irmap-scan-path /usr/share/dbus-1 "
            f">{images_dir}/dump.log 2>&1 "
            f"|| {{ tail -n 40 {images_dir}/dump.log >&2; exit 9; }}\n"
            "cat /proc/sys/kernel/ns_last_pid\n"
        )
        out = _run(
            [self.docker_bin, "exec", "-u", "0", cid, "bash", "-c", script],
            timeout=self.startup_timeout,
        )
        try:
            last_pid = int(out.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            last_pid = 0
        _run([self.docker_bin, "commit", cid, tag], timeout=self.startup_timeout)
        self._snapshots[tag] = self._spec_from_handle(handle)
        self._mem[tag] = {
            "images_dir": images_dir,
            "token": handle.token,
            "park": max(last_pid + 128, self.pid_floor),
        }
        return tag

    def restore(self, snapshot_id: str) -> SandboxHandle:
        """Bring a memory checkpoint back LIVE: fresh ``--privileged --init`` container
        from the committed donor image, booted IDLE (``sleep infinity``), PID counter
        parked above the dumped range, then ``criu restore --restore-detached`` —
        the desktop tree resumes with its processes, memory, and X11 state intact,
        and the readiness gate confirms the restored ``shinkend`` answers. The handle
        carries the DONOR's token (the restored runtime keeps the token it held in
        memory). Fork is this same path (snapshot + restore)."""
        image = self._checkpoints.get(snapshot_id, {}).get("snapshot_id", snapshot_id)
        mem = self._mem.get(image)
        if mem is None:
            raise ProviderError(
                f"unknown memory snapshot {image!r} — the CRIU tier restores only "
                "snapshots taken by this provider instance"
            )
        spec = self._snapshots.get(image)
        spec = replace(spec, image=image) if spec is not None else SandboxSpec(image=image)
        for attempt in range(self._CREATE_ATTEMPTS):
            try:
                return self._restore_once(spec, image, mem)
            except ProviderError as exc:
                msg = str(exc).lower()
                lost_race = any(m in msg for m in self._PORT_RACE_MARKERS)
                if not lost_race or attempt == self._CREATE_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _restore_once(self, spec: SandboxSpec, image: str, mem: dict[str, Any]) -> SandboxHandle:
        handle = self._launch_container(spec, command=("sleep", "infinity"), token=mem["token"])
        try:
            script = (
                "set -e\n"
                # Park helper/squatter PIDs above the dumped tree range (pitfall 2).
                f"echo {mem['park']} > /proc/sys/kernel/ns_last_pid\n"
                f"criu restore --images-dir {mem['images_dir']} --tcp-close --shell-job "
                "--restore-detached >/tmp/criu-restore.log 2>&1 "
                "|| { tail -n 40 /tmp/criu-restore.log >&2; exit 9; }\n"
            )
            _run(
                [
                    self.docker_bin,
                    "exec",
                    "-u",
                    "0",
                    self._container_of(handle),
                    "bash",
                    "-c",
                    script,
                ],
                timeout=self.startup_timeout,
            )
            self._wait_ready(handle)
        except Exception:
            self.destroy(handle)
            raise
        handle.metadata["image"] = image
        handle.metadata["memory_restore"] = True
        return handle

    def delete_snapshot(self, snapshot_id: str) -> None:
        """Reclaim the committed image AND the CRIU images directory on the shared
        volume (a transient unprivileged container runs the ``rm``). Idempotent."""
        image = self._checkpoints.get(snapshot_id, {}).get("snapshot_id", snapshot_id)
        mem = self._mem.pop(image, None)
        super().delete_snapshot(snapshot_id)
        if mem is not None and self._volume_created:
            subprocess.run(
                [
                    self.docker_bin,
                    "run",
                    "--rm",
                    "-v",
                    f"{self.images_volume}:{self._CKPT_MOUNT}",
                    self.image,
                    "rm",
                    "-rf",
                    "--",
                    mem["images_dir"],
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )

    def cleanup_snapshots(self) -> int:
        """Reclaim every snapshot this provider took, then the shared images volume
        itself (best-effort — a volume still mounted by a live container is left)."""
        # The volume goes away wholesale below — skip the per-directory rm containers.
        self._mem.clear()
        count = super().cleanup_snapshots()
        if self._volume_created:
            subprocess.run(
                [self.docker_bin, "volume", "rm", "-f", self.images_volume],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            self._volume_created = False
        return count


# --- process-memory state verifier ---------------------------------------------------
# A marker that lives in PROCESS MEMORY inside the checkpointed tree — the state a
# files-only tier (docker commit / warm-pool graft) CANNOT carry across a restore.
# `start_memory_marker` plants a long-running python (spawned via the ACI `launch_app`
# verb, so it is a child of `shinkend` INSIDE the dumped tree) that increments a counter
# that exists only in its heap; `read_memory_marker` signals it (SIGUSR1) and reads the
# freshly written answer. After a memory-tier restore the SAME pid answers with the SAME
# nonce and a counter ≥ the value observed at checkpoint time; after any files-only
# restore there is no live process at all (the stale answer file is removed before
# signalling, so a baked-in file can never fake a pass).

MARKER_OUT = "/tmp/shinken-memory-marker"

_MARKER_SOURCE = """\
import os, signal, sys, time
nonce, out = sys.argv[1], sys.argv[2]
with open(out + ".pid", "w") as f:
    f.write(str(os.getpid()))
beats = 0  # lives ONLY in this process's memory

def _dump(_sig, _frame):
    tmp = "%s.%d.tmp" % (out, os.getpid())
    with open(tmp, "w") as f:
        f.write("%s:%d:%d" % (nonce, beats, os.getpid()))
    os.replace(tmp, out)

signal.signal(signal.SIGUSR1, _dump)
while True:
    beats += 1
    time.sleep(0.05)
"""

_QUERY_SOURCE = """\
import os, signal, sys, time
out = sys.argv[1]
pid = int(open(out + ".pid").read().strip())
try:
    os.remove(out)  # a stale (committed/baked-in) answer must never fake a pass
except FileNotFoundError:
    pass
try:
    os.kill(pid, signal.SIGUSR1)
except ProcessLookupError:
    print("no-such-process:%d" % pid)
    sys.exit(4)
for _ in range(150):
    if os.path.exists(out):
        print(open(out).read().strip())
        sys.exit(0)
    time.sleep(0.02)
print("marker-silent:%d" % pid)
sys.exit(5)
"""


class MemoryMarkerError(ProviderError):
    """The in-guest memory marker could not be started or queried."""


def start_memory_marker(env: Any, *, nonce: str | None = None, out: str = MARKER_OUT) -> dict:
    """Plant the process-memory marker inside the (future) checkpoint tree and return
    its baseline reading ``{"nonce", "beats", "pid"}``. ``env`` is a connected
    :class:`~shinken.Sandbox` whose runtime advertises ``launch_app`` + ``exec``."""
    nonce = nonce or uuid.uuid4().hex[:12]
    env.launch_app("python3", ["-c", _MARKER_SOURCE, nonce, out])
    deadline = time.monotonic() + 10.0
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            baseline = read_memory_marker(env, out=out)
            if baseline["nonce"] == nonce:
                return baseline
            last = MemoryMarkerError(f"stale marker answered: {baseline}")
        except MemoryMarkerError as exc:  # pidfile/process not up yet
            last = exc
        time.sleep(0.1)
    raise MemoryMarkerError(f"memory marker never came up: {last}")


def read_memory_marker(env: Any, *, out: str = MARKER_OUT, timeout: float = 15.0) -> dict:
    """Ask the LIVE marker process to dump its in-memory counter; returns
    ``{"nonce", "beats", "pid"}`` or raises :class:`MemoryMarkerError` (no such
    process / no answer — exactly what a files-only restore produces)."""
    res = env.exec(["python3", "-c", _QUERY_SOURCE, out], timeout=timeout)
    body = (res.get("stdout") or "").strip()
    if res.get("exit_code") != 0:
        raise MemoryMarkerError(
            f"memory marker query failed (exit {res.get('exit_code')}): "
            f"{body or res.get('stderr', '').strip()}"
        )
    try:
        nonce, beats, pid = body.split(":")
        return {"nonce": nonce, "beats": int(beats), "pid": int(pid)}
    except ValueError as exc:
        raise MemoryMarkerError(f"malformed marker answer: {body!r}") from exc


def verify_memory_marker(env: Any, baseline: dict, *, out: str = MARKER_OUT) -> dict:
    """Check PROCESS-MEMORY continuity against a pre-checkpoint ``baseline``: the same
    pid (CRIU restores exact PIDs) must answer with the same nonce and a counter at or
    above the baseline value (a restarted marker would answer near zero; a files-only
    restore has no live process at all). Returns ``{"ok": bool, ...reading/error}``."""
    try:
        now = read_memory_marker(env, out=out)
    except MemoryMarkerError as exc:
        return {"ok": False, "error": str(exc)}
    ok = (
        now["nonce"] == baseline["nonce"]
        and now["pid"] == baseline["pid"]
        and now["beats"] >= baseline["beats"]
    )
    return {"ok": ok, **now}
