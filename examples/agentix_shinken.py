"""Shinken as an Agentix-shaped sandbox provider — scripted end to end (no model API).

Demonstrates ``shinken.integrations.agentix``: :class:`ShinkenAgentixProvider` exposes
``DockerLocalProvider`` + the typed ACI through the ``SandboxProvider`` lifecycle Agentix
(<https://github.com/Agentix-Project/Agentix>) orchestrates — ``async create/delete/get``
plus the scoped ``session()`` context manager — with no agentix import required (the shape
is structural; ``register_with_agentix()`` wires it into an installed Agentix).

The runtime-state extra their backends lack: checkpoint a prepared sandbox once, then
construct the provider with ``golden=<checkpoint>`` and every ``create()`` forks from that
golden state instead of cold-booting.

Run (Docker + the local image required):

    make sandbox-image                      # once: build shinken/sandbox-linux
    PYTHONPATH=sdk/python/src python examples/agentix_shinken.py
"""

from __future__ import annotations

import asyncio
import time

from shinken.integrations.agentix import ShinkenAgentixProvider
from shinken.providers.docker import DockerLocalProvider


async def main() -> int:
    docker = DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    provider = ShinkenAgentixProvider(docker)

    # Their create -> use -> delete lifecycle, scoped by session() exactly as in Agentix.
    golden: str
    async with provider.session({"image": "shinken/sandbox-linux"}) as sandbox:
        print(f"sandbox: {sandbox.sandbox_id} at {sandbox.runtime_url} ({sandbox.status})")
        info = await provider.get(sandbox.sandbox_id)
        print(f"get(): status={info.status}")
        health = await sandbox.health()
        print(f"health(): {health['status']} rtt={health['rtt_ms']:.1f}ms")

        # The handle carries the typed ACI instead of pickle-RPC: observe + act.
        aci = sandbox.aci()
        shot = await asyncio.to_thread(aci.screenshot)
        print(f"screenshot: {len(shot['bytes'])} bytes ({shot['w']}x{shot['h']})")
        await asyncio.to_thread(aci.click, x=200, y=150)
        await asyncio.to_thread(aci.type_text, "agentix-shaped provider over the typed ACI")

        # Runtime-state extra: name this prepared state as the golden checkpoint.
        golden = await provider.checkpoint(sandbox.sandbox_id, name="agentix-golden")
        print(f"golden checkpoint: {golden}")

    # Fork-native creates: every create() resumes the golden state, no cold boot, no setup.
    forked = ShinkenAgentixProvider(docker, golden=golden)
    t0 = time.perf_counter()
    async with forked.session(None) as replica:
        dt = time.perf_counter() - t0
        print(f"forked create(): {replica.sandbox_id} ready in {dt:.1f}s from {golden}")
        shot = await asyncio.to_thread(replica.aci().screenshot)
        print(f"replica screenshot: {len(shot['bytes'])} bytes")
    docker.delete_snapshot(golden)  # reclaim the demo's snapshot image

    # Optional: register into an installed Agentix (soft import; not required above).
    try:
        from shinken.integrations.agentix import register_with_agentix

        register_with_agentix("shinken")
        print("registered as the 'shinken' provider in the installed Agentix registry")
    except Exception as exc:  # noqa: BLE001 — agentix absent is the normal case here
        print(f"agentix not installed ({exc.__class__.__name__}); structural shape only")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
