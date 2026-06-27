"""API-v2 regression suite — every repro from the merciless usability audit, as tests.

Covers: the use-after-close deadlock (typed SessionClosed), handle exposure +
destroy(), provider.session(), owner-aware cleanup_orphans/destroy_all/list/gc,
snapshot-image labels + fork-image GC accounting, first-class Checkpoint +
SandboxFleet (concurrent map), Sandbox.spawn(), token-redacting handle repr, typed
ConnectError/UnknownVerb/ProviderRequired, screenshot(max_long_edge=), act_model,
eager verifier-receipt validation, the unified Task dataclass, the ps/gc CLI, and
__all__/version hygiene. Live Docker proofs are gated on SHINKEN_DOCKER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

import shinken
from shinken import cli
from shinken import eval as ev
from shinken import gym as g
from shinken import providers as providers_registry
from shinken.adapters.anthropic import AnthropicComputerUseAdapter
from shinken.providers.base import (
    GcReport,
    ProviderCapabilities,
    SandboxHandle,
    SandboxProvider,
    UnsupportedProviderOperation,
)
from shinken.providers.docker import DockerLocalProvider, _pid_alive

# ------------------------------------------------------------------------------ helpers


def _watchdog(fn, timeout=10.0):
    """Run ``fn`` on a side thread and FAIL (not hang) if it deadlocks — the audit's
    use-after-close repro blocked forever, so its regression test must be hang-proof."""
    result: dict = {}

    def run():
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised on the test thread
            result["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        pytest.fail(f"{fn} deadlocked (> {timeout}s) — use-after-close regression")
    if "exc" in result:
        raise result["exc"]
    return result.get("value")


class _FakeForkProvider(SandboxProvider):
    """Lifecycle provider over the in-process mock shinkend: create/fork/restore mint
    handles that all dial the mock, so provider-managed Sandbox surfaces (handle,
    destroy, spawn, Checkpoint.spawn/spawn_many) are exercised end to end."""

    capabilities = ProviderCapabilities(
        name="fake-fork",
        supports_lifecycle=True,
        supports_gui=False,
        supports_snapshot=True,
        supports_fork=True,
        supports_checkpoint=True,
        supports_resume=True,
        snapshot_kind="disk",
        tier="test",
    )

    def __init__(self, addr: str) -> None:
        self.addr = addr
        self.created = 0
        self.restored = 0
        self.destroyed: list[str] = []
        self.deleted_snapshots: list[str] = []
        self._lock = threading.Lock()  # spawn_many restores concurrently

    def _handle(self, sandbox_id: str) -> SandboxHandle:
        return SandboxHandle(provider="fake-fork", sandbox_id=sandbox_id, addr=self.addr)

    def create(self, spec=None) -> SandboxHandle:
        self.created += 1
        return self._handle(f"base-{self.created}")

    def destroy(self, handle: SandboxHandle) -> None:
        self.destroyed.append(handle.sandbox_id)

    def checkpoint(self, handle, *, name=None, event_seq=None, agent_state_ref=None) -> str:
        return "ckpt-xyz"

    def snapshot_spec(self, snapshot_or_checkpoint_id):
        return {"spec_for": str(snapshot_or_checkpoint_id)}

    def fork(self, handle: SandboxHandle) -> SandboxHandle:
        return self._handle(f"fork-of-{handle.sandbox_id}")

    def restore(self, snapshot_id: str) -> SandboxHandle:
        with self._lock:
            self.restored += 1
            n = self.restored
        return self._handle(f"restore-{n}")

    def resume(self, handle_or_checkpoint):
        return self.restore(str(handle_or_checkpoint))

    def delete_snapshot(self, snapshot_id: str) -> None:
        self.deleted_snapshots.append(str(snapshot_id))


def _dead_pid() -> int:
    """A pid that belonged to a real (now exited) process."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _docker_router(monkeypatch, routes):
    """Monkeypatch shinken.providers.docker._run with a (matcher → stdout) router;
    returns the recorded command list. Unmatched commands answer empty stdout."""
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        for matcher, out in routes:
            if matcher(cmd):
                return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    return calls


# ------------------------------------------------ 1. use-after-close: typed, no deadlock


