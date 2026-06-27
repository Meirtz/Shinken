"""ng_run entrypoint: the Shinken computer-use resources server.

NeMo Gym launches this file as ``cd <workspace>/resources_servers/shinken_cua && python
app.py`` inside a per-server venv (built from the sibling ``requirements.txt``). Build
that workspace OUTSIDE the repo with ``python examples/nemo_gym/make_workspace.py`` —
see ``examples/nemo_gym/README.md``.

Environment knobs (read here, not from YAML — NeMo Gym validates its own config keys):
- ``CUA_GYM_TASKS``     task-bundle root (default: the two demo bundles shipped in-repo)
- ``SHINKEN_IMAGE``     sandbox image (default: the provider's default, shinken/sandbox-linux)
- ``SHINKEN_STATE_FIDELITY`` trusted caller opt-in: ``filesystem`` or ``process_memory``
- ``SHINKEN_IDLE_TTL_S`` abandoned rollout TTL (default: 900)
- ``SHINKEN_MAX_GOLDENS`` maximum cached task snapshots (default: 32)
- ``SHINKEN_GOLDEN_TTL_S`` idle golden snapshot TTL (default: 3600)
- ``SHINKEN_REAP_INTERVAL_S`` active maintenance interval (default: 30)
- ``SHINKEN_MAX_PENDING_CLEANUP`` cleanup backlog high-water mark (default: 64)
- ``SHINKEN_CLEANUP_RETRY_BATCH`` attempts per cleanup-queue drain (default: 16)
"""

from __future__ import annotations

import os
from pathlib import Path

from shinken import DockerLocalProvider, SandboxSpec
from shinken.integrations.cua_gym import CuaGymTaskSource
from shinken.integrations.nemo_gym import (
    ShinkenComputerEngine,
    build_resources_server_cls,
)

DEMO_TASKS = Path(__file__).resolve().parent / "tasks"


def engine_factory(_config: object) -> ShinkenComputerEngine:
    root = os.environ.get("CUA_GYM_TASKS") or str(DEMO_TASKS)
    kwargs: dict = {"name_prefix": "shinken-nemogym"}
    if os.environ.get("SHINKEN_IMAGE"):
        kwargs["image"] = os.environ["SHINKEN_IMAGE"]
    provider = DockerLocalProvider(**kwargs)
    fidelity = os.environ.get("SHINKEN_STATE_FIDELITY")
    if fidelity is None and Path(root).resolve() == DEMO_TASKS.resolve():
        fidelity = "filesystem"
    if fidelity not in (None, "filesystem", "process_memory"):
        raise ValueError("SHINKEN_STATE_FIDELITY must be filesystem or process_memory")
    spec = SandboxSpec(state_fidelity=fidelity) if fidelity is not None else None
    lifecycle = {}
    float_knobs = {
        "SHINKEN_IDLE_TTL_S": "idle_ttl_s",
        "SHINKEN_GOLDEN_TTL_S": "golden_ttl_s",
        "SHINKEN_REAP_INTERVAL_S": "reap_interval_s",
    }
    for env_name, argument in float_knobs.items():
        if env_name in os.environ:
            lifecycle[argument] = float(os.environ[env_name])
    if "SHINKEN_MAX_GOLDENS" in os.environ:
        lifecycle["max_goldens"] = int(os.environ["SHINKEN_MAX_GOLDENS"])
    if "SHINKEN_MAX_PENDING_CLEANUP" in os.environ:
        lifecycle["max_pending_cleanup"] = int(
            os.environ["SHINKEN_MAX_PENDING_CLEANUP"]
        )
    if "SHINKEN_CLEANUP_RETRY_BATCH" in os.environ:
        lifecycle["cleanup_retry_batch"] = int(
            os.environ["SHINKEN_CLEANUP_RETRY_BATCH"]
        )
    return ShinkenComputerEngine(
        provider, CuaGymTaskSource(root), spec=spec, **lifecycle
    )


ShinkenComputerResourcesServer = build_resources_server_cls(engine_factory)


if __name__ == "__main__":
    ShinkenComputerResourcesServer.run_webserver()
