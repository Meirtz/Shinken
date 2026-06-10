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


def test_resume_screencast_continues_stream_and_seq(mock_shinkend):
    """A resumed stream keeps the logical stream id and seq carries on (#56), so the
    frame gap is readable off the first resumed frame."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=3) as stream:
            frames = list(stream)
        old_id, last_seq = frames[-1]["stream"], frames[-1]["seq"]

        with env.resume_screencast(old_id, fps=30, timeout=5, limit=2) as stream:
            resumed = list(stream)
        assert env.active_stream == old_id
        assert all(f["stream"] == old_id for f in resumed)
        assert resumed[0]["seq"] > last_seq
        assert resumed[1]["seq"] == resumed[0]["seq"] + 1


def test_resume_screencast_across_reconnect(mock_shinkend):
    """The reconnect contract (#56): after a drop, a NEW Sandbox against the same
    runtime resumes the old session's logical stream — same id, seq continuing."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=3) as stream:
            frames = list(stream)
        old_id, last_seq = frames[-1]["stream"], frames[-1]["seq"]

    with shinken.connect(mock_shinkend) as env2:
        with env2.resume_screencast(old_id, fps=30, timeout=5, limit=2) as stream:
            resumed = list(stream)
        assert resumed[0]["stream"] == old_id
        assert resumed[0]["seq"] > last_seq


def test_resume_unknown_stream_starts_fresh(mock_shinkend):
    """An unknown (or expired) resume_stream starts fresh — new id, seq 0 — so the
    client can tell continuity was lost from the first frame."""
    with shinken.connect(mock_shinkend) as env:
        with env.resume_screencast("ghost", fps=30, timeout=5, limit=2) as stream:
            frames = list(stream)
        assert frames[0]["stream"] != "ghost"
        assert frames[0]["seq"] == 0


def test_resume_fallback_adopts_live_stream_id(mock_shinkend):
    """After a resume that fell back to a fresh stream, the public ``active_stream``
    reflects the LIVE id observed on frames — not the requested dead one — so the
    next resume targets a stream the runtime actually holds (#56)."""
    with shinken.connect(mock_shinkend) as env:
        with env.resume_screencast("ghost", fps=30, timeout=5, limit=1) as stream:
            frames = list(stream)
        assert env.active_stream == frames[0]["stream"]
        assert env.active_stream != "ghost"


def test_resume_without_params_reuses_original_capture_params(mock_shinkend):
    """Resume continues stream identity + seq only — the runtime does not remember
    capture params — so a same-Sandbox resume re-sends the ORIGINAL fps/
    max_long_edge instead of silently resetting them to defaults (#56)."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(
            fps=17, timeout=5, limit=1, max_long_edge=320, format="jpeg", quality=60
        ) as stream:
            frames = list(stream)
        old_id = frames[0]["stream"]

        with env.resume_screencast(old_id, timeout=5, limit=1) as stream:
            resumed = list(stream)
        # the mock echoes max_long_edge as the frame width — the resumed frame keeps it
        assert resumed[0]["w"] == 320
        # and the mock records the params each start_screencast actually carried
        last = env.query("state")["casts"][-1]
        assert last["resume_stream"] == old_id
        assert last["fps"] == 17
        assert last["max_long_edge"] == 320
        # codec must survive a resume too — else a JPEG stream silently flips to the PNG
        # default on reconnect (~20x larger frames, the bandwidth lever lost)
        assert last["format"] == "jpeg"
        assert last["quality"] == 60
        assert resumed[0]["format"] == "jpeg"


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