def test_method_after_close_raises_session_closed(mock_shinkend, tmp_path):
    env = shinken.connect(mock_shinkend)
    env.close()
    env.close()  # double-close stays idempotent
    with pytest.raises(shinken.SessionClosed):
        _watchdog(env.ping)
    with pytest.raises(shinken.SessionClosed):
        _watchdog(env.screenshot)
    with pytest.raises(shinken.SessionClosed):
        _watchdog(lambda: env.act("click", {"kind": "point_px", "x": 1, "y": 2}))
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(shinken.SessionClosed):
        env.put_file(str(f), "/tmp/x.txt")
    # SessionClosed stays a ShinkenError/RuntimeError for legacy handlers
    assert issubclass(shinken.SessionClosed, shinken.ShinkenError)
    assert issubclass(shinken.SessionClosed, RuntimeError)


def test_close_on_shared_loop_is_typed_and_leaves_siblings_alive(mock_shinkend):
    with shinken.SharedLoop() as loop:
        env1 = shinken.connect(mock_shinkend, loop=loop)
        env2 = shinken.connect(mock_shinkend, loop=loop)
        env1.close()
        with pytest.raises(shinken.SessionClosed):
            _watchdog(env1.ping)
        assert _watchdog(env2.ping) >= 0  # the sibling keeps multiplexing
        env2.close()


def test_method_after_shared_loop_close_raises_instead_of_hanging(mock_shinkend):
    # The audit's nastiest variant: the SANDBOX was never closed, but its loop was —
    # scheduling onto the stopped loop used to block forever.
    loop = shinken.SharedLoop()
    env = shinken.connect(mock_shinkend, loop=loop)
    loop.close()
    with pytest.raises(shinken.SessionClosed):
        _watchdog(env.ping)
    env.close()  # still idempotent/graceful after the loop is gone


