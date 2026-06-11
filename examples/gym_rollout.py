"""Fork-native gym rollouts on Shinken — scripted end to end (no model API).

Demonstrates ``shinken.gym`` — the trainer-facing ``make/reset/step/evaluate`` shape over
the narrow waist, where **reset() is a fork from the task's golden checkpoint** (task
setup runs once; every other gym re-provisions the sandbox per episode):

1. ``make`` builds the golden checkpoint once (boot base → ``task.setup`` → checkpoint);
2. a :class:`ShinkenGymPool` forks N replicas **in parallel** (one SharedLoop client
   thread, one cross-replica FrameCache);
3. a scripted policy emits **raw model text** — an XML tool call and the Shinken tag
   dialect — routed through ``shinken.dialect.parse_actions`` by ``step()``;
4. ``evaluate()`` scores each replica with the task verifier (golden-state inheritance);
5. the typed episodes export to the HF-``datasets`` columnar shape (``to_hf_dataset``
   falls back to a plain dict-of-lists when ``datasets`` isn't installed).

Run (Docker + the local image required):

    make sandbox-image                      # once: build shinken/sandbox-linux
    PYTHONPATH=sdk/python/src python examples/gym_rollout.py [n_envs]

The provider's warm pool is sized to the env count, so resets are served by pre-booted
containers (graft, no boot) — the measured wedge (docs/engineering/benchmarks.md §1).
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

from shinken.gym import GymTask, MultiTurnDataloader, ShinkenGymPool, to_hf_dataset
from shinken.providers.docker import DockerLocalProvider


def make_marker_task(workdir: Path) -> GymTask:
    """Golden state = a marker file placed ONCE at setup; the verifier reads it back from
    each replica, so reward 1.0 means the fork really inherited the golden state."""
    marker = workdir / "marker.txt"
    marker.write_text("golden-state")
    marker.chmod(0o644)

    def setup(sess):  # runs once, into the golden checkpoint
        sess.put_file(str(marker), "/tmp/gym_marker.txt")

    def verify(sess):  # runs per replica
        out = workdir / "readback.txt"
        sess.get_file("/tmp/gym_marker.txt", str(out))
        return 1.0 if out.read_text() == "golden-state" else 0.0

    return GymTask(
        "gym-rollout-demo",
        instruction="every forked episode must inherit the golden marker",
        setup=setup,
        verify=verify,
    )


def scripted_policy(step: int) -> str:
    """A stand-in for the model: emits RAW TEXT in two real grammars — an XML tool call
    (Qwen/Hermes JSON-in-XML) on the first turn, the Shinken tag dialect afterwards."""
    if step == 0:
        return (
            "<tool_call>\n"
            '{"name": "computer_use", "arguments": '
            '{"action": "left_click", "coordinate": [320, 240]}}\n'
            "</tool_call>"
        )
    if step == 1:
        return '<actions><type_text text="forked!"/><key combo="ctrl+s"/></actions>'
    return "<done/>"


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    workdir = Path(tempfile.mkdtemp(prefix="shinken-gym-demo-"))
    task = make_marker_task(workdir)
    provider = DockerLocalProvider(
        image="shinken/sandbox-linux", startup_timeout=120.0, warm_pool_size=n
    )
    try:
        with ShinkenGymPool(task, provider, n) as pool:
            # --- make: golden checkpoint ONCE (boot base + setup + checkpoint) ---------
            t0 = time.perf_counter()
            pool.make()
            print(f"make (golden build, once): {time.perf_counter() - t0:.2f}s")

            # --- parallel reset: N forks from the ONE golden checkpoint ----------------
            t0 = time.perf_counter()
            results = pool.reset()
            wall = time.perf_counter() - t0
            per_env = [info["reset_ms"] for _obs, info in results]
            print(
                f"reset x{n} (parallel fork fan-out): {wall:.2f}s wall — per-env "
                f"reset_ms p50 {statistics.median(per_env):.0f} "
                f"({', '.join(f'{ms:.0f}' for ms in per_env)})"
            )

            # --- steps: raw model text through parse_actions ---------------------------
            for step_i in range(3):
                outs = pool.step([scripted_policy(step_i)] * n)
                statuses = [
                    "done" if done else f"{len(info.get('results', []))} actions ok"
                    for _obs, _r, done, info in outs
                ]
                print(f"step {step_i}: {statuses}")
            rewards = [ep.reward for env in pool.envs for ep in env.episodes]
            print(f"episode rewards (golden-state inheritance per fork): {rewards}")

            # --- export: the training-native columnar shape ----------------------------
            ds = to_hf_dataset(pool.episodes)
            if isinstance(ds, dict):  # `datasets` not installed — same columns, plain dict
                print(
                    f"export: plain dict-of-lists, {len(ds['episode'])} rows, "
                    f"columns {sorted(ds)}"
                )
            else:
                print(f"export: datasets.Dataset with {len(ds)} rows: {ds.column_names}")

            # --- the same pool through the MultiTurnDataloader collection shape --------
            loader = MultiTurnDataloader(pool, total_episodes=n)
            for batch in loader:
                responses = [scripted_policy(step) for step in batch["step"]]
                loader.async_step({"env_id": batch["env_id"], "responses": responses})
            print(
                f"dataloader: collected {loader._completed} more episodes "
                f"({len(pool.episodes)} total) — every boundary a fork, never a re-boot"
            )
    finally:
        provider.shutdown_pool()  # reclaim the warm containers
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
