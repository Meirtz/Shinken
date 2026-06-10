#!/usr/bin/env python3
"""Drive `shinken.integrations.prorl_agent_server.ShinkenRuntime` the way a rollout
server's gateway node would: start -> prepare (upload+exec) -> RUN exec -> collect ->
stop. Prints one compact JSON receipt.

This is the runtime half of the ProRL-Agent-Server integration
(https://github.com/NVIDIA-NeMo/ProRL-Agent-Server — see the module docstring for the
contract and the `topology.yaml` snippet that loads this class via
`runtime.import_path`). No `polar` install is needed here: the script exercises the
duck-typed contract directly against a local Docker sandbox.

Usage:
    python scripts/prorl_runtime_example.py [--image shinken/sandbox-linux]
                                            [--golden SNAPSHOT_ID]

Requires Docker and the local sandbox image (`make image` / images/linux).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from shinken.integrations.prorl_agent_server import ShinkenRuntime


@dataclass
class RuntimeSpecLike:
    """Stand-in for the rollout server's RuntimeSpec (same attribute names)."""

    image: str
    kwargs: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)
    workdir: str | None = None
    cpus: float | None = None
    memory_mb: int | None = None


async def gateway_session(spec: RuntimeSpecLike, session_dir: Path) -> dict:
    receipt: dict = {"runtime": None, "checks": []}

    def check(name: str, ok: bool, **info) -> None:
        receipt["checks"].append({"name": name, "ok": bool(ok), **info})

    runtime = ShinkenRuntime(spec, f"example-{int(time.time())}", session_dir)
    t0 = time.perf_counter()
    await runtime.start()  # INIT: cold boot, or resume-from-golden via kwargs
    receipt["runtime"] = runtime.runtime_id
    check("start", True, ms=round((time.perf_counter() - t0) * 1000))
    try:
        # prepare: one upload + one exec, like a PrepareAction recipe
        task = session_dir / "task.json"
        task.write_text(json.dumps({"instruction": "demo"}))
        await runtime.upload_file(str(task), "/polar/session/task.json")
        result = await runtime.exec("cat /polar/session/task.json")
        check("prepare", result.return_code == 0 and "demo" in (result.stdout or ""))

        # RUN: the agent command (here: prove the in-guest ACI coordinates exist)
        result = await runtime.exec('echo "aci=$SHINKEND_ADDR token_set=${SHINKEND_TOKEN:+yes}"')
        check(
            "run_env",
            result.return_code == 0,
            stdout=(result.stdout or "").strip(),
        )

        # timeout semantics: the gateway maps return_code == -1 to "timeout"
        result = await runtime.exec("sleep 5", timeout_sec=0.5)
        check("timeout_is_minus_one", result.return_code == -1)

        # collect: artifacts out of the guest
        await runtime.exec("echo artifact > /polar/session/artifacts/out.txt")
        fetched = session_dir / "artifacts" / "out.txt"
        await runtime.download_file("/polar/session/artifacts/out.txt", str(fetched))
        check("collect", fetched.read_text().strip() == "artifact")
    finally:
        await runtime.stop()
        check("stop", True)
    receipt["ok"] = all(c["ok"] for c in receipt["checks"])
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="shinken/sandbox-linux")
    parser.add_argument("--golden", default=None, help="Shinken snapshot/checkpoint id")
    args = parser.parse_args()

    kwargs: dict = {"provider": "docker"}
    if args.golden:
        kwargs["golden_snapshot"] = args.golden
    spec = RuntimeSpecLike(image=args.image, kwargs=kwargs)

    with tempfile.TemporaryDirectory(prefix="shinken-prorl-") as session_dir:
        receipt = asyncio.run(gateway_session(spec, Path(session_dir)))
    print(json.dumps(receipt, separators=(",", ":")))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
