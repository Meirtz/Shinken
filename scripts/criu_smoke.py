"""CRIU memory-tier live smoke: dump → restore → the PROCESS-MEMORY marker survives.

Proves the one thing the files-only Docker commit tier cannot:
a restored replica carries LIVE process+memory state. The flow, end to end on a real
Docker daemon (privileged containers — see ``images/linux/Dockerfile.criu``):

1. boot a donor (``CriuDockerProvider.create``), plant a golden FILE marker and the
   in-memory marker (a running python child of ``shinkend``, counter in its heap only);
2. ``checkpoint()`` = ``criu dump --leave-stopped`` + ``docker commit`` in one stopped
   consistency window, followed by donor resume;
3. donor-resumed check: the donor answers a screenshot AND its marker keeps beating after
   the checkpoint;
4. ``restore()`` a replica, reconnect (donor token), and verify BOTH markers: the
   golden file (files tier) and the memory marker — same pid, same nonce, counter
   ≥ the value read just before the dump (process-memory continuity);
5. destroy everything, reclaim snapshots + the images volume.

Run (Docker + shinken/sandbox-linux-criu image required):
    PYTHONPATH=sdk/python/src python scripts/criu_smoke.py
Prints one compact JSON receipt; exit 0 only if every check passed.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from shinken.providers.criu import (
    CriuDockerProvider,
    read_memory_marker,
    start_memory_marker,
    verify_memory_marker,
)

MARKER = "criu-golden-state-v1"
GUEST_FILE = "/tmp/shinken_criu_golden.txt"


def main() -> int:
    receipt: dict = {"smoke": "criu-memory-tier", "checks": {}}
    checks = receipt["checks"]
    t_total = time.monotonic()
    provider = CriuDockerProvider()
    donor = provider.create()
    replica = None
    try:
        env = provider.connect(donor)
        try:
            src = Path(tempfile.mkdtemp()) / "golden.txt"
            src.write_text(MARKER)
            env.put_file(str(src), GUEST_FILE)
            baseline = start_memory_marker(env)
            receipt["marker_baseline"] = baseline
            time.sleep(2.0)  # let the counter accumulate a meaningful continuity floor

            # read the counter RIGHT BEFORE the dump — the continuity floor
            pre_dump = read_memory_marker(env)
            t0 = time.monotonic()
            ckpt = provider.checkpoint(donor, name="smoke")
            receipt["checkpoint_ms"] = round((time.monotonic() - t0) * 1000, 1)
        finally:
            env.close()  # --tcp-close may reset the donor session at dump; reconnect below

        # The provider resumes the --leave-stopped donor after the atomic rootfs commit;
        # it must serve again and its in-memory marker must keep beating.
        env = provider.connect(donor)
        try:
            shot = env.screenshot()
            donor_now = read_memory_marker(env)
            checks["donor_still_serving"] = len(shot["png"]) > 8
            checks["donor_marker_still_beating"] = (
                donor_now["pid"] == baseline["pid"] and donor_now["beats"] >= pre_dump["beats"]
            )
        finally:
            env.close()

        t0 = time.monotonic()
        replica = provider.restore(ckpt)
        receipt["restore_to_usable_ms"] = round((time.monotonic() - t0) * 1000, 1)
        renv = provider.connect(replica)
        try:
            out = Path(tempfile.mkdtemp()) / "got.txt"
            renv.get_file(GUEST_FILE, str(out))
            checks["replica_inherited_golden_file"] = out.read_text().strip() == MARKER
            mem = verify_memory_marker(renv, pre_dump)
            receipt["replica_marker"] = mem
            checks["replica_memory_state_survived"] = mem["ok"]
            shot = renv.screenshot()
            checks["replica_serving_screenshots"] = len(shot["png"]) > 8
        finally:
            renv.close()
    finally:
        if replica is not None:
            provider.destroy(replica)
        provider.destroy(donor)
        provider.cleanup_snapshots()

    receipt["total_s"] = round(time.monotonic() - t_total, 1)
    ok = bool(checks) and all(checks.values())
    receipt["ok"] = ok
    print(json.dumps(receipt, indent=1))
    print(f"criu memory-tier smoke {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
