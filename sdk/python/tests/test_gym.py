"""Fork-native gym adapter (shinken.gym).

Fixture-tested against a recording fork provider + the in-process mock runtime: golden
setup runs ONCE and every reset() is a fork (never a setup rerun), step() routes canonical
ACI dicts AND raw model text (dialect + XML tool-call grammars), episodes land as typed
Trajectories, the HF-datasets exporter keeps the columnar contract, and the
MultiTurnDataloader-shaped iterator collects with auto-reset-as-fork. One Docker-gated
live test (SHINKEN_DOCKER_TESTS=1) gates reset_ms p50 and golden-state inheritance.
"""

from __future__ import annotations

import collections
import json
import os
import sys

import pytest

import shinken
from shinken import eval as ev
from shinken import gym as g
from shinken.runtime.trajectory import Trajectory

# ---------------------------------------------------------------------------- fixtures


class _ForkProvider:
    """Recording lifecycle provider: connect() dials the in-process mock shinkend and
    passes connect kwargs through, so SharedLoop/FrameCache multiplexing is exercised
    for real; resume() asserts every replica forks from the ONE golden checkpoint."""

    def __init__(self, addr: str) -> None:
        self.addr = addr
        self.calls: collections.Counter = collections.Counter()
        self.destroyed: list = []

    def create(self, spec=None):
        self.calls["create"] += 1
        return f"base-{self.calls['create']}"

    def connect(self, handle, **kwargs):
        self.calls["connect"] += 1
        return shinken.connect(self.addr, **kwargs)

    def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None):
        self.calls["checkpoint"] += 1
        return "golden-1"

    def resume(self, ckpt):
        assert ckpt == "golden-1"  # every fork comes from the single golden checkpoint
        self.calls["resume"] += 1
        return f"replica-{self.calls['resume']}"

    def destroy(self, handle):
        self.calls["destroy"] += 1
        self.destroyed.append(handle)

    def delete_snapshot(self, ckpt):
        self.calls["delete_snapshot"] += 1


class _TransitionSession:
    """Small deterministic env whose observation bytes expose state transitions.

    The regular mock runtime serves a fixed 1x1 PNG, which cannot prove whether an
    exported row contains s_t or s_{t+1}. This session emits distinct bytes per state.
    """

    def __init__(self) -> None:
        self.state = 0
        self.closed = False

    def _observation(self) -> dict:
        image = f"state-{self.state}".encode()
        return {"bytes": image, "png": image, "format": "png", "w": 1, "h": 1}

    def screenshot(self, **_kwargs):
        return self._observation()

    def step(self, actions, *, observe=None):
        del observe
        self.state += 1
        return {
            "observation": self._observation(),
            "results": [{"verb": action["verb"], "ok": True} for action in actions],
        }

    def close(self):
        self.closed = True


class _TransitionProvider:
    """Lifecycle stub for transition-semantics and dataloader tests."""

    def __init__(self) -> None:
        self.next_replica = 0

    def create(self, spec=None):
        del spec
        return "base"

    def connect(self, handle, **_kwargs):
        del handle
        return _TransitionSession()

    def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None):
        del handle, name, event_seq, agent_state_ref
        return "golden-transition"

    def resume(self, checkpoint):
        assert checkpoint == "golden-transition"
        self.next_replica += 1
        return f"replica-{self.next_replica}"

    def destroy(self, handle):
        del handle

    def delete_snapshot(self, checkpoint):
        assert checkpoint == "golden-transition"


def _typed_hi_task(setups: list | None = None) -> g.GymTask:
    """Verifier reads the mock's OBSERVED state (not the task's inputs): reward 1.0 only
    when the episode actually typed 'hi'."""

    def verify(sess):
        return 1.0 if sess.query("state").get("typed") == "hi" else 0.0

    def setup(sess):
        if setups is not None:
            setups.append(1)

    return g.GymTask("typed-hi", instruction="type hi then stop", setup=setup, verify=verify)


# ------------------------------------------------------------------- make/reset lifecycle


def test_surface_pin():
    # The gym shape trainers consume: make/reset/step/evaluate/close (+ module make()).
    for method in ("make", "reset", "step", "evaluate", "close"):
        assert callable(getattr(g.ShinkenGymEnv, method)), method
    assert callable(g.make)


