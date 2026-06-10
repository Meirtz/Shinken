"""CUA-Gym interop adapter (shinken.integrations.cua_gym).

Fixture-tested against the surveyed protocol shapes (xlang-ai/CUA-Gym @ 1e50b797 — the
exported bundle layout, the env method surface, the `REWARD: X.X` contract) with a mock
runtime + fake fork provider; one short Docker-gated live test (SHINKEN_DOCKER_TESTS=1).
"""

from __future__ import annotations

import collections
import json
import os
import textwrap

import pytest

import shinken
from shinken.artifacts import ArtifactRef, sha256_file
from shinken.integrations import cua_gym as cg

# ---------------------------------------------------------------------------- fixtures


def _write_bundle(
    root,
    task_id: str,
    *,
    instruction: str = "do the thing",
    reward: str | None = "print('REWARD: 1.0')\n",
    with_setup: bool = True,
):
    """One exported CUA-Gym bundle in the surveyed output/final/<task_id>/ shape."""
    d = root / task_id
    d.mkdir(parents=True)
    config: dict = {
        "instruction": instruction,
        "id": task_id,
        "app_type": "demo",
        "config": [],
        "evaluator": {"type": "python", "url": f"oss://bucket/{task_id}/reward.py"},
    }
    if with_setup:
        (d / "initial_setup.py").write_text("open('/tmp/marker.txt','w').write('golden')\n")
        (d / "golden_patch.py").write_text("# initial -> golden\n")
        config["config"] = [
            {
                "type": "download",
                "parameters": {
                    "files": [
                        {
                            "url": f"oss://bucket/{task_id}/initial_setup.py",
                            "path": "/home/user/initial_setup.py",
                        }
                    ]
                },
            },
            {
                "type": "execute",
                "parameters": {"command": "python3 /home/user/initial_setup.py"},
            },
        ]
    (d / "config.json").write_text(json.dumps(config))
    if reward is not None:
        (d / "reward.py").write_text(reward)
    return d


@pytest.fixture
def bundle_root(tmp_path):
    _write_bundle(tmp_path, "task_a")
    _write_bundle(tmp_path, "task_b", instruction="second task")
    return tmp_path


