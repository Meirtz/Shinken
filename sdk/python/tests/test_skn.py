"""M2: the `.skn` replay bundle — record, save, load, scrub."""

from __future__ import annotations

import jsonschema
import pytest

import shinken
from shinken.skn import Recorder, Replay, _validate_bundle, summarize

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


def test_save_is_atomic_no_leftover_temp(tmp_path):
    rec = Recorder(platform="linux")
    rec.marker("x")
    path = str(tmp_path / "run.skn")
    rec.save(path)
    assert Replay.load(path)  # complete + re-openable
    # the temp file is renamed into place, never left behind
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "run.skn"]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_save_rejects_malformed_event_then_writes_when_unvalidated(tmp_path):
    rec = Recorder(platform="linux")
    rec.action("click", {"verb": "click"}, "c1")
    # a malformed event slips into the log (bad kind) — strict save must reject it...
    rec._events.append({"seq": 99, "dt": 0.0, "kind": "teleport", "src": "x", "payload": {}})
    with pytest.raises(jsonschema.ValidationError):
        rec.save(str(tmp_path / "bad.skn"))
    # ...and validation runs BEFORE any write, so no partial/temp file is left
    assert list(tmp_path.iterdir()) == []
    # opt-out still writes a (re-loadable) bundle
    path = rec.save(str(tmp_path / "bad.skn"), validate=False)
    assert Replay.load(path)
    assert not any(p.name.endswith(".tmp") for p in tmp_path.iterdir())


def test_valid_bundle_passes_schema_validation():
    rec = Recorder(platform="linux")
    rec.action("click", {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}}, "c1")
    rec.observation({"image": {"w": 1, "h": 1, "scope": "screen"}}, png=_PNG, action_id="c1")
    rec.marker("done")
    _validate_bundle(rec.manifest(), rec.events)  # must not raise


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
