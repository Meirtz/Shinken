"""Dirty-tile delta screencast (B2) + image-codec capability negotiation.

Wire travel of the ``delta`` flag, raw tile passthrough in ``next_frame``, the
delta-remembering resume path, and the typed pre-send rejection of a codec the
runtime's welcome did not advertise — all against the schema-validating mock
shinkend (so any out-of-contract emission fails loudly).
"""

from __future__ import annotations

import asyncio

import pytest

import shinken
from shinken.client import AsyncSandbox, Capabilities, _parse_welcome

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


# --- delta wire travel + tile passthrough -------------------------------------------


def test_delta_flag_travels_and_tiles_come_back_raw(mock_shinkend):
    """``delta=True`` goes over the wire (schema-validated by the mock); the first
    frame is a full keyframe and later frames are raw tile dicts with decoded bytes
    and NO 'bytes'/'png' keys — compositing is the consumer's job."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=4, delta=True) as stream:
            frames = list(stream)
        assert env.query("state")["casts"][-1]["delta"] is True

        # frame 0: the keyframe — a normal full image
        key = frames[0]
        assert key["png"][:8] == _PNG_SIG
        assert "tiles" not in key

        # frames 1..: dirty tiles only
        for f in frames[1:]:
            assert "bytes" not in f and "png" not in f
            tiles = f["tiles"]
            assert len(tiles) == 1
            t = tiles[0]
            assert (t["x"], t["y"], t["w"], t["h"]) == (0, 0, 1, 1)
            assert t["bytes"][:8] == _PNG_SIG  # decoded from base64, raw passthrough
        # seq still advances across keyframe + tile frames
        assert [f["seq"] for f in frames] == [0, 1, 2, 3]


def test_screencast_without_delta_never_sends_the_flag(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=1) as stream:
            frames = list(stream)
        assert "tiles" not in frames[0]
        # an omitted delta must stay OFF the wire (schema-minimal action)
        assert env.query("state")["casts"][-1]["delta"] is None


def test_resume_remembers_delta_and_restarts_with_a_keyframe(mock_shinkend):
    """Resume reuses the remembered ``delta`` like format/quality (#56) — and the
    resumed stream re-keys: its first frame is a full image, not tiles, because the
    runtime's tile baseline does not survive the stream task."""
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, timeout=5, limit=2, delta=True) as stream:
            frames = list(stream)
        old_id = frames[0]["stream"]

        with env.resume_screencast(old_id, timeout=5, limit=2) as stream:
            resumed = list(stream)
        last = env.query("state")["casts"][-1]
        assert last["resume_stream"] == old_id
        assert last["delta"] is True  # remembered, like format/quality
        assert resumed[0]["png"][:8] == _PNG_SIG  # keyframe first on resume
        assert resumed[0]["seq"] > frames[-1]["seq"]  # same logical stream, seq carries on
        assert "tiles" in resumed[1]


# --- image-codec capability negotiation ---------------------------------------------


def test_mock_welcome_advertises_both_codecs(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.capabilities.image_formats == ["png", "jpeg"]


def test_parse_welcome_defaults_image_formats_to_png_only():
    """A welcome from a pre-negotiation runtime (no image_formats) parses as
    png-only — those runtimes only ever encoded PNG."""
    caps, _ = _parse_welcome(
        {
            "type": "welcome",
            "v": 0,
            "server": {"platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click"],
                "targets": ["point_px"],
                "observation_types": ["screenshot"],
            },
        }
    )
    assert caps.image_formats == ["png"]


def test_capabilities_dataclass_defaults_to_png_only():
    assert Capabilities(0, [], [], []).image_formats == ["png"]


class _RecordingWS:
    """A fake transport that records sends — proves rejection happens BEFORE the wire."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, s):
        self.sent.append(s)

    async def close(self):
        pass


def test_unadvertised_format_raises_before_sending():
    """Requesting a codec outside ``capabilities.image_formats`` raises a typed
    ValueError with NOTHING sent — no nack round-trip against a runtime that already
    said it can't encode it."""
    ws = _RecordingWS()
    sb = AsyncSandbox(ws, Capabilities(0, [], [], []), "linux")  # png-only default

    async def go():
        with pytest.raises(ValueError, match="jpeg"):
            await sb.screenshot(format="jpeg")
        with pytest.raises(ValueError, match="jpeg"):
            await sb.astart_screencast(format="jpeg")

    asyncio.run(go())
    assert ws.sent == []  # rejected before anything hit the transport


def test_advertised_format_still_goes_through(mock_shinkend):
    """The negotiation gate must not block a codec the runtime DOES advertise."""
    with shinken.connect(mock_shinkend) as env:
        shot = env.screenshot(format="jpeg")
        assert shot["format"] == "jpeg"
