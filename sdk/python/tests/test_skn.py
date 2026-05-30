"""M2: the `.skn` replay bundle — record, save, load, scrub."""

from __future__ import annotations

import shinken
from shinken.skn import Recorder, Replay, summarize

_PNG = b"\x89PNG\r\n\x1a\nDATA"


def test_recorder_roundtrip(tmp_path):
    rec = Recorder(platform="linux")
    rec.action("click", {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}}, "c1")
    rec.observation({"image": {"w": 2, "h": 2, "scope": "screen"}}, png=_PNG, action_id="c1")
    rec.marker("done")

    path = str(tmp_path / "run.skn")
    rec.save(path)

    rp = Replay.load(path)
    assert len(rp) == 3
    assert rp.manifest["skn_version"] == 0
    assert [e["kind"] for e in rp.events] == ["action", "observation", "marker"]
    assert [e["seq"] for e in rp.events] == [0, 1, 2]
    # content-addressed media round-trips
    sha = rp.events[1]["payload"]["image"]["ref"]
    assert rp.media(sha) == _PNG
    # action paired to observation by id
    assert rp.events[0]["action_id"] == rp.events[1]["action_id"] == "c1"


def test_media_dedup():
    rec = Recorder()
    rec.observation({}, png=_PNG)
    rec.observation({}, png=_PNG)  # identical → same sha, stored once
    assert len(rec._media) == 1


def test_record_session_to_skn(mock_shinkend, tmp_path):
    path = str(tmp_path / "session.skn")
    with shinken.connect(mock_shinkend, record=True) as env:
        env.click(x=10, y=20)
        env.type_text("hi")
        env.screenshot()
        env.save_replay(path)

    rp = Replay.load(path)
    kinds = [e["kind"] for e in rp.events]
    assert kinds.count("action") == 2 and "observation" in kinds
    obs = next(e for e in rp.events if e["kind"] == "observation")
    assert rp.media(obs["payload"]["image"]["ref"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert "run.skn" not in summarize(path)  # summarize names the given path
    assert "events" in summarize(path)
