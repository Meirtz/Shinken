"""M2: the `.skn` replay bundle — record, save, load, scrub."""

from __future__ import annotations

import jsonschema
import pytest

import shinken
from shinken import cli
from shinken.skn import Recorder, Replay, _validate_bundle, check_pairing, summarize

_PNG = b"\x89PNG\r\n\x1a\nDATA"


def _ev(seq, kind, src, action_id=None, dt=0.0):
    e = {"seq": seq, "dt": dt, "kind": kind, "src": src, "payload": {}}
    if action_id is not None:
        e["action_id"] = action_id
    return e


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
    # click + type_text + screenshot — screenshot is recorded as an action too (#160)
    assert kinds.count("action") == 3 and "observation" in kinds
    obs = next(e for e in rp.events if e["kind"] == "observation")
    assert rp.media(obs["payload"]["image"]["ref"])[:8] == b"\x89PNG\r\n\x1a\n"
    # the screenshot observation is paired to its screenshot action via action_id (#160)
    shot = next(e for e in rp.events if e["kind"] == "action" and e["src"] == "screenshot")
    assert obs.get("action_id") == shot.get("action_id") and shot.get("action_id")


def test_replay_is_not_a_runtime_checkpoint(mock_shinkend, tmp_path):
    """save_replay() produces a replay bundle, never a runtime checkpoint: it must not
    emit snapshot_ref events, since runtime checkpoint/restore/fork is unimplemented (#42)."""
    path = str(tmp_path / "session.skn")
    with shinken.connect(mock_shinkend, record=True) as env:
        env.click(x=1, y=2)
        env.screenshot()
        env.save_replay(path)

    rp = Replay.load(path)
    assert not any(e["kind"] == "snapshot_ref" for e in rp.events)
    assert not any("snapshot_ref" in e for e in rp.events)
    assert "run.skn" not in summarize(path)  # summarize names the given path
    assert "events" in summarize(path)


def test_replay_steps_group_action_with_observations(tmp_path):
    rec = Recorder(platform="linux")
    rec.action("click", {"verb": "click"}, "c1")
    rec.observation({"image": {"w": 1, "h": 1, "scope": "screen"}}, png=_PNG, action_id="c1")
    rec.action("type_text", {"verb": "type_text", "text": "hi"}, "c2")
    rp = Replay.load(rec.save(str(tmp_path / "r.skn")))
    steps = rp.steps()
    assert len(steps) == 2
    assert steps[0]["action"]["action_id"] == "c1"
    assert [e["kind"] for e in steps[0]["events"]] == ["action", "observation"]
    assert steps[1]["action"]["action_id"] == "c2"


def test_check_pairing_detects_dangling_action_id():
    good = [
        _ev(0, "action", "click", action_id="c1"),
        _ev(1, "observation", "image", action_id="c1", dt=0.1),
    ]
    check_pairing(good)  # valid pairing — no raise
    bad = good + [_ev(2, "observation", "image", action_id="ZZZ", dt=0.2)]
    with pytest.raises(ValueError):
        check_pairing(bad)


def test_cli_replay_step_and_validate(tmp_path, capsys):
    rec = Recorder(platform="linux")
    rec.action("click", {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}}, "c1")
    rec.observation({"image": {"w": 2, "h": 2, "scope": "screen"}}, png=_PNG, action_id="c1")
    path = rec.save(str(tmp_path / "s.skn"))

    assert cli.main(["replay", path, "--step"]) == 0
    out = capsys.readouterr().out
    assert "step 1:" in out and "action[click]" in out and "media=" in out

    assert cli.main(["replay", path, "--validate"]) == 0
    assert "replay OK" in capsys.readouterr().out


