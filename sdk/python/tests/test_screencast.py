"""Screencast: the SDK starts a server-pushed stream and demuxes frames from RPC
replies, exercised against the in-process mock shinkend."""

from __future__ import annotations

import shinken

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_screencast_streams_distinct_frames(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert "start_screencast" in env.capabilities.verbs
        assert "screencast" in env.capabilities.observation_types

        frames = []
        with env.screencast(fps=30, timeout=5, limit=5) as stream:
            for frame in stream:
                frames.append(frame)

        assert len(frames) == 5
        assert [f["seq"] for f in frames] == [0, 1, 2, 3, 4]
        assert all(f["png"][:8] == _PNG_SIG for f in frames)
        assert all(f["stream"] for f in frames)


def test_rpc_still_works_after_screencast(mock_shinkend):
    """The reader/demux must keep RPC replies and stream frames separate."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=3) as stream:
            collected = list(stream)
        assert len(collected) == 3
        # RPC round-trips must still resolve correctly once the stream has stopped.
        assert env.ping() >= 0.0
        assert env.screen_size() == {"w": 1280, "h": 800}
        shot = env.screenshot()
        assert shot["png"][:8] == _PNG_SIG


def test_screencast_sends_max_long_edge(mock_shinkend):
    """The bandwidth cap must travel over the wire to the runtime (the mock echoes
    it back as the frame width)."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=3, max_long_edge=640) as stream:
            frames = list(stream)
        assert len(frames) == 3
        assert all(f["w"] == 640 for f in frames)


def test_screencast_stop_makes_next_frame_end(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=2) as stream:
            assert len(list(stream)) == 2

        # Stop pushes an explicit end sentinel so callers don't block forever after
        # a stream has ended.
        assert env._loop.run(env._inner.next_frame(timeout=5)) is None


def test_screencast_restart_clears_stale_frames(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        first_stream = env._loop.run(env._inner.astart_screencast(fps=30))
        first = env._loop.run(env._inner.next_frame(timeout=5))
        assert first is not None and first["stream"] == first_stream

        second_stream = env._loop.run(env._inner.astart_screencast(fps=30))
        second = env._loop.run(env._inner.next_frame(timeout=5))
        assert second is not None
        assert second["stream"] == second_stream
        assert second["stream"] != first_stream

        env._loop.run(env._inner.astop_screencast())