class _FakeExec:
    """Recording guest-exec channel: (argv, kwargs) log + per-substring canned replies."""

    def __init__(self, responses: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.responses = responses or {}

    def __call__(self, argv, *, timeout=60.0, workdir=None, detach=False):
        self.calls.append((list(argv), {"timeout": timeout, "workdir": workdir, "detach": detach}))
        joined = " ".join(argv)
        for needle, reply in self.responses.items():
            if needle in joined:
                return reply
        return 0, "", ""


class _FakeGuestFS:
    """In-memory guest filesystem with the guest-transport put/get contract (absolute
    guest paths, like ``DockerGuestTransport``). Shared across replicas, simulating the
    fork's inheritance of golden-state files."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def put(self, local_path, guest_path, scope="session"):
        from pathlib import Path

        data = Path(local_path).read_bytes()
        self.files[guest_path] = data
        return ArtifactRef(guest_path, sha256_file(local_path), len(data), scope, "put")

    def get(self, guest_path, local_path, *, expect_sha256=None, scope="session"):
        from pathlib import Path

        if guest_path not in self.files:
            raise FileNotFoundError(guest_path)
        Path(local_path).write_bytes(self.files[guest_path])
        return ArtifactRef(
            guest_path, sha256_file(local_path), len(self.files[guest_path]), scope, "get"
        )


class _FakeForkProvider:
    """Shinken-provider-shaped fake: counts lifecycle calls; connect() opens a real SDK
    session against the mock shinkend and attaches a guest transport, exactly as
    ``DockerLocalProvider.connect`` does (#154)."""

    def __init__(self, addr: str):
        self.addr = addr
        self.calls: collections.Counter = collections.Counter()
        self.destroyed: list[str] = []
        self.guest_fs = _FakeGuestFS()

    def create(self, spec=None):
        self.calls["create"] += 1
        return "base"

    def connect(self, handle):
        self.calls["connect"] += 1
        env = shinken.connect(self.addr)
        env._set_guest_transport(self.guest_fs)
        return env

    def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None):
        self.calls["checkpoint"] += 1
        return "ckpt-golden"

    def resume(self, ckpt):
        assert ckpt == "ckpt-golden"  # every replica forks the ONE golden checkpoint
        self.calls["resume"] += 1
        return f"replica-{self.calls['resume']}"

    def destroy(self, handle):
        self.calls["destroy"] += 1
        self.destroyed.append(str(handle))

    def delete_snapshot(self, ckpt):
        self.calls["delete_snapshot"] += 1


def _env(task, provider, fake_exec):
    return cg.ShinkenCuaGymEnv(task, provider, exec_factory=lambda _p, _h: fake_exec)


# ---------------------------------------------------------------------------- TaskSource


def test_task_source_loads_exported_bundles(bundle_root):
    src = cg.CuaGymTaskSource(bundle_root)
    assert len(src) == 2 and not src.skipped
    task = src.get("task_a")
    assert task.instruction == "do the thing"
    assert task.app_type == "demo"
    assert task.reward_script.is_file()
    assert task.setup_script is not None and task.golden_patch is not None
    assert [s["type"] for s in task.setup_steps] == ["download", "execute"]
    assert {t.task_id for t in src} == {"task_a", "task_b"}


def test_task_source_root_from_env_var(bundle_root, monkeypatch):
    monkeypatch.setenv("CUA_GYM_TASKS", str(bundle_root))
    assert len(cg.CuaGymTaskSource()) == 2


def test_task_source_requires_a_root(monkeypatch):
    monkeypatch.delenv("CUA_GYM_TASKS", raising=False)
    with pytest.raises(cg.CuaGymError, match="CUA_GYM_TASKS"):
        cg.CuaGymTaskSource()


def test_task_source_skips_malformed_bundles_visibly(tmp_path):
    _write_bundle(tmp_path, "good")
    _write_bundle(tmp_path, "no_reward", reward=None)
    (tmp_path / "no_config").mkdir()
    src = cg.CuaGymTaskSource(tmp_path)
    assert len(src) == 1
    reasons = {p.name: why for p, why in src.skipped}
    assert reasons == {"no_reward": "no reward.py", "no_config": "no config.json"}
    with pytest.raises(KeyError, match="no_reward"):
        src.get("no_reward")


def test_parse_reward_contract():
    # Their contract: last `REWARD: X.X` line of reward.py stdout wins.
    assert cg.parse_reward("REWARD: 0.5") == 0.5
    assert cg.parse_reward("noise\nREWARD: 0.0\nmore\nREWARD: 1.0\n") == 1.0
    assert cg.parse_reward("LLM_JUDGE: score=0.4\nREWARD: 0.4") == 0.4
    assert cg.parse_reward("no marker here") is None
    assert cg.parse_reward("") is None
    assert cg.parse_reward("REWARD: not-a-number") is None


# ----------------------------------------------------------------- env surface + fork reset


def test_env_mirrors_cua_gym_env_surface():
    # Pin the adapter to the surveyed utils/env.py operation surface (duck-type contract).
    for method in cg.CUA_GYM_ENV_SURFACE:
        assert callable(getattr(cg.ShinkenCuaGymEnv, method)), method


def test_reset_builds_golden_once_then_forks(bundle_root, mock_shinkend):
    prov = _FakeForkProvider(mock_shinkend)
    fake = _FakeExec()
    with _env(cg.CuaGymTaskSource(bundle_root).get("task_a"), prov, fake) as env:
        obs = env.reset()
        # golden build: one create, one checkpoint, base destroyed; replica resumed from it
        assert prov.calls["create"] == 1 and prov.calls["checkpoint"] == 1
        assert prov.calls["resume"] == 1 and "base" in prov.destroyed
        # the bundle's execute step ran in-guest during golden setup
        setup_cmds = [argv for argv, _ in fake.calls]
        assert ["sh", "-c", "python3 /home/user/initial_setup.py"] in setup_cmds
        assert obs["instruction"] == "do the thing" and isinstance(obs["screenshot"], bytes)

        n_setup_calls = len(fake.calls)
        env.reset()  # second reset: NO new golden build, just another fork
        assert prov.calls["create"] == 1 and prov.calls["checkpoint"] == 1
        assert prov.calls["resume"] == 2
        assert "replica-1" in prov.destroyed  # the stale replica was torn down
        assert len(fake.calls) == n_setup_calls  # setup did not rerun
    assert prov.calls["delete_snapshot"] == 1  # dispose() reclaimed the golden snapshot


def test_setup_download_resolves_from_bundle_never_network(bundle_root, mock_shinkend, tmp_path):
    task = cg.CuaGymTaskSource(bundle_root).get("task_a")
    # Point the download step at a file that is NOT in the bundle: must raise, not fetch.
    broken = dict(task.config)
    broken["config"] = [
        {
            "type": "download",
            "parameters": {"files": [{"url": "oss://bucket/x/missing.py", "path": "/tmp/x.py"}]},
        }
    ]
    bad = cg.CuaGymTask(task.task_id, task.instruction, task.app_type, task.path, broken)
    env = _env(bad, _FakeForkProvider(mock_shinkend), _FakeExec())
    with pytest.raises(cg.CuaGymError, match="missing.py"):
        env.reset()


def test_setup_execute_failure_is_typed(bundle_root, mock_shinkend):
    task = cg.CuaGymTaskSource(bundle_root).get("task_a")
    fake = _FakeExec(responses={"initial_setup.py": (1, "", "boom")})
    env = _env(task, _FakeForkProvider(mock_shinkend), fake)
    with pytest.raises(cg.CuaGymError, match="setup command failed"):
        env.reset()


def test_execute_run_bash_and_screen_size_shapes(bundle_root, mock_shinkend):
    fake = _FakeExec(responses={"echo hi": (0, "hi\n", "")})
    task = cg.CuaGymTaskSource(bundle_root).get("task_a")
    with _env(task, _FakeForkProvider(mock_shinkend), fake) as env:
        env.reset()
        # their Flask response shape: {"output", "error", "returncode"}
        assert env.execute("echo hi") == {"output": "hi\n", "error": "", "returncode": 0}
        env.run_bash("ls /tmp", working_dir="/tmp")
        argv, kw = fake.calls[-1]
        assert argv == ["sh", "-c", "ls /tmp"] and kw["workdir"] == "/tmp"
        assert env.get_screen_size() == {"width": 1280, "height": 800}  # their key names
        assert env.get_accessibility_tree() is None  # parity: shipped, unused by their pipeline
        env.launch("xterm -e top")
        argv, kw = fake.calls[-1]
        assert argv == ["xterm", "-e", "top"] and kw["detach"] is True


def test_evaluate_runs_reward_and_parses_marker(bundle_root, mock_shinkend):
    fake = _FakeExec(responses={".py": (0, "checking...\nREWARD: 1.0\n", "")})
    task = cg.CuaGymTaskSource(bundle_root).get("task_a")
    with _env(task, _FakeForkProvider(mock_shinkend), fake) as env:
        env.reset()
        assert env.evaluate() == 1.0
        # reward.py was uploaded then executed with the guest python
        argv, _ = fake.calls[-1]
        assert argv[0] == "python3" and argv[1].endswith(".py")


def test_evaluate_without_reward_marker_is_typed_not_zero(bundle_root, mock_shinkend):
    fake = _FakeExec(responses={"shinken_cua_": (1, "traceback...", "ImportError")})
    task = cg.CuaGymTaskSource(bundle_root).get("task_a")
    with _env(task, _FakeForkProvider(mock_shinkend), fake) as env:
        env.reset()
        with pytest.raises(cg.CuaGymError, match="no 'REWARD: X.X' line"):
            env.evaluate()


def test_operations_before_reset_are_typed(bundle_root, mock_shinkend):
    src = cg.CuaGymTaskSource(bundle_root)
    env = _env(src.get("task_a"), _FakeForkProvider(mock_shinkend), _FakeExec())
    with pytest.raises(cg.CuaGymError, match="reset"):
        env.execute("ls")


def test_default_exec_factory_requires_a_docker_shape():
    class _NoExecProvider:
        pass

    with pytest.raises(cg.CuaGymError, match="exec_factory"):
        cg.default_exec_factory(_NoExecProvider(), object())


# ---------------------------------------------------------------------------- live (Docker)

requires_docker = pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCKER_TESTS") != "1",
    reason="live Docker test: set SHINKEN_DOCKER_TESTS=1 (needs the shinken/sandbox-linux image)",
)


