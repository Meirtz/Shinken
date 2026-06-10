"""SharedLoop: N sync sessions multiplexed on ONE background loop thread.

The async core (aconnect + asyncio.gather on the caller's loop) is the canonical way to
drive many sandboxes concurrently; SharedLoop only removes the sync facade's
thread-per-session resource cost without adding any orchestration surface."""

from __future__ import annotations

import threading

import pytest

import shinken


def _loop_threads() -> list[threading.Thread]:
    """Live 'shinken-loop' Thread OBJECTS — counted as objects, not deduped names (every
    _BackgroundLoop thread shares the same name, so a set of names would be vacuous)."""
    return [t for t in threading.enumerate() if t.name == "shinken-loop" and t.is_alive()]


def test_n_sessions_share_one_loop_thread(mock_shinkend_many):
    addrs = mock_shinkend_many(4)
    before = len(_loop_threads())
    with shinken.SharedLoop() as loop:
        envs = [shinken.connect(a, loop=loop) for a in addrs]
        try:
            # All four sessions are live and the process grew by exactly ONE loop thread.
            assert len(_loop_threads()) - before == 1
            for env in envs:
                shot = env.screenshot()
                assert shot["format"] == "png" and shot["w"] == 1
        finally:
            for env in envs:
                env.close()
        # Closing the sandboxes must NOT stop the caller-owned shared loop.
        assert len(_loop_threads()) - before == 1
    assert len(_loop_threads()) == before  # SharedLoop.close() stops its thread


def test_dedicated_loops_grow_per_session(mock_shinkend_many):
    """The contrast case: default connect() = one loop thread per session."""
    addrs = mock_shinkend_many(3)
    before = len(_loop_threads())
    envs = [shinken.connect(a) for a in addrs]
    try:
        assert len(_loop_threads()) - before == 3
    finally:
        for env in envs:
            env.close()
    assert len(_loop_threads()) == before  # each owned loop stopped with its Sandbox


def test_closing_one_shared_session_leaves_siblings_live(mock_shinkend_many):
    addrs = mock_shinkend_many(2)
    with shinken.SharedLoop() as loop:
        a = shinken.connect(addrs[0], loop=loop)
        b = shinken.connect(addrs[1], loop=loop)
        a.close()
        # Sibling on the same loop still works after a's close.
        assert b.ping() >= 0
        b.close()


def test_failed_dial_does_not_kill_the_shared_loop(mock_shinkend_many):
    addrs = mock_shinkend_many(1)
    with shinken.SharedLoop() as loop:
        env = shinken.connect(addrs[0], loop=loop)
        with pytest.raises(Exception):  # noqa: B017 — any connection error
            shinken.connect("127.0.0.1:1", loop=loop)
        # The earlier session on the same shared loop is unaffected.
        assert env.ping() >= 0
        env.close()


def test_shared_loop_close_is_idempotent(mock_shinkend_many):
    loop = shinken.SharedLoop()
    loop.close()
    loop.close()  # second close is a no-op, no raise
