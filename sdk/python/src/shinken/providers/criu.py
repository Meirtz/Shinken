"""CRIU memory-tier Docker provider — checkpoint/restore with LIVE process+memory state.

``CriuDockerProvider`` is the productized form of the positive CRIU spike
(``spikes/criu-memory-tier/``): an opt-in tier over :class:`DockerLocalProvider`
whose checkpoint carries the **running process tree** (open apps, mid-task
processes, in-memory program state), not just the filesystem. The mechanics are
exactly the spike's proven probes:

- ``snapshot()`` = ``criu dump --leave-stopped`` of the supervised desktop tree,
  ``docker commit`` while every dumped task remains stopped, then ``SIGCONT`` in a
  finally-safe path. Memory and rootfs therefore share one consistency window while
  the donor still resumes after checkpointing;
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

import logging
import re
import time
import uuid
from dataclasses import replace
from typing import Any

from .base import ProviderCapabilities, ProviderError, SandboxHandle, SandboxSpec
from .docker import DockerLocalProvider, _run

_log = logging.getLogger("shinken.providers.criu")


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
    _IMAGES_DIR_RE = re.compile(r"^/ckpt/[0-9a-f]{32}$")
    _VOLUME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

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
        self._validate_volume_name(self.images_volume)
        # Donor containers park ns_last_pid here BEFORE booting the desktop tree, so
        # every PID/TID in the dumped tree lands above the early PIDs a fresh restore
        # target occupies (tini, the idle sleep, exec helpers) — CRIU restores EXACT
        # pids and any squatter fails the restore (spike pitfall 2).
        self.pid_floor = int(pid_floor)
        self._volume_created = False
        self._volumes_created: set[str] = set()
        self._image_volumes: dict[str, str] = {}
        # snapshot id -> {images_dir, park, images_volume}; bearer tokens stay only in
        # the checkpointed process memory and are recovered after restore.
        self._mem: dict[str, dict[str, Any]] = {}

    # --- container shaping --------------------------------------------------------

    def _tier_run_args(self, spec: SandboxSpec) -> list[str]:
        volume = self._image_volumes.get(str(spec.image), self.images_volume)
        self._ensure_volume(volume)
        return [
            "--privileged",  # in-container CRIU needs CAP_SYS_ADMIN — loud and on purpose
            "--init",  # PID 1 must reap the tree criu dump kills/orphans (spike pitfall 1)
            "-v",
            f"{volume}:{self._CKPT_MOUNT}",
            "--label",
            f"shinken.criu_images_volume={volume}",
            "-e",
            f"SHINKEN_CRIU_PID_FLOOR={self.pid_floor}",
        ]

    def _ensure_volume(self, volume: str | None = None) -> None:
        volume = volume or self.images_volume
        self._validate_volume_name(volume)
        if volume in self._volumes_created:
            return
        _run(
            [
                self.docker_bin,
                "volume",
                "create",
                "--label",
                "shinken.provider=docker-criu",
                volume,
            ]
        )
        self._volumes_created.add(volume)
        if volume == self.images_volume:
            self._volume_created = True

    @classmethod
    def _validate_volume_name(cls, volume: str) -> None:
        if not cls._VOLUME_RE.fullmatch(volume):
            raise ProviderError(f"invalid CRIU images volume name {volume!r}")

    def _remove_images_dir(self, volume: str, images_dir: str) -> None:
        self._validate_volume_name(volume)
        if not self._IMAGES_DIR_RE.fullmatch(images_dir):
            raise ProviderError(f"invalid CRIU images_dir {images_dir!r}")
        _run(
            [
                self.docker_bin,
                "run",
                "--rm",
                "-v",
                f"{volume}:{self._CKPT_MOUNT}",
                self.image,
                "rm",
                "-rf",
                "--",
                images_dir,
            ],
            timeout=60.0,
        )

    # --- runtime-state primitives (memory tier) ------------------------------------

    def _take_snapshot(
        self,
        handle: SandboxHandle,
        *,
        name: str | None = None,
        checkpoint_id: str | None = None,
        event_seq: int | None = None,
        agent_state_ref: str | None = None,
    ) -> str:
        """Dump memory and commit the rootfs inside one stopped consistency window."""
        cid = self._container_of(handle)
        spec = self._spec_from_handle(handle)
        self._validate_spec(spec)
        donor_image = str(handle.metadata.get("image") or self.image)
        images_volume = self._image_volumes.get(donor_image, self.images_volume)
        self._ensure_volume(images_volume)  # idempotent; donor already mounted it
        snapshot_uuid = uuid.uuid4().hex
        tag = f"shinken-memsnap:{snapshot_uuid}"
        images_dir = f"{self._CKPT_MOUNT}/{snapshot_uuid}"
        script = (
            "set -e\n"
            f"mkdir -p {images_dir}\n"
            f'criu dump --tree "$(cat {self._TREE_PIDFILE})" --images-dir {images_dir} '
            "--tcp-close --shell-job --leave-stopped "
            # Belt-and-braces for inotify watches on overlayfs (whose file handles
            # CRIU cannot open): let irmap's path scan cover the dbus service dirs.
            # The image's bus config holds zero watches by design, but an app the
            # session launched later may have added its own.
            "--irmap-scan-path /usr/share/dbus-1 "
            f">{images_dir}/dump.log 2>&1 "
            f"|| {{ tail -n 40 {images_dir}/dump.log >&2; exit 9; }}\n"
            "cat /proc/sys/kernel/ns_last_pid\n"
        )
        commit_completed = False
        try:
            out = _run(
                [self.docker_bin, "exec", "-u", "0", cid, "bash", "-c", script],
                timeout=self.startup_timeout,
            )
            try:
                last_pid = int(out.stdout.strip().splitlines()[-1])
            except (IndexError, ValueError):
                last_pid = 0
            tier_metadata = {
                "images_dir": images_dir,
                "park": max(last_pid + 128, self.pid_floor),
                "images_volume": images_volume,
            }
            record = self._snapshot_record(
                snapshot_id=tag,
                spec=spec,
                name=name,
                checkpoint_id=checkpoint_id,
                event_seq=event_seq,
                agent_state_ref=agent_state_ref,
                tier_metadata=tier_metadata,
            )
            committed = _run(
                [
                    self.docker_bin,
                    "commit",
                    *self._snapshot_commit_changes(record),
                    cid,
                    tag,
                ],
                timeout=self.startup_timeout,
            )
            commit_completed = True
            image_ref = committed.stdout.strip().splitlines()[-1]
            if not image_ref.startswith("sha256:"):
                raise ProviderError(
                    f"docker commit did not return an immutable sha256 image id: {image_ref!r}"
                )
        except BaseException as primary_error:
            # A failed dump/commit can still leave part or all of the tree stopped.  Resume
            # is secondary cleanup: report it as the cause, never as a replacement for the
            # primary snapshot error the caller needs to diagnose.
            try:
                self._resume_donor(cid)
            except BaseException as resume_error:
                self._cleanup_unpublished_snapshot(
                    images_volume,
                    images_dir,
                    image_tag=tag if commit_completed else None,
                )
                raise primary_error from resume_error
            self._cleanup_unpublished_snapshot(
                images_volume,
                images_dir,
                image_tag=tag if commit_completed else None,
            )
            raise
        else:
            # A successful commit is not a successful snapshot until the donor has been
            # resumed and its supervised tree root is observed outside a stopped state.
            try:
                self._resume_donor(cid)
            except BaseException:
                self._cleanup_unpublished_snapshot(images_volume, images_dir, image_tag=tag)
                raise
        if not commit_completed:  # pragma: no cover - the branches above are exhaustive
            raise AssertionError("snapshot commit did not complete")
        self._remember_snapshot_record(record, image_ref=image_ref)
        self._snapshots[tag] = spec
        return tag

    def _cleanup_unpublished_snapshot(
        self, images_volume: str, images_dir: str, *, image_tag: str | None
    ) -> None:
        """Best-effort cleanup for a snapshot that will not be returned to its caller."""
        if image_tag is not None:
            try:
                _run(
                    [self.docker_bin, "rmi", "-f", image_tag],
                    timeout=self.startup_timeout,
                )
            except Exception:  # noqa: BLE001 - preserve the snapshot/resume failure
                _log.error("failed to remove unpublished CRIU image %s", image_tag, exc_info=True)
        try:
            self._remove_images_dir(images_volume, images_dir)
        except Exception:  # noqa: BLE001 - cleanup must not mask snapshot/resume failure
            _log.error("failed to remove incomplete CRIU dump %s", images_dir, exc_info=True)

    def snapshot(self, handle: SandboxHandle, name: str | None = None) -> str:
        """Create a UUID-addressed process snapshot; ``name`` is metadata only."""
        return self._take_snapshot(handle, name=name)

    _DONOR_RESUME_ATTEMPTS = 2

    def _resume_donor(self, cid: str) -> None:
        # CRIU --leave-stopped stops every task in the tree. CONT to pid -1 resumes all
        # permissible stopped tasks except this docker-exec helper and PID 1. Verify the
        # supervised tree root left T/t state; retry once as a bounded fallback for a
        # transient docker-exec failure or a signal that did not take effect promptly.
        script = (
            f'tree_pid="$(cat {self._TREE_PIDFILE})"\n'
            "kill -s CONT -- -1\n"
            "for _ in 1 2 3 4 5; do\n"
            '  state="$(awk \'/^State:/ {print $2}\' "/proc/$tree_pid/status" '
            '2>/dev/null || true)"\n'
            '  case "$state" in R|S|D|I) exit 0 ;; *) sleep 0.05 ;; esac\n'
            "done\n"
            'echo "CRIU donor tree root $tree_pid is stopped or missing" >&2\n'
            "exit 10\n"
        )
        last_error: Exception | None = None
        for _attempt in range(self._DONOR_RESUME_ATTEMPTS):
            try:
                _run(
                    [self.docker_bin, "exec", "-u", "0", cid, "bash", "-c", script],
                    timeout=self.startup_timeout,
                )
                return
            except Exception as exc:  # noqa: BLE001 - retry cleanup, preserve cause
                last_error = exc
        assert last_error is not None
        raise ProviderError(
            f"failed to resume and verify CRIU donor {cid!r} after "
            f"{self._DONOR_RESUME_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def _hydrate_tier_metadata(self, record: dict[str, Any]) -> None:
        tier = record.get("tier_metadata")
        snapshot_id = record.get("snapshot_id")
        if not isinstance(tier, dict) or not isinstance(snapshot_id, str):
            return
        images_dir = tier.get("images_dir")
        park = tier.get("park")
        volume = tier.get("images_volume")
        if not isinstance(images_dir, str) or not self._IMAGES_DIR_RE.fullmatch(images_dir):
            raise ProviderError(f"invalid persisted CRIU images_dir {images_dir!r}")
        if not isinstance(park, int) or isinstance(park, bool) or park < 0:
            raise ProviderError(f"invalid persisted CRIU PID park value {park!r}")
        volume = str(volume or self.images_volume)
        self._validate_volume_name(volume)
        self._mem[snapshot_id] = {
            "images_dir": images_dir,
            "park": park,
            "images_volume": volume,
        }

    def _recover_process_token(self, container_id: str) -> str:
        result = _run(
            [
                self.docker_bin,
                "exec",
                "-u",
                "0",
                container_id,
                "bash",
                "-c",
                f"pid=$(cat {self._TREE_PIDFILE}); "
                'tr "\\0" "\\n" < "/proc/$pid/environ" | '
                "sed -n 's/^SHINKEND_TOKEN=//p' | head -n 1",
            ],
            timeout=self.startup_timeout,
        )
        token = result.stdout.strip()
        if not token:
            raise ProviderError("shinkend token was not recoverable from process state")
        return token

    def list(self) -> list[SandboxHandle]:
        """Rebuild live CRIU handles, including process token and images volume.

        The idle container's Config.Env token is not the restored shinkend token. Read
        the latter from the live tree and recover the non-secret volume from the run
        label (or the mount list for containers created by an older provider).
        """
        handles = super().list()
        for handle in handles:
            cid = self._container_of(handle)
            volume_result = _run(
                [
                    self.docker_bin,
                    "inspect",
                    "-f",
                    '{{index .Config.Labels "shinken.criu_images_volume"}}',
                    cid,
                ]
            )
            volume = volume_result.stdout.strip()
            if not volume or volume == "<no value>":
                mount_result = _run(
                    [
                        self.docker_bin,
                        "inspect",
                        "-f",
                        '{{range .Mounts}}{{if eq .Destination "/ckpt"}}{{.Name}}{{end}}{{end}}',
                        cid,
                    ]
                )
                volume = mount_result.stdout.strip()
            self._validate_volume_name(volume)
            image = str(handle.metadata.get("image") or self.image)
            self._image_volumes[image] = volume
            handle.metadata["criu_images_volume"] = volume
            handle.token = self._recover_process_token(cid)
        return handles

    def restore(self, snapshot_id: str) -> SandboxHandle:
        """Bring a memory checkpoint back LIVE: fresh ``--privileged --init`` container
        from the committed donor image, booted IDLE (``sleep infinity``), PID counter
        parked above the dumped range, then ``criu restore --restore-detached`` —
        the desktop tree resumes with its processes, memory, and X11 state intact,
        and the readiness gate confirms the restored ``shinkend`` answers. The handle
        carries the DONOR's token (the restored runtime keeps the token it held in
        memory). Fork is this same path (snapshot + restore)."""
        snapshot_key, image_ref, record = self._resolve_snapshot(snapshot_id)
        mem = self._mem.get(snapshot_key)
        if mem is None:
            raise ProviderError(
                f"unknown memory snapshot {snapshot_key!r} — no persistent CRIU tier metadata"
            )
        spec = self._snapshots.get(snapshot_key)
        self._validate_replay_metadata(spec, process_memory=True)
        lineage = self._lineage_metadata(snapshot_key, record, str(snapshot_id))
        self._image_volumes[image_ref] = str(mem["images_volume"])
        spec = (
            replace(
                spec,
                image=image_ref,
                metadata={**spec.metadata, **lineage},
            )
            if spec is not None
            else SandboxSpec(image=image_ref, metadata=lineage)
        )
        for attempt in range(self._CREATE_ATTEMPTS):
            try:
                handle = self._restore_once(spec, image_ref, mem)
                handle.metadata.update(
                    {**lineage, "restore_path": "criu", "pool_status": "not_applicable"}
                )
                return handle
            except ProviderError as exc:
                msg = str(exc).lower()
                lost_race = any(m in msg for m in self._PORT_RACE_MARKERS)
                if not lost_race or attempt == self._CREATE_ATTEMPTS - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _restore_once(self, spec: SandboxSpec, image: str, mem: dict[str, Any]) -> SandboxHandle:
        handle = self._launch_container(spec, command=("sleep", "infinity"))
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
            handle.token = self._recover_process_token(self._container_of(handle))
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
        resolved = self._resolve_snapshot_for_delete(snapshot_id)
        if resolved is None:
            return
        snapshot_key, _image_ref, _record = resolved
        mem = self._mem.get(snapshot_key)
        if mem is not None:
            volume = str(mem.get("images_volume") or self.images_volume)
            self._remove_images_dir(volume, mem["images_dir"])
            self._mem.pop(snapshot_key, None)
        super().delete_snapshot(snapshot_id)

    def cleanup_snapshots(self) -> int:
        """Reclaim this process's snapshot dirs/images, never the shared volume.

        The fixed default volume may also contain snapshots owned by another provider
        instance. Deleting it wholesale would turn process-local cleanup into cross-run
        data loss, so each persisted images directory is removed individually.
        """
        return super().cleanup_snapshots()


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