def test_make_runs_setup_once_and_every_reset_is_a_fork(mock_shinkend):
    prov = _ForkProvider(mock_shinkend)
    setups: list = []
    env = g.make(_typed_hi_task(setups), prov)
    # golden build: one base, setup ran once, one checkpoint, base reclaimed
    assert setups == [1]
    assert prov.calls["create"] == 1 and prov.calls["checkpoint"] == 1
    assert prov.destroyed == ["base-1"]

    obs, info = env.reset()
    assert prov.calls["resume"] == 1
    assert obs["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert info["reset_ms"] > 0 and info["golden_checkpoint"] == "golden-1"
    assert info["task"] == "typed-hi" and info["instruction"] == "type hi then stop"

    env.reset()  # second episode: ANOTHER fork — no setup rerun, no new checkpoint
    assert prov.calls["resume"] == 2
    assert setups == [1] and prov.calls["checkpoint"] == 1
    assert "replica-1" in prov.destroyed  # the stale replica was torn down

    env.dispose()
    assert "replica-2" in prov.destroyed
    assert prov.calls["delete_snapshot"] == 1  # golden snapshot reclaimed


def test_step_before_reset_is_typed(mock_shinkend):
    env = g.ShinkenGymEnv(_typed_hi_task(), _ForkProvider(mock_shinkend))
    with pytest.raises(g.GymError, match="reset"):
        env.step({"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 1}})


def test_session_property_exposes_the_live_replica(mock_shinkend):
    """`env.session` is the public handle a harness uses for out-of-band probes
    (readiness waits, auxiliary captures) against the current fork."""
    env = g.ShinkenGymEnv(_typed_hi_task(), _ForkProvider(mock_shinkend))
    with pytest.raises(g.GymError, match="reset"):
        _ = env.session
    with env:
        env.reset()
        assert env.session.query("platform") == "linux"  # the same live session verify sees


def test_observe_args_codec_levers_reach_the_wire_on_reset(mock_shinkend):
    """The reset observation honors the full screenshot lever set — format, quality AND
    max_long_edge — so a codec-tier study serves the model the tier's settings from the
    very first frame (asserted from the mock's recorded wire action)."""
    env = g.ShinkenGymEnv(
        _typed_hi_task(),
        _ForkProvider(mock_shinkend),
        observe_args={"format": "jpeg", "quality": 50, "max_long_edge": 768},
    )
    with env:
        env.reset()
        sent = env.session.query("state")["screenshots"][-1]
        assert sent["format"] == "jpeg"
        assert sent["quality"] == 50
        assert sent["max_long_edge"] == 768


# ------------------------------------------------------------------------- step routing


def test_step_accepts_canonical_dicts_and_lists(mock_shinkend):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        click = {"verb": "click", "target": {"kind": "point_px", "x": 10, "y": 20}}
        obs, reward, done, info = env.step(click)
        assert not done and reward is None
        assert [r["verb"] for r in info["results"]] == ["click"]
        assert all(r["ok"] for r in info["results"])
        obs, _, _, info = env.step(
            [{"verb": "type_text", "text": "hi"}, {"verb": "key", "keys": "ctrl+s"}]
        )
        assert [r["verb"] for r in info["results"]] == ["type_text", "key"]
        # the actions actually landed (observed effects, not echoes)
        state = env._sess.query("state")
        assert {"x": 10, "y": 20} in [{"x": c["x"], "y": c["y"]} for c in state["clicks"]]
        assert state["typed"] == "hi" and "ctrl+s" in state["keys"]
        assert obs["png"][:8] == b"\x89PNG\r\n\x1a\n"  # post-step frame, fused via step()


def test_trajectory_and_exporter_pair_agent_input_with_action():
    """A transition is (s_t, a_t, s_{t+1}); the HF row must use (s_t, a_t)."""
    task = g.GymTask("transition", instruction="advance once")
    with g.make(task, _TransitionProvider()) as env:
        initial, _ = env.reset()
        action = {"verb": "type_text", "text": "advance"}
        post, reward, done, _ = env.step(action)
        assert initial["bytes"] == b"state-0"
        assert post["bytes"] == b"state-1"
        assert reward is None and not done

        _, _, done, _ = env.step("<done/>")
        assert done
        episode = env.episodes[-1]

    first, terminal = episode.trajectory.steps
    assert first.observation == initial  # reset's s0 is the first policy input
    assert first.actions == [action]
    assert first.next_observation == post
    assert terminal.observation == post  # the next turn consumes the previous s_{t+1}
    assert terminal.actions == []
    assert terminal.next_observation is not None

    serialized = episode.trajectory.to_dict()["steps"]
    assert episode.trajectory.metadata["transition_semantics"] == "s_t_action_s_t_plus_1_v1"
    assert serialized[0]["observation"]["bytes"] == b"state-0"
    assert serialized[0]["next_observation"]["bytes"] == b"state-1"

    records = g.episodes_to_records([episode])
    assert records["image"] == [b"state-0", b"state-1"]
    assert json.loads(records["actions_json"][0]) == [action]


def test_step_routes_raw_model_text_dialect_and_xml(mock_shinkend):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        # native Shinken tag dialect
        _, _, done, info = env.step('<actions><type_text text="hi"/></actions>')
        assert not done and [r["verb"] for r in info["results"]] == ["type_text"]
        # wild-type XML tool-call grammar (Qwen/Hermes JSON-in-XML), format="auto"
        args = '{"action": "left_click", "coordinate": [135, 742]}'
        xml = f'<tool_call>\n{{"name": "computer_use", "arguments": {args}}}\n</tool_call>'
        _, _, done, info = env.step(xml)
        assert not done and [r["verb"] for r in info["results"]] == ["click"]
        clicks = env._sess.query("state")["clicks"]
        assert {"x": 135, "y": 742} in [{"x": c["x"], "y": c["y"]} for c in clicks]


def test_step_malformed_text_raises_teaching_error(mock_shinkend):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        with pytest.raises(shinken.DialectError):
            env.step("<click x=10>")  # unquoted attribute — malformed, never silently dropped


# --------------------------------------------------------------- episode end + evaluate


def test_done_control_action_scores_and_emits_typed_trajectory(mock_shinkend):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        env.step('<actions><type_text text="hi"/></actions>')
        obs, reward, done, info = env.step("<done/>")
        assert done and reward == 1.0 and info["terminal"] == "done"
        ep = env.episodes[-1]
        assert isinstance(ep.trajectory, Trajectory)
        assert ep.trajectory.exit_reason == "task_complete" and ep.trajectory.terminal == "done"
        assert len(ep.trajectory.steps) == 2 and ep.reward == 1.0
        assert ep.trajectory.steps[0].info["raw_text"].startswith("<actions>")
        assert ep.trajectory.metadata["golden_checkpoint"] == "golden-1"
        with pytest.raises(g.GymError, match="done"):
            env.step("<done/>")  # episode over — reset() forks the next one


def test_verifier_reads_observed_state_not_inputs(mock_shinkend):
    # An episode that types the WRONG text scores 0.0 — the verifier is not a tautology.
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        env.step('<actions><type_text text="WRONG"/></actions>')
        _, reward, done, _ = env.step("<done/>")
        assert done and reward == 0.0


def test_max_steps_budget_ends_episode(mock_shinkend):
    env = g.ShinkenGymEnv(g.GymTask("budget"), _ForkProvider(mock_shinkend), max_steps=2).make()
    env.reset()
    _, reward, done, _ = env.step({"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 1}})
    assert not done
    _, reward, done, info = env.step(
        {"verb": "click", "target": {"kind": "point_px", "x": 2, "y": 2}}
    )
    assert done and reward is None and info["terminal"] == "max_steps"
    assert env.episodes[-1].trajectory.exit_reason == "max_steps"
    env.dispose()


def test_evaluate_reward_shapes(mock_shinkend):
    prov = _ForkProvider(mock_shinkend)
    receipt_task = g.GymTask(
        "receipt", verify=lambda s: ev.VerifierReceipt.from_checks([ev.check("ok", True)])
    )
    with g.make(receipt_task, prov) as env:
        env.reset()
        assert env.evaluate() == 1.0  # VerifierReceipt.passed -> 1.0
        assert env.last_receipt.passed
    # float passthrough + typed rejection of nonsense
    assert g._reward_from(0.25) == 0.25
    assert g._reward_from(False) == 0.0
    assert g._reward_from({"passed": True}) == 1.0
    with pytest.raises(g.GymError, match="verifier returned"):
        g._reward_from("not a reward")


def test_evaluate_without_verifier_is_typed(mock_shinkend):
    with g.make(g.GymTask("bare"), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        with pytest.raises(g.GymError, match="no verifier"):
            env.evaluate()


def test_scorer_fault_is_typed_never_a_fake_zero(mock_shinkend):
    def broken_verify(_sess):
        raise RuntimeError("scorer crashed")

    with g.make(g.GymTask("broken", verify=broken_verify), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        _, reward, done, info = env.step("<done/>")
        assert done and reward is None and "scorer_error" in info
        assert env.episodes[-1].trajectory.exit_reason == "scorer_error"


def test_replica_death_finalizes_episode_as_sandbox_died(mock_shinkend):
    class _DeadSession:
        def step(self, *_a, **_k):
            raise ConnectionResetError("container OOM-killed")

    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        env.reset()
        env._sess.close()
        env._sess = _DeadSession()  # the transport died under the episode
        with pytest.raises(ConnectionResetError):
            env.step({"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 1}})
        ep = env.episodes[-1]
        assert ep.trajectory.exit_reason == "sandbox_died"
        assert "sandbox_died" in ep.trajectory.metadata["error"]
        env._sess = None  # the dead stub has no close()


def test_structured_observation_knob(mock_shinkend):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend), observation="structured") as env:
        obs, _info = env.reset()
        assert "tree_text" in obs and obs["elements"]  # the guest a11y tree, not pixels
        obs, _, _, info = env.step('<actions><click x="10" y="10"/></actions>')
        assert "tree_text" in obs and [r["verb"] for r in info["results"]] == ["click"]
    with pytest.raises(g.GymError, match="observation"):
        g.ShinkenGymEnv(g.GymTask("x"), object(), observation="hologram")


# --------------------------------------------------------------------------------- pool


def test_pool_one_golden_parallel_reset_and_shared_loop(mock_shinkend):
    prov = _ForkProvider(mock_shinkend)
    setups: list = []
    with g.ShinkenGymPool(_typed_hi_task(setups), prov, 3) as pool:
        results = pool.reset()
        # ONE golden build (one setup, one checkpoint), N parallel forks
        assert setups == [1] and prov.calls["checkpoint"] == 1
        assert prov.calls["resume"] == 3 and len(results) == 3
        assert all(info["reset_ms"] > 0 for _obs, info in results)
        # all sessions multiplex one SharedLoop + one FrameCache
        loops = {id(env.connect_kwargs["loop"]) for env in pool.envs}
        caches = {id(env.connect_kwargs["frame_cache"]) for env in pool.envs}
        assert len(loops) == 1 and len(caches) == 1
        outs = pool.step(['<actions><type_text text="hi"/></actions>'] * 3)
        assert all(not done for _o, _r, done, _i in outs)
        assert pool.evaluate() == [1.0, 1.0, 1.0]
    # close(): every replica destroyed (3 + 1 base) and the ONE snapshot reclaimed
    assert prov.calls["destroy"] == 4
    assert prov.calls["delete_snapshot"] == 1


def test_pool_step_arity_is_typed(mock_shinkend):
    with g.ShinkenGymPool(_typed_hi_task(), _ForkProvider(mock_shinkend), 2) as pool:
        pool.reset()
        with pytest.raises(g.GymError, match="expected 2 actions"):
            pool.step(["<done/>"])


# ----------------------------------------------------------------------------- exporter


def _run_episode(env: g.ShinkenGymEnv) -> None:
    env.reset()
    env.step('<actions><type_text text="hi"/></actions>')
    env.step("<done/>")


def test_exporter_dict_of_lists_shape_and_columns(mock_shinkend):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        _run_episode(env)
        _run_episode(env)
        records = g.episodes_to_records(env.episodes)
    assert set(records) == set(g.EXPORT_COLUMNS)
    n = len(records["episode"])
    assert n == 4  # 2 episodes x 2 steps, one row per step
    assert all(len(col) == n for col in records.values())  # columnar: aligned lists
    assert records["image"][0][:8] == b"\x89PNG\r\n\x1a\n"  # images as PNG bytes
    assert json.loads(records["actions_json"][0]) == [{"verb": "type_text", "text": "hi"}]
    assert records["raw_text"][0].startswith("<actions>")
    assert records["done"] == [False, True, False, True]
    assert records["reward"] == [1.0, 1.0, 1.0, 1.0]  # episode reward broadcast to rows
    assert set(records["exit_reason"]) == {"task_complete"}  # the typed taxonomy column
    assert records["episode"] == [0, 0, 1, 1] and records["step"] == [0, 1, 0, 1]
    assert all(ms >= 0 for ms in records["reset_ms"])


def test_to_hf_dataset_plain_dict_fallback(mock_shinkend, monkeypatch):
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        _run_episode(env)
        episodes = env.episodes
    monkeypatch.setitem(sys.modules, "datasets", None)  # import datasets -> ImportError
    out = g.to_hf_dataset(episodes)
    assert isinstance(out, dict) and set(out) == set(g.EXPORT_COLUMNS)


def test_to_hf_dataset_real_when_installed(mock_shinkend):
    datasets = pytest.importorskip("datasets")
    with g.make(_typed_hi_task(), _ForkProvider(mock_shinkend)) as env:
        _run_episode(env)
        ds = g.to_hf_dataset(env.episodes)
    assert isinstance(ds, datasets.Dataset) and len(ds) == 2


# --------------------------------------------------------------------------- dataloader


def test_dataloader_batches_step_routing_and_autoreset_fork(mock_shinkend):
    prov = _ForkProvider(mock_shinkend)
    pool = g.ShinkenGymPool(_typed_hi_task(), prov, 2)
    loader = g.MultiTurnDataloader(pool, total_episodes=4)
    batches = 0
    for batch in loader:
        batches += 1
        assert set(batch) == {"env_id", "observation", "image", "instruction", "step", "task"}
        assert all(img[:8] == b"\x89PNG\r\n\x1a\n" for img in batch["image"])
        responses = [
            "<done/>" if step > 0 else '<actions><type_text text="hi"/></actions>'
            for step in batch["step"]
        ]
        rows = loader.async_step({"env_id": batch["env_id"], "responses": responses})
        for row, step in zip(rows, batch["step"], strict=False):
            assert row["done"] == (step > 0)
            if row["done"]:
                assert row["reward"] == 1.0
    assert loader._completed == 4 and len(loader.episodes) == 4
    assert all(ep.reward == 1.0 for ep in loader.episodes)
    # 2 initial forks + exactly 2 auto-reset forks: the budget never over-forks
    assert prov.calls["resume"] == 4
    sample = loader.sample_episodes(2)
    assert len(sample) == 2 and all(isinstance(ep, g.Episode) for ep in sample)
    loader.close()
    assert prov.calls["delete_snapshot"] == 1


def test_dataloader_preserves_observation_generation_across_async_step():
    """The async facade must record exactly the observation yielded for each response."""
    pool = g.ShinkenGymPool(
        g.GymTask("transition", instruction="advance once"), _TransitionProvider(), 1
    )
    loader = g.MultiTurnDataloader(pool, total_episodes=1)
    try:
        initial_batch = next(loader)
        initial = initial_batch["observation"][0]
        assert initial["bytes"] == b"state-0"
        rows = loader.async_step(
            {
                "env_id": initial_batch["env_id"],
                "responses": [{"verb": "type_text", "text": "advance"}],
            }
        )
        assert not rows[0]["done"]

        next_batch = next(loader)
        post = next_batch["observation"][0]
        assert post["bytes"] == b"state-1"
        rows = loader.async_step({"env_id": next_batch["env_id"], "responses": ["<done/>"]})
        assert rows[0]["done"]

        first, terminal = loader.episodes[0].trajectory.steps
        assert first.observation == initial
        assert first.next_observation == post
        assert terminal.observation == post
    finally:
        loader.close()


def test_dataloader_accepts_their_worker_id_key(mock_shinkend):
    pool = g.ShinkenGymPool(_typed_hi_task(), _ForkProvider(mock_shinkend), 1)
    loader = g.MultiTurnDataloader(pool, total_episodes=1)
    batch = next(loader)
    rows = loader.async_step({"worker_id": batch["env_id"], "responses": ["<done/>"]})
    assert rows[0]["done"]
    loader.close()


def test_dataloader_unparseable_response_aborts_as_agent_error(mock_shinkend):
    pool = g.ShinkenGymPool(_typed_hi_task(), _ForkProvider(mock_shinkend), 1)
    loader = g.MultiTurnDataloader(pool, total_episodes=2)
    batch = next(loader)
    rows = loader.async_step({"env_id": batch["env_id"], "responses": ["just prose, no actions"]})
    assert rows[0]["done"] and "error" in rows[0]["info"]
    aborted = loader.episodes[-1]
    assert aborted.trajectory.exit_reason == "agent_error"  # typed, recorded, not a crash
    batch = next(loader)  # collection continues on a fresh fork
    assert batch["env_id"] == [0]
    loader.close()


def test_dataloader_batch_size_cannot_exceed_envs(mock_shinkend):
    pool = g.ShinkenGymPool(_typed_hi_task(), _ForkProvider(mock_shinkend), 1)
    with pytest.raises(g.GymError, match="one step per batch"):
        g.MultiTurnDataloader(pool, batch_size=2)
    pool.close()


def test_dataloader_misaligned_batch_return_is_typed(mock_shinkend):
    pool = g.ShinkenGymPool(_typed_hi_task(), _ForkProvider(mock_shinkend), 1)
    loader = g.MultiTurnDataloader(pool, total_episodes=1)
    next(loader)
    with pytest.raises(g.GymError, match="aligned"):
        loader.async_step({"responses": ["<done/>"]})
    loader.close()


# ---------------------------------------------------------------------------- live (Docker)

requires_docker = pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCKER_TESTS") != "1",
    reason="live Docker test: set SHINKEN_DOCKER_TESTS=1 (needs the shinken/sandbox-linux image)",
)


@requires_docker
def test_live_golden_resets_inherit_filesystem_state(tmp_path):
    """Golden checkpoint once -> three cold disk restores, each inheriting the marker.

    The former live warm graft is intentionally not exercised: it is disabled until
    pool-hit/pool-miss equivalence can be proved.
    """
    from shinken.providers.base import ProviderError
    from shinken.providers.docker import DockerLocalProvider

    marker = tmp_path / "marker.txt"
    marker.write_text("golden-state")
    os.chmod(marker, 0o644)

    def setup(sess):  # runs ONCE, into the golden checkpoint
        sess.put_file(str(marker), "/tmp/gym_marker.txt")

    def verify(sess):  # runs per replica: golden state must survive the fork
        out = tmp_path / "readback.txt"
        sess.get_file("/tmp/gym_marker.txt", str(out))
        return 1.0 if out.read_text() == "golden-state" else 0.0

    task = g.GymTask(
        "live-gym", instruction="inherit the golden marker", setup=setup, verify=verify
    )
    provider = DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    try:
        with g.make(task, provider) as env:
            reset_ms: list[float] = []
            for _ in range(3):
                try:
                    obs, info = env.reset()
                except ProviderError:
                    obs, info = env.reset()  # one infra retry (busy shared daemon)
                reset_ms.append(info["reset_ms"])
                assert obs["png"][:8] == b"\x89PNG\r\n\x1a\n"
                assert env.evaluate() == 1.0  # state inheritance, per replica
            assert all(ms > 0 for ms in reset_ms)
            # one real step over the typed ACI on the final replica
            _, _, done, info = env.step('<actions><click x="100" y="100"/></actions>')
            assert not done and info["results"][0]["ok"]
            _, reward, done, _ = env.step("<done/>")
            assert done and reward == 1.0
            ep = env.episodes[-1]
            assert ep.trajectory.exit_reason == "task_complete" and len(ep.trajectory.steps) == 2
    finally:
        provider.destroy_all()
