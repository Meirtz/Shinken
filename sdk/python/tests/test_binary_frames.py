"""Binary WebSocket media frames (change-proportional observation pipeline, half A).

A session that negotiates ``hello.accept.binary_frames`` against a runtime advertising
``capabilities.binary_frames`` receives every image-bearing observation as ONE binary
WS message — ``u32 LE header_len | JSON header | raw codec payload`` — instead of
base64-in-JSON text. These tests cover the frame parser, the end-to-end negotiated
path against the mock runtime, both back-compat fallbacks (client opt-out and
pre-binary server), and the payload-byte frame-budget accounting.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

import shinken
from shinken import client as client_mod
from shinken.client import _frame_resident_bytes, _parse_binary_frame

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)


def _frame(header: dict, payloads: list[bytes]) -> bytes:
    head = json.dumps(header).encode()
    return len(head).to_bytes(4, "little") + head + b"".join(payloads)


# ---- parser ----


def test_parse_binary_image_frame_returns_text_path_shape():
    payload = b"\xff\xd8raw-jpeg"
    raw = _frame(
        {
            "type": "observation",
            "obs_id": "s-3",
            "stream": "s",
            "seq": 3,
            "image": {"off": 0, "len": len(payload), "w": 8, "h": 4, "format": "jpeg"},
        },
        [payload],
    )
    msg = _parse_binary_frame(raw)
    # Same dict shape as the JSON path, with raw bytes under image.data (no ref).
    assert msg["type"] == "observation"
    assert (msg["stream"], msg["seq"]) == ("s", 3)
    assert msg["image"]["data"] == payload
    assert "ref" not in msg["image"]
    assert "off" not in msg["image"] and "len" not in msg["image"]
    assert (msg["image"]["w"], msg["image"]["h"]) == (8, 4)
    assert msg["wire_len"] == len(raw)


def test_parse_binary_tiles_frame_slices_each_payload():
    a, b = b"AAAA", b"BB"
    raw = _frame(
        {
            "type": "observation",
            "obs_id": "s-4",
            "stream": "s",
            "seq": 4,
            "tiles": [
                {"x": 0, "y": 0, "w": 64, "h": 64, "off": 0, "len": 4},
                {"x": 64, "y": 0, "w": 36, "h": 64, "off": 4, "len": 2},
            ],
        },
        [a, b],
    )
    msg = _parse_binary_frame(raw)
    tiles = msg["tiles"]
    assert [t["data"] for t in tiles] == [a, b]
    assert tiles[1]["x"] == 64
    assert all("off" not in t and "len" not in t for t in tiles)


@pytest.mark.parametrize(
    "raw",
    [
        b"\x01\x02",  # shorter than the 4-byte length prefix
        (100).to_bytes(4, "little") + b"{}",  # header_len exceeds the frame
        # tile slice pointing outside the payload area
        _frame(
            {
                "type": "observation",
                "obs_id": "o",
                "stream": "s",
                "seq": 0,
                "tiles": [{"x": 0, "y": 0, "w": 1, "h": 1, "off": 0, "len": 99}],
            },
            [b"xy"],
        ),
    ],
)
def test_parse_binary_frame_rejects_malformed(raw):
    with pytest.raises(ValueError):
        _parse_binary_frame(raw)


# ---- end-to-end: negotiated binary path (mock advertises + client offers) ----


def test_binary_screenshot_same_surface_and_wire_savings(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.capabilities.binary_frames is True
        shot = env.screenshot()
        # Identical caller surface: bytes/format/w/h (+ png alias when PNG).
        assert shot["bytes"] == _PNG_1X1
        assert shot["png"] == _PNG_1X1
        assert shot["format"] == "png"
        # The binary frame is smaller than payload*4/3 (no base64 inflation).
        assert shot["wire_len"] is not None
        assert shot["wire_len"] < len(_PNG_1X1) * 4 / 3 + 200


def test_binary_screencast_frames_and_delta_tiles(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with env.screencast(fps=30, limit=3) as frames:
            seqs = [f["seq"] for f in frames]
        assert seqs == [0, 1, 2]
        with env.screencast(fps=30, limit=3, delta=True) as frames:
            got = list(frames)
        assert got[0]["bytes"] == _PNG_1X1  # keyframe
        for f in got[1:]:
            assert f["tiles"][0]["bytes"] == _PNG_1X1
            assert (f["tiles"][0]["w"], f["tiles"][0]["h"]) == (1, 1)


# ---- back-compat fallbacks ----


def test_client_opt_out_keeps_text_frames(mock_shinkend):
    """binary_frames=False pins the legacy base64-in-JSON path against a
    binary-capable server (the A/B measurement switch)."""
    with shinken.connect(mock_shinkend, binary_frames=False) as env:
        assert env.capabilities.binary_frames is True  # server CAN, session didn't ask
        shot = env.screenshot()
        assert shot["bytes"] == _PNG_1X1
        # Text path: wire is the JSON message, so >= base64 of the payload.
        assert shot["wire_len"] >= len(_PNG_1X1) * 4 / 3


def test_pre_binary_server_falls_back_to_text(mock_shinkend_text_only):
    """An old runtime ignores the accept offer; the session sees binary_frames=False
    and every frame still parses (text path)."""
    with shinken.connect(mock_shinkend_text_only) as env:
        assert env.capabilities.binary_frames is False
        shot = env.screenshot()
        assert shot["bytes"] == _PNG_1X1
        with env.screencast(fps=30, limit=2) as frames:
            assert [f["seq"] for f in frames] == [0, 1]


def test_hello_carries_accept_binary_frames(mock_shinkend):
    """The wire hello must carry accept.binary_frames=true (schema-validated by the
    mock: a contract-violating hello would be nacked and fail the handshake)."""
    env = shinken.connect(mock_shinkend)
    env.close()


# ---- frame-budget accounting uses payload bytes ----


def test_frame_resident_bytes_counts_payload_not_base64():
    binary_item = {"type": "observation", "image": {"data": b"x" * 300}}
    text_item = {"type": "observation", "image": {"ref": "A" * 400}}
    tiles_item = {"type": "observation", "tiles": [{"data": b"x" * 10}, {"data": b"y" * 5}]}
    assert _frame_resident_bytes(binary_item) == 300
    assert _frame_resident_bytes(text_item) == 400  # resident = the queued b64 string
    assert _frame_resident_bytes(tiles_item) == 15
    assert _frame_resident_bytes(client_mod._STREAM_END) == 0


def test_budget_eviction_accounts_binary_payload_bytes(mock_shinkend):
    """The per-loop byte budget sees the binary frames' raw payload size: queue
    enough frames to exceed a small budget and confirm eviction keeps `used` under
    it (payload bytes, not base64 length). Driven via asyncio.run per repo
    convention (no pytest-asyncio dependency)."""

    async def go():
        sb = await shinken.aconnect(mock_shinkend)
        try:
            await sb.astart_screencast(fps=30)
            await asyncio.sleep(0.3)  # let several frames queue
            state = sb._loop_budget_state()
            assert state["used"] <= client_mod.GLOBAL_FRAME_BUDGET_BYTES
            frame = await sb.next_frame(timeout=2.0)
            assert frame is not None and frame["bytes"] == _PNG_1X1
        finally:
            await sb.close()

    old = client_mod.GLOBAL_FRAME_BUDGET_BYTES
    client_mod.GLOBAL_FRAME_BUDGET_BYTES = 2 * len(_PNG_1X1)
    try:
        asyncio.run(go())
    finally:
        client_mod.GLOBAL_FRAME_BUDGET_BYTES = old