def test_async_method_after_close_raises_session_closed(mock_shinkend):
    async def run():
        sb = await shinken.aconnect(mock_shinkend)
        await sb.close()
        await sb.close()  # idempotent
        with pytest.raises(shinken.SessionClosed):
            await sb.ping()
        with pytest.raises(shinken.SessionClosed):
            await sb.act("click", {"kind": "point_px", "x": 1, "y": 2})
        with pytest.raises(shinken.SessionClosed):
            await sb.step([{"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}}])

    asyncio.run(run())


# --------------------------------------------------------- 2. handle exposure + destroy


def test_handle_property_and_destroy(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    handle = provider.create()
    env = provider.connect(handle)
    assert env.handle is handle
    env.destroy()
    assert provider.destroyed == [handle.sandbox_id]
    with pytest.raises(shinken.SessionClosed):  # destroy closed the session too
        _watchdog(env.ping)


def test_destroy_without_provider_is_typed_and_safe(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.handle is None
        with pytest.raises(shinken.ProviderRequired):
            env.destroy()
        assert env.ping() >= 0  # the failed destroy did not half-close the session


def test_async_handle_and_destroy(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)

    async def run():
        handle = provider.create()
        sb = await shinken.aconnect(handle.addr)
        sb._set_provider_context(provider, handle)
        assert sb.handle is handle
        await sb.destroy()
        assert provider.destroyed == [handle.sandbox_id]
        bare = await shinken.aconnect(handle.addr)
        try:
            with pytest.raises(shinken.ProviderRequired):
                await bare.destroy()
        finally:
            await bare.close()

    asyncio.run(run())


# ------------------------------------------------------------- 3. provider.session() CM


def test_provider_session_context_manager_destroys_on_exit(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    with provider.session() as env:
        assert env.ping() >= 0
        sandbox_id = env.handle.sandbox_id
    assert sandbox_id in provider.destroyed


def test_provider_session_destroys_even_when_body_raises(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    with pytest.raises(RuntimeError, match="boom"):
        with provider.session() as env:
            sandbox_id = env.handle.sandbox_id
            raise RuntimeError("boom")
    assert sandbox_id in provider.destroyed


def test_provider_session_destroys_when_connect_fails():
    provider = _FakeForkProvider("127.0.0.1:1")  # nothing listens there

    with pytest.raises(shinken.ConnectError):
        with provider.session():
            pytest.fail("body must not run when connect fails")
    assert provider.destroyed == ["base-1"]  # the created substrate was reclaimed


def test_provider_session_surfaces_close_failure_and_still_destroys():
    provider = _FakeForkProvider("unused")

    class FailingClose:
        def close(self):
            raise RuntimeError("session close failed")

    provider.connect = lambda _handle, **_kwargs: FailingClose()
    with pytest.raises(RuntimeError, match="session close failed"):
        with provider.session():
            pass
    assert provider.destroyed == ["base-1"]


def test_provider_session_surfaces_destroy_failure_after_normal_body():
    provider = _FakeForkProvider("unused")
    attempted: list[str] = []

    class Closed:
        def close(self):
            return None

    def fail_destroy(handle):
        attempted.append(handle.sandbox_id)
        raise RuntimeError("session destroy failed")

    provider.connect = lambda _handle, **_kwargs: Closed()
    provider.destroy = fail_destroy
    with pytest.raises(RuntimeError, match="session destroy failed"):
        with provider.session():
            pass
    assert attempted == ["base-1"]


def test_provider_session_preserves_body_error_when_all_teardown_fails(caplog):
    provider = _FakeForkProvider("unused")
    attempted: list[str] = []

    class FailingClose:
        def close(self):
            raise RuntimeError("secondary close failure")

    def fail_destroy(handle):
        attempted.append(handle.sandbox_id)
        raise RuntimeError("secondary destroy failure")

    provider.connect = lambda _handle, **_kwargs: FailingClose()
    provider.destroy = fail_destroy
    with caplog.at_level("ERROR"):
        with pytest.raises(ValueError, match="primary body failure"):
            with provider.session():
                raise ValueError("primary body failure")
    assert attempted == ["base-1"]
    assert "session close failed while handling body error" in caplog.text
    assert "session destroy failed while handling body error" in caplog.text


# ----------------------------------------- 4. owner-aware cleanup_orphans / list / repr


def test_pid_alive_probe():
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(_dead_pid()) is False


def test_cleanup_orphans_reclaims_dead_owners_never_live(monkeypatch):
    dead, live, now = _dead_pid(), os.getpid(), time.time()
    inspect_out = "\n".join(
        [
            f"c-dead\t{dead}\t{now}",
            f"c-live\t{live}\t{now}",
            f"c-old\t{live}\t{now - 7200}",
            "c-unlabeled\t\t",  # pre-label SDK: unknown owner — reclaim only by age
        ]
    )
    calls = _docker_router(
        monkeypatch,
        [
            (lambda c: c[:2] == ["docker", "ps"], "c-dead\nc-live\nc-old\nc-unlabeled\n"),
            (lambda c: c[:2] == ["docker", "inspect"] and "-f" in c, inspect_out),
        ],
    )
    provider = DockerLocalProvider()
    assert provider.cleanup_orphans() == 1  # ONLY the dead-owner container
    rm = next(c for c in calls if c[:3] == ["docker", "rm", "-f"])
    assert rm[3:] == ["c-dead"], "live-owner and unlabeled containers must be untouched"

    calls.clear()
    # max_age_s is the explicit operator opt-in: age reclaims regardless of owner
    assert provider.cleanup_orphans(max_age_s=3600) == 2
    rm = next(c for c in calls if c[:3] == ["docker", "rm", "-f"])
    assert sorted(rm[3:]) == ["c-dead", "c-old"]


def test_create_stamps_owner_and_created_labels(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19011)
    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", lambda _self, _handle: None)
    DockerLocalProvider().create()
    cmd = commands[0]
    assert f"shinken.owner_pid={os.getpid()}" in cmd
    assert any(a.startswith("shinken.created_at=") for a in cmd)


def test_list_rebuilds_handles_from_labels(monkeypatch):
    inspect_json = json.dumps(
        [
            {
                "Id": "abc123def456",
                "Name": "/shinken-local-rebuilt1",
                "Config": {
                    "Labels": {
                        "shinken.owner_pid": "77",
                        "shinken.created_at": "1718000000.5",
                    },
                    "Env": ["FOO=bar", "SHINKEND_TOKEN=c0a0secrettoken"],
                    "Image": "shinken/sandbox-linux",
                },
                "NetworkSettings": {
                    "Ports": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49154"}]}
                },
            }
        ]
    )
    _docker_router(
        monkeypatch,
        [
            (lambda c: c[:2] == ["docker", "ps"], "abc123def456\n"),
            (lambda c: c[:2] == ["docker", "inspect"] and "-f" not in c, inspect_json),
        ],
    )
    handles = DockerLocalProvider().list()
    assert len(handles) == 1
    h = handles[0]
    assert h.sandbox_id == "shinken-local-rebuilt1"
    assert h.addr == "127.0.0.1:49154"  # addr rebuilt from the port map
    assert h.token == "c0a0secrettoken"  # token recovered from the container env
    assert h.created_at == 1718000000.5
    assert h.metadata["owner_pid"] == 77
    # repr never leaks the recovered token (medium fix #8)
    assert "c0a0secrettoken" not in repr(h)
    assert "c0a0…" in repr(h)


def test_list_empty_when_no_labeled_containers(monkeypatch):
    _docker_router(monkeypatch, [(lambda c: c[:2] == ["docker", "ps"], "")])
    assert DockerLocalProvider().list() == []


def test_base_provider_list_and_gc_unsupported_by_default(mock_shinkend):
    provider = shinken.ExternalProvider(addr=mock_shinkend)
    with pytest.raises(UnsupportedProviderOperation):
        provider.list()
    with pytest.raises(UnsupportedProviderOperation):
        provider.gc()


def test_sandbox_handle_repr_redacts_token():
    h = SandboxHandle(
        provider="docker-local",
        sandbox_id="s1",
        addr="127.0.0.1:1",
        token="c0a0deadbeefcafe",
    )
    assert "c0a0deadbeefcafe" not in repr(h)
    assert "token='c0a0…'" in repr(h)
    assert "token=None" in repr(SandboxHandle(provider="p", sandbox_id="s", addr="a"))


def test_sandbox_handle_repr_recursively_redacts_metadata_credentials():
    metadata: dict = {
        "safe": "visible",
        "api_token": "top-secret-token",
        "nested": [
            {"AWS_SECRET_ACCESS_KEY": "cloud-secret"},
            {"headers": {"Authorization": "Bearer bearer-secret"}},
            "PASSWORD=env-secret",
            "-----BEGIN " + "PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
        ],
    }
    metadata["cycle"] = metadata
    blob = repr(SandboxHandle(provider="p", sandbox_id="s", addr="a", metadata=metadata))
    for secret in (
        "top-secret-token",
        "cloud-secret",
        "bearer-secret",
        "env-secret",
        "private-material",
    ):
        assert secret not in blob
    assert "visible" in blob
    assert "<recursive>" in blob


# ------------------------------------------------- 5. snapshot labels + gc + fork GC


def _mock_docker(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=30.0):
        calls.append(cmd)
        out = "sha256:api-v2-image\n" if cmd[:2] == ["docker", "commit"] else "cid\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("shinken.providers.docker._run", fake_run)
    monkeypatch.setattr("shinken.providers.docker._free_port", lambda _host: 19012)
    monkeypatch.setattr(DockerLocalProvider, "_wait_ready", lambda _self, _handle: None)
    return calls


def _container_handle(cid: str = "c1") -> SandboxHandle:
    return SandboxHandle(
        provider="docker-local", sandbox_id=cid, addr="127.0.0.1:1", metadata={"container_id": cid}
    )


def test_snapshot_commit_stamps_labels(monkeypatch):
    calls = _mock_docker(monkeypatch)
    DockerLocalProvider().snapshot(_container_handle(), name="labeled")
    commit = calls[0]
    assert commit[:2] == ["docker", "commit"]
    assert "LABEL shinken.snapshot=true" in commit
    assert any(a == f"LABEL shinken.owner_pid={os.getpid()}" for a in commit)
    assert any(a.startswith("LABEL shinken.created_at=") for a in commit)


def test_fork_image_gc_accounting(monkeypatch):
    # The audit's leak: every fork left one shinken-snap:* image behind. Now the
    # intermediate commit rides the child handle and is reclaimed with it.
    calls = _mock_docker(monkeypatch)
    rmis: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        rmis.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)
    provider = DockerLocalProvider()
    child = provider.fork(_container_handle("c1"))
    snap = child.metadata["fork_snapshot"]
    assert snap.startswith("shinken-snap:")
    assert snap in provider._snapshots  # accounted while the child lives
    image_ref = provider._snapshot_images[snap]
    # the intermediate must NOT propagate into descendants' specs (grandchild forks)
    assert "fork_snapshot" not in provider._spec_from_handle(child).metadata

    provider.destroy(child)
    assert any(c[:3] == ["docker", "rmi", "-f"] and image_ref in c for c in rmis), rmis
    assert snap not in provider._snapshots  # zero images left per fork+destroy cycle
    assert any(c[:2] == ["docker", "commit"] for c in calls)


def test_gc_skips_live_owners_unless_force(monkeypatch):
    dead, live = _dead_pid(), os.getpid()
    rmis: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        rmis.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)

    def container_inspect(c):
        return c[:2] == ["docker", "inspect"] and "-f" in c

    def image_inspect(c):
        return c[:3] == ["docker", "image", "inspect"]

    calls = _docker_router(
        monkeypatch,
        [
            (lambda c: c[:2] == ["docker", "ps"], "c-dead\nc-live\n"),
            (image_inspect, f"i-dead\t{dead}\t1.0\ni-live\t{live}\t1.0"),
            (container_inspect, f"c-dead\t{dead}\t1.0\nc-live\t{live}\t1.0"),
            (lambda c: c[:2] == ["docker", "images"], "i-dead\ni-live\n"),
        ],
    )
    provider = DockerLocalProvider()
    report = provider.gc(snapshots=True)
    assert isinstance(report, GcReport)
    assert report.containers == 1 and report.images == 1 and report.skipped == 2
    images_query = next(c for c in calls if c[:3] == ["docker", "images", "-q"])
    assert "label=shinken.provider=docker-local" in images_query
    rm = next(c for c in calls if c[:3] == ["docker", "rm", "-f"])
    assert rm[3:] == ["c-dead"]
    assert ["docker", "rmi", "-f", "i-dead"] in rmis
    assert not any("i-live" in c for c in rmis)

    calls.clear()
    rmis.clear()
    forced = provider.gc(snapshots=True, force=True)
    assert forced.containers == 2 and forced.images == 2 and forced.skipped == 0


def test_gc_containers_only_by_default(monkeypatch):
    dead = _dead_pid()
    calls = _docker_router(
        monkeypatch,
        [
            (lambda c: c[:2] == ["docker", "ps"], "c-dead\n"),
            (lambda c: c[:2] == ["docker", "inspect"], f"c-dead\t{dead}\t1.0"),
        ],
    )
    report = DockerLocalProvider().gc()
    assert report.containers == 1 and report.images == 0
    assert not any(c[:2] == ["docker", "images"] for c in calls)


def test_cleanup_snapshots_sweeps_by_label_for_this_owner(monkeypatch):
    rmis: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        rmis.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("shinken.providers.docker.subprocess.run", fake_subprocess_run)
    calls = _docker_router(
        monkeypatch,
        [(lambda c: c[:2] == ["docker", "images"], "shinken-snap:leftover\n")],
    )
    provider = DockerLocalProvider()  # fresh instance: empty in-memory registry
    assert provider.cleanup_snapshots() == 1  # found globally by label anyway
    assert ["docker", "rmi", "-f", "shinken-snap:leftover"] in rmis
    images_cmd = next(c for c in calls if c[:2] == ["docker", "images"])
    assert f"label=shinken.owner_pid={os.getpid()}" in images_cmd  # never a sibling's


# --------------------------------------------------- 6+7. Checkpoint / fleet / spawn


def test_checkpoint_is_first_class_and_str_compatible(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    with provider.session() as env:
        ckpt = env.checkpoint("golden")
        assert isinstance(ckpt, shinken.Checkpoint)
        # today's str-consumers keep working verbatim
        assert isinstance(ckpt, str) and ckpt == "ckpt-xyz" and str(ckpt) == "ckpt-xyz"
        assert ckpt.startswith("ckpt-") and json.dumps({"id": ckpt})
        assert ckpt.id == "ckpt-xyz" and ckpt.name == "golden"
        assert ckpt.provider is provider
        assert ckpt.spec == {"spec_for": "ckpt-xyz"}  # spec-compat info rides along
        assert "ckpt-xyz" in repr(ckpt)

        sib = ckpt.spawn()
        try:
            assert sib.handle.sandbox_id == "restore-1"
            assert sib.ping() >= 0
        finally:
            sib.destroy()
        ckpt.delete()
        assert provider.deleted_snapshots == ["ckpt-xyz"]


def test_checkpoint_requires_provider_is_typed(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(shinken.ProviderRequired):
            env.checkpoint("nope")
    orphan = shinken.Checkpoint("ckpt-orphan")
    with pytest.raises(shinken.ProviderRequired):
        orphan.spawn()
    with pytest.raises(shinken.ProviderRequired):
        orphan.delete()


def test_checkpoint_spawn_destroys_restored_handle_when_connect_fails():
    provider = _FakeForkProvider("unused")

    def fail_connect(_handle, **_kwargs):
        raise RuntimeError("handshake failed")

    provider.connect = fail_connect
    checkpoint = shinken.Checkpoint("ckpt-xyz", provider=provider)
    with pytest.raises(RuntimeError, match="handshake failed"):
        checkpoint.spawn()
    assert provider.destroyed == ["restore-1"]


def test_checkpoint_spawn_preserves_connect_error_when_destroy_also_fails():
    provider = _FakeForkProvider("unused")
    attempted: list[str] = []

    def fail_connect(_handle, **_kwargs):
        raise ValueError("primary connect failure")

    def fail_destroy(handle):
        attempted.append(handle.sandbox_id)
        raise RuntimeError("secondary destroy failure")

    provider.connect = fail_connect
    provider.destroy = fail_destroy
    checkpoint = shinken.Checkpoint("ckpt-xyz", provider=provider)
    with pytest.raises(ValueError, match="primary connect failure"):
        checkpoint.spawn()
    assert attempted == ["restore-1"]


def test_spawn_many_returns_fleet_and_destroys_all_on_exit(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    with provider.session() as env:
        ckpt = env.checkpoint()
        with ckpt.spawn_many(3) as fleet:
            assert isinstance(fleet, shinken.SandboxFleet)
            assert len(fleet) == 3 and len(fleet.envs) == 3
            rtts = fleet.map(lambda e: e.ping())
            assert len(rtts) == 3 and all(rtt >= 0 for rtt in rtts)
        # destroy-all on exit: every restored replica was provider-destroyed
        assert {f"restore-{i}" for i in (1, 2, 3)} <= set(provider.destroyed)
        for member in fleet.envs:
            with pytest.raises(shinken.SessionClosed):
                _watchdog(member.ping)


def test_spawn_many_destroys_failed_and_connected_replicas_on_partial_connect_failure(
    mock_shinkend,
):
    provider = _FakeForkProvider(mock_shinkend)
    real_connect = provider.connect

    def connect_with_one_failure(handle, **kwargs):
        if handle.sandbox_id == "restore-2":
            raise RuntimeError("replica handshake failed")
        return real_connect(handle, **kwargs)

    provider.connect = connect_with_one_failure
    checkpoint = shinken.Checkpoint("ckpt-xyz", provider=provider)
    with pytest.raises(RuntimeError, match="replica handshake failed"):
        checkpoint.spawn_many(3)
    assert set(provider.destroyed) == {"restore-1", "restore-2", "restore-3"}


def test_fleet_map_is_concurrent_not_serial(mock_shinkend):
    """Concurrency asserted STRUCTURALLY (overlap observed via a counter), not by
    wall-clock — a 2-core CI runner can stretch 4 parallel naps past any timing
    bound without being serial."""
    provider = _FakeForkProvider(mock_shinkend)
    with provider.session() as env:
        ckpt = env.checkpoint()
        n, nap = 4, 0.2
        in_flight, peak = [0], [0]
        gate = threading.Lock()

        def probe(e):
            e.ping()
            with gate:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
            time.sleep(nap)
            with gate:
                in_flight[0] -= 1

        with ckpt.spawn_many(n) as fleet:
            fleet.map(probe)
        assert peak[0] >= 2, f"fleet.map never overlapped: peak concurrency {peak[0]}"


def test_sandbox_spawn_returns_connected_sibling(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    with provider.session() as env:
        sib = env.spawn()
        try:
            assert sib.handle.sandbox_id == f"fork-of-{env.handle.sandbox_id}"
            assert sib.ping() >= 0
        finally:
            sib.destroy()


def test_sandbox_spawn_destroys_fork_when_connect_fails(mock_shinkend):
    provider = _FakeForkProvider(mock_shinkend)
    base = provider.create()
    env = provider.connect(base)
    real_connect = provider.connect

    def fail_fork_connect(handle, **kwargs):
        if handle.sandbox_id.startswith("fork-of-"):
            raise RuntimeError("fork handshake failed")
        return real_connect(handle, **kwargs)

    provider.connect = fail_fork_connect
    try:
        with pytest.raises(RuntimeError, match="fork handshake failed"):
            env.spawn()
        assert f"fork-of-{base.sandbox_id}" in provider.destroyed
    finally:
        env.destroy()


def test_spawn_without_provider_is_typed(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(shinken.ProviderRequired):
            env.spawn()


# ----------------------------------------------------------------- 9. typed errors


def test_dead_addr_raises_typed_connect_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here any more
    with pytest.raises(shinken.ConnectError) as exc_info:
        shinken.connect(f"127.0.0.1:{port}")
    # back-compat: still a ConnectionError AND a ShinkenError/RuntimeError
    assert isinstance(exc_info.value, ConnectionError)
    assert isinstance(exc_info.value, shinken.ShinkenError)
    assert str(port) in str(exc_info.value)


def test_unknown_verb_is_typed(mock_shinkend_no_structured):
    # A pre-engine runtime answers "unknown verb: invoke_action" — typed, not stringly.
    with shinken.connect(mock_shinkend_no_structured) as env:
        with pytest.raises(shinken.UnknownVerb):
            env.act("invoke_action", {"kind": "element_ref", "ref": "e1"})
        with pytest.raises(RuntimeError):  # back-compat: UnknownVerb IS a RuntimeError
            env.act("invoke_action", {"kind": "element_ref", "ref": "e1"})


def test_exec_not_advertised_is_unknown_verb(mock_shinkend_no_exec):
    with shinken.connect(mock_shinkend_no_exec) as env:
        with pytest.raises(shinken.UnknownVerb, match="not advertised"):
            env.exec(["true"])


# ---------------------------------------------------- 10. screenshot(max_long_edge=)


def test_screenshot_max_long_edge_ships_on_the_wire(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        shot = env.screenshot(max_long_edge=640, format="jpeg", quality=70)
        assert shot["bytes"] and shot["format"] == "jpeg"
        assert "png" not in shot  # the deprecated alias only ever labels real PNG
        sent = env.query("state")["screenshots"][-1]
        assert sent["max_long_edge"] == 640
        assert sent["format"] == "jpeg" and sent["quality"] == 70
        # default PNG keeps the deprecated alias, bytes stays canonical
        shot2 = env.screenshot()
        assert shot2["png"] == shot2["bytes"]


# ------------------------------------------------------------------- 11. act_model


def test_act_model_one_liner_round_trip(mock_shinkend):
    adapter = AnthropicComputerUseAdapter()
    with shinken.connect(mock_shinkend) as env:
        result = env.act_model(adapter, {"action": "left_click", "coordinate": [30, 40]})
        # the adapter's tool_result shape, with the post-step frame inside
        assert result["content"][0]["type"] == "image"
        assert result["content"][0]["source"]["media_type"] == "image/png"
        clicks = env.query("state")["clicks"]
        assert {"verb": "click", "kind": "point_px", "x": 30, "y": 40} in clicks


def test_act_model_failure_is_typed(mock_shinkend_no_structured):
    adapter = AnthropicComputerUseAdapter()

    class _RawAdapter:  # drives an unknown verb through act_model
        def to_aci_action(self, tool_call):
            return {"verb": "invoke_action", "target": {"kind": "element_ref", "ref": "e1"}}

        def to_tool_result(self, observation):
            return observation

    with shinken.connect(mock_shinkend_no_structured) as env:
        with pytest.raises(shinken.ShinkenError, match="invoke_action"):
            env.act_model(_RawAdapter(), {})
        # adapter translation errors keep the adapter's typed error
        from shinken.adapters.base import AdapterError

        with pytest.raises(AdapterError):
            env.act_model(adapter, {"action": "cursor_position"})


# --------------------------------------------- 12. eval receipt validation + one Task


def test_verifier_dict_receipt_is_coerced_to_a_pass(mock_shinkend):
    # The audit's repro: a dict return surfaced as agent_error (+ setup_errors=n).
    task = ev.Task(
        "dict-receipt",
        run=lambda env: env.type_text("ok"),
        verify=lambda env: {"passed": True, "checks": [{"name": "typed", "ok": True}]},
    )
    summary = ev.run_eval(task, lambda: shinken.connect(mock_shinkend), n=1)
    assert summary.passed == 1 and summary.setup_errors == 0
    assert summary.kinds == {"pass": 1}
    assert summary.results[0].receipt.checks[0]["name"] == "typed"


def test_verifier_garbage_receipt_is_typed_scorer_error(mock_shinkend):
    task = ev.Task("garbage", run=lambda env: None, verify=lambda env: "looks great!")
    summary = ev.run_eval(task, lambda: shinken.connect(mock_shinkend), n=1)
    r = summary.results[0]
    assert r.passed is False
    assert r.exit_reason == "scorer_error"  # typed ScorerError, not a fake verdict
    assert "VerifierReceipt" in (r.error or "")


def test_task_is_one_dataclass_across_eval_and_gym():
    assert g.GymTask is ev.Task  # the deprecated alias points at the unified dataclass
    assert g.Task is ev.Task
    task = ev.Task("t", instruction="do the thing", metadata={"k": "v"})
    assert task.instruction == "do the thing" and task.metadata == {"k": "v"}
    assert task.run is None and task.verify is None and task.setup is None
    # eval's harness still demands run+verify eagerly (typed, not a downstream crash)
    with pytest.raises(ValueError, match="needs both run and verify"):
        ev.run_eval(task, lambda: None, n=1)


# ------------------------------------------------------- 13. CLI ps/gc + hygiene


class _CliProvider(SandboxProvider):
    capabilities = ProviderCapabilities(
        name="fake-cli", supports_lifecycle=True, supports_gui=False
    )

    def __init__(self) -> None:
        self.gc_args: list = []

    def create(self, spec=None):  # pragma: no cover — registry factory needs the class
        raise NotImplementedError

    def destroy(self, handle):  # pragma: no cover
        raise NotImplementedError

    def list(self):
        return [
            SandboxHandle(
                provider="fake-cli",
                sandbox_id="shinken-local-cli1",
                addr="127.0.0.1:49001",
                token="c0a0secret",
                created_at=time.time() - 30,
                metadata={"owner_pid": 4242},
            )
        ]

    def gc(self, snapshots=False, force=False):
        self.gc_args.append((snapshots, force))
        return GcReport(containers=2, images=1, skipped=3)


@pytest.fixture
def cli_provider():
    provider = _CliProvider()
    providers_registry.register("fake-cli", lambda: provider)
    try:
        yield provider
    finally:
        providers_registry._REGISTRY.pop("fake-cli", None)


def test_cli_ps_lists_handles_without_leaking_tokens(cli_provider, capsys):
    rc = cli.main(["ps", "--provider", "fake-cli"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "shinken-local-cli1" in out and "127.0.0.1:49001" in out and "4242" in out
    assert "c0a0secret" not in out


def test_cli_gc_prints_report(cli_provider, capsys):
    rc = cli.main(["gc", "--provider", "fake-cli", "--snapshots"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "reclaimed 2 container(s), 1 snapshot image(s)" in out and "skipped 3" in out
    assert cli_provider.gc_args == [(True, False)]


def test_cli_version_is_stamped(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    assert "0.1.0" in capsys.readouterr().out
    assert shinken.__version__ == "0.1.0"


def test_gym_env_is_a_context_manager_and_disposes(mock_shinkend):
    class _Provider(_FakeForkProvider):
        def checkpoint(self, handle, **kw):
            return "golden-ctx"

    provider = _Provider(mock_shinkend)
    with g.make(ev.Task("ctx", instruction="x"), provider) as env:
        assert env.golden_checkpoint == "golden-ctx"
        env.reset()
    assert "golden-ctx" in provider.deleted_snapshots  # dispose() ran on exit


def test_module_all_hygiene():
    for mod in (ev, g):
        for name in mod.__all__:
            assert hasattr(mod, name), f"{mod.__name__}.__all__ lists missing {name!r}"
    assert "Task" in ev.__all__ and "Task" in g.__all__ and "GymTask" in g.__all__
    for name in (
        "Checkpoint",
        "SandboxFleet",
        "SessionClosed",
        "ConnectError",
        "UnknownVerb",
        "ProviderRequired",
        "ScorerError",
        "GcReport",
    ):
        assert name in shinken.__all__ and hasattr(shinken, name)


# ---------------------------------------------------------------------- live (Docker)

requires_docker = pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCKER_TESTS") != "1",
    reason="live Docker test: set SHINKEN_DOCKER_TESTS=1 (needs the shinken/sandbox-linux image)",
)


@requires_docker
def test_live_session_checkpoint_spawn_and_list():
    provider = DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    with provider.session() as env:
        assert env.screenshot()["bytes"][:8] == b"\x89PNG\r\n\x1a\n"
        own_id = env.handle.sandbox_id
        listed = {h.sandbox_id: h for h in provider.list()}
        assert own_id in listed
        assert listed[own_id].token  # token recovered from the container env
        assert listed[own_id].token not in repr(listed[own_id])  # and redacted in repr

        ckpt = env.checkpoint("api-v2-live")
        assert isinstance(ckpt, shinken.Checkpoint)
        sibling = ckpt.spawn()
        try:
            assert sibling.screenshot()["bytes"][:8] == b"\x89PNG\r\n\x1a\n"
            assert sibling.handle.sandbox_id != own_id
        finally:
            sibling.destroy()
        ckpt.delete()
    # session() reclaimed the substrate on exit
    assert own_id not in {h.sandbox_id for h in provider.list()}


@requires_docker
def test_live_spawn_fork_image_gc_and_owner_safe_gc():
    provider = DockerLocalProvider(image="shinken/sandbox-linux", startup_timeout=120.0)
    with provider.session() as env:
        sibling = env.spawn()
        snap = sibling.handle.metadata["fork_snapshot"]
        assert snap.startswith("shinken-snap:")
        sibling.destroy()
        # the fork's intermediate commit was reclaimed with the child
        out = subprocess.run(
            ["docker", "images", "-q", snap], capture_output=True, text=True, timeout=30
        )
        assert out.stdout.strip() == "", f"fork image {snap} leaked"
        # gc must NOT touch this live process's session
        report = provider.gc(snapshots=True)
        assert report.skipped >= 1
        assert env.ping() >= 0
