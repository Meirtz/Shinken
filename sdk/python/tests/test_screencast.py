"""Screencast: the SDK starts a server-pushed stream, demuxes frames from RPC
replies, and records them — exercised against the in-process mock shinkend."""

from __future__ import annotations

import shinken
from shinken.skn import Replay

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def test_screencast_streams_distinct_frames(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert "start_screencast" in env.capabilities.verbs
        assert "screencast" in env.capabilities.observation_types

        frames = []
        with env.screencast(fps=100, timeout=5, limit=5) as stream:
            for frame in stream:
                frames.append(frame)

        assert len(frames) == 5
        assert [f["seq"] for f in frames] == [0, 1, 2, 3, 4]
        assert all(f["png"][:8] == _PNG_SIG for f in frames)
        assert all(f["stream"] for f in frames)


def test_rpc_still_works_after_screencast(mock_shinkend):
    """The reader/demux must keep RPC replies and stream frames separate."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=100, timeout=5, limit=3) as stream:
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
        with env.screencast(fps=100, timeout=5, limit=3, max_long_edge=640) as stream:
            frames = list(stream)
        assert len(frames) == 3
        assert all(f["w"] == 640 for f in frames)


def test_screencast_records_frames(mock_shinkend, tmp_path):
    path = tmp_path / "cast.skn"
    with shinken.connect(mock_shinkend, record=True) as env:
        with env.screencast(fps=100, timeout=5, limit=4) as stream:
            for _ in stream:
                pass
        env.save_replay(str(path))

    replay = Replay.load(str(path))
    obs = [e for e in replay.events if e["kind"] == "observation"]
    assert len(obs) == 4
    # each recorded frame's media is a real PNG, content-addressed
    sha = obs[0]["payload"]["image"]["ref"]
    assert replay.media(sha)[:8] == _PNG_SIG


def test_screencast_records_scoped_capture_region(mock_shinkend, tmp_path):
    """A scoped screencast must record its true capture region in the .skn, not a
    hardcoded ``screen`` — replay metadata otherwise overstates captured pixels (#143)."""
    path = tmp_path / "scoped.skn"
    with shinken.connect(mock_shinkend, record=True) as env:
        with env.screencast(fps=100, timeout=5, limit=3, scope="active_window") as stream:
            for _ in stream:
                pass
        env.save_replay(str(path))

    replay = Replay.load(str(path))
    obs = [e for e in replay.events if e["kind"] == "observation"]
    assert len(obs) == 3
    assert all(e["payload"]["image"]["scope"] == "active_window" for e in obs)


def test_screencast_stop_makes_next_frame_end(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=100, timeout=5, limit=2) as stream:
            assert len(list(stream)) == 2

        # Stop pushes an explicit end sentinel so callers don't block forever after
        # a stream has ended.
        assert env._loop.run(env._inner.next_frame(timeout=5)) is None


def test_screencast_restart_clears_stale_frames(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        first_stream = env._loop.run(env._inner.astart_screencast(fps=100))
        first = env._loop.run(env._inner.next_frame(timeout=5))
        assert first is not None and first["stream"] == first_stream

        second_stream = env._loop.run(env._inner.astart_screencast(fps=100))
        second = env._loop.run(env._inner.next_frame(timeout=5))
        assert second is not None
        assert second["stream"] == second_stream
        assert second["stream"] != first_stream

        env._loop.run(env._inner.astop_screencast())