@requires_docker
def test_live_fork_reset_and_reward(tmp_path):
    """Golden checkpoint → fork-reset ×2 on the real Docker disk tier: setup runs once,
    both replicas inherit the golden file, reward.py scores 1.0 in-guest."""
    from shinken.providers.base import ProviderError
    from shinken.providers.docker import DockerLocalProvider

    d = tmp_path / "live_task"
    d.mkdir()
    (d / "initial_setup.py").write_text("open('/tmp/marker.txt','w').write('golden')\n")
    (d / "reward.py").write_text(
        textwrap.dedent(
            """\
            try:
                ok = open('/tmp/marker.txt').read() == 'golden'
            except OSError:
                ok = False
            print('REWARD: 1.0' if ok else 'REWARD: 0.0')
            """
        )
    )
    (d / "config.json").write_text(
        json.dumps(
            {
                "instruction": "live fork smoke",
                "id": "live_task",
                "app_type": "os",
                "config": [
                    {
                        "type": "download",
                        "parameters": {
                            "files": [{"url": "oss://x/initial_setup.py", "path": "/tmp/setup.py"}]
                        },
                    },
                    {"type": "execute", "parameters": {"command": "python3 /tmp/setup.py"}},
                ],
                "evaluator": {"type": "python", "url": "oss://x/reward.py"},
            }
        )
    )
    task = cg.CuaGymTaskSource(tmp_path).get("live_task")
    # generous startup timeout: the live smoke shares the daemon with whatever else runs
    provider = DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    with cg.ShinkenCuaGymEnv(task, provider) as env:
        try:
            obs = env.reset()
        except ProviderError:
            # infra failure (a starved desktop boot on a busy shared daemon) is the
            # retry-eligible class — one retry, mirroring the SDK's own taxonomy
            obs = env.reset()
        assert obs["screenshot"] and obs["screenshot"][:4] == b"\x89PNG"
        assert env.evaluate() == 1.0
        golden = env.golden_checkpoint
        env.reset()  # second replica: same checkpoint, no setup rerun
        assert env.golden_checkpoint == golden
        assert env.download("/tmp/marker.txt") == b"golden"  # inherited golden state
        assert env.evaluate() == 1.0
