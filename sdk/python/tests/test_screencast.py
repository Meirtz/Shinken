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