def test_cli_replay_validate_fails_on_broken_pairing(tmp_path, capsys):
    rec = Recorder(platform="linux")
    rec.action("click", {"verb": "click"}, "c1")
    # schema-valid event, but its action_id points at no real action → pairing breaks
    rec._events.append(_ev(5, "observation", "image", action_id="NOPE", dt=0.5))
    path = rec.save(str(tmp_path / "broken.skn"))  # schema-valid; pairing dangling
    assert cli.main(["replay", path, "--validate"]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_capability_envelope_and_permissions_recorded(mock_shinkend, tmp_path):
    path = str(tmp_path / "cap.skn")
    with shinken.connect(mock_shinkend, record=True) as env:
        # envelope exposed in session metadata (reference semantics)
        assert env.sandbox_capabilities["input_automation"] is True
        assert env.sandbox_capabilities["egress"] is False
        env.click(x=1, y=1)
        env.record_permission("grant", capability="screenshot")
        env.record_permission("deny", capability="egress", reason="not in scope")
        env.save_replay(path)

    rp = Replay.load(path)
    rp.validate()  # schema + action/observation pairing
    # capability envelope is in the manifest
    assert rp.manifest["capabilities"]["screenshot"] is True
    # a capability meta event is recorded at session start
    cap = rp.events[0]
    assert cap["kind"] == "meta" and cap["src"] == "capability_envelope"
    assert cap["payload"]["capabilities"]["privileged_install"] is False
    # grant + deny decisions recorded and replayable
    perms = [e for e in rp.events if e["kind"] == "permission"]
    assert [p["src"] for p in perms] == ["grant", "deny"]
    assert perms[1]["payload"]["capability"] == "egress"
    assert perms[1]["payload"]["reason"] == "not in scope"


def test_capability_envelope_override():
    rec = Recorder(capabilities={"egress": "github.com", "clipboard": True})
    assert rec.capabilities["egress"] == "github.com"
    assert rec.capabilities["clipboard"] is True
    assert rec.capabilities["screenshot"] is True  # defaults retained


def test_redact_media_drops_bytes(tmp_path):
    import zipfile

    rec = Recorder(platform="linux", redact_media=True)
    rec.observation({"image": {"w": 2, "h": 2, "scope": "screen"}}, png=_PNG)
    path = rec.save(str(tmp_path / "red.skn"))
    rp = Replay.load(path)
    assert rp.manifest["redaction"]["media"] is True
    img = next(e for e in rp.events if e["kind"] == "observation")["payload"]["image"]
    # metadata-only (#206): the content hash (identity) is kept, the bytes are not
    assert img.get("redacted") is True
    assert isinstance(img["ref"], str) and len(img["ref"]) == 64  # sha256 retained
    assert img["w"] == 2 and img["h"] == 2 and img["scope"] == "screen"  # dimensions retained
    with zipfile.ZipFile(path) as z:  # and no raw bytes anywhere in the bundle
        assert not any(n.startswith("media/") for n in z.namelist())


def test_redaction_enum_maps_to_flags():
    from shinken.skn import Redaction, redaction_flags

    assert redaction_flags(Redaction.NONE) == (False, False)
    assert redaction_flags("text") == (False, True)
    assert redaction_flags("media") == (True, False)  # metadata-only media, text kept
    assert redaction_flags(Redaction.FULL) == (True, True)


def test_connect_redaction_full_redacts_text_and_keeps_media_hash(mock_shinkend, tmp_path):
    path = str(tmp_path / "r.skn")
    with shinken.connect(mock_shinkend, record=True, redaction="full") as env:
        env.type_text("s3cret")
        env.screenshot()
        env.save_replay(path)
    rp = Replay.load(path)
    assert rp.manifest["redaction"] == {"media": True, "text": True}
    act = next(e for e in rp.events if e["kind"] == "action" and e["src"] == "type_text")
    assert act["payload"]["text"] == "[redacted]"
    img = next(e for e in rp.events if e["kind"] == "observation")["payload"]["image"]
    assert img.get("redacted") is True and len(img["ref"]) == 64  # metadata-only: hash kept


def test_connect_default_keeps_full_content(mock_shinkend, tmp_path):
    # Default (no redaction) keeps full text + media — the privacy-safe-by-default flip is
    # deferred pending eval/verifier fidelity handling (#206).
    path = str(tmp_path / "n.skn")
    with shinken.connect(mock_shinkend, record=True) as env:
        env.type_text("plain")
        env.save_replay(path)
    rp = Replay.load(path)
    assert rp.manifest["redaction"] == {"media": False, "text": False}
    act = next(e for e in rp.events if e["kind"] == "action" and e["src"] == "type_text")
    assert act["payload"]["text"] == "plain"


def test_redact_text_strips_typed_secret():
    rec = Recorder(redact_text=True)
    ev = rec.action("type_text", {"verb": "type_text", "text": "hunter2"}, "c1")
    assert ev["payload"]["text"] == "[redacted]"


def test_sdk_redacted_run(mock_shinkend, tmp_path):
    import zipfile

    path = str(tmp_path / "s.skn")
    with shinken.connect(mock_shinkend, record=True, redact_media=True, redact_text=True) as env:
        env.type_text("s3cret")
        env.screenshot()
        env.record_permission("deny", capability="credentials", sensitive=True)
        env.save_replay(path)

    rp = Replay.load(path)
    rp.validate()
    assert rp.manifest["redaction"] == {"media": True, "text": True}
    act = next(e for e in rp.events if e["kind"] == "action" and e["src"] == "type_text")
    assert act["payload"]["text"] == "[redacted]"  # typed secret not persisted
    obs = next(e for e in rp.events if e["kind"] == "observation")
    assert obs["payload"]["image"].get("redacted") is True
    with zipfile.ZipFile(path) as z:
        assert not any(n.startswith("media/") for n in z.namelist())
    perm = next(e for e in rp.events if e["kind"] == "permission")
    assert perm["payload"]["sensitive"] is True  # permission can mark a sensitive scope
