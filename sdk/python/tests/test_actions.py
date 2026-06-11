"""M1a: the SDK sends typed actions and the runtime acks them."""

from __future__ import annotations

import pytest

import shinken
from shinken.client import _target


def test_click_move_scroll_acked(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.click(x=300, y=200)["ok"] is True
        assert env.move(x=10, y=10)["ok"] is True
        assert env.scroll(x=10, y=10, dy=-300)["ok"] is True
        assert env.act("double_click", {"kind": "point_px", "x": 1, "y": 2})["ok"] is True


def test_type_text_and_key_acked(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.type_text("hello shinken")["ok"] is True
        assert env.key("ctrl+a")["ok"] is True


def test_screenshot_returns_png(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        shot = env.screenshot()
        assert shot["png"][:8] == b"\x89PNG\r\n\x1a\n"
        assert shot["w"] == 1 and shot["h"] == 1
        # Default codec is the lossless PNG; `bytes` aliases `png`.
        assert shot["format"] == "png"
        assert shot["bytes"] == shot["png"]


def test_screenshot_format_and_quality_travel_the_wire(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        shot = env.screenshot(format="jpeg", quality=70)
        # The mock echoes the requested codec, proving `format`/`quality` reached shinkend.
        assert shot["format"] == "jpeg"
        assert "bytes" in shot
        # The 'png' back-compat alias must be ABSENT for non-PNG codecs: legacy readers
        # fail loudly instead of mislabeling JPEG bytes as PNG.
        assert "png" not in shot


def test_screenshot_max_long_edge_travels_the_wire(mock_shinkend):
    """`max_long_edge` is the same downscale lever the screencast and the pipelined
    step() observe dict expose; the on-demand screenshot must carry it too. Asserted
    from the mock's RECORDED wire action, not from the request's own inputs."""
    with shinken.connect(mock_shinkend) as env:
        env.screenshot(format="jpeg", quality=50, max_long_edge=768)
        sent = env.query("state")["screenshots"][-1]
        assert sent["format"] == "jpeg"
        assert sent["quality"] == 50
        assert sent["max_long_edge"] == 768
        # omitted levers stay off the wire (a pre-downscale runtime never sees them)
        env.screenshot()
        assert env.query("state")["screenshots"][-1]["max_long_edge"] is None


def test_target_builder():
    assert _target(None, 5, 6) == {"kind": "point_px", "x": 5, "y": 6}
    assert _target("e1", None, None) == {"kind": "element_ref", "ref": "e1"}
    assert _target({"kind": "point_norm", "x": 0.5, "y": 0.5}, None, None)["kind"] == "point_norm"
    assert _target(None, None, None) is None
    with pytest.raises(TypeError):
        _target(object(), None, None)


# ---- drag / mouse_down / mouse_up (coordinate-tier gesture verbs) ----


def test_drag_travels_the_wire_with_to_button_and_duration(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        ack = env.drag(x=10, y=20, to_x=300, to_y=200, duration_ms=250, button="left")
        assert ack["ok"] is True
        # The mock records the observed wire shape — both endpoints + the levers.
        drags = env.query("state")["drags"]
        assert drags == [
            {
                "from": {"x": 10, "y": 20},
                "to": {"x": 300, "y": 200},
                "duration_ms": 250,
                "button": "left",
            }
        ]


def test_drag_requires_both_endpoints(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(ValueError, match="source"):
            env.drag(to_x=3, to_y=4)
        with pytest.raises(ValueError, match="destination"):
            env.drag(x=1, y=2)


def test_mouse_down_up_decomposed_gesture(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.mouse_down(x=100, y=150)["ok"] is True
        assert env.move(x=200, y=250)["ok"] is True
        assert env.mouse_up(button="right")["ok"] is True  # release needs no target
        buttons = env.query("state")["buttons"]
        assert buttons == [
            {"verb": "mouse_down", "button": None, "x": 100, "y": 150},
            {"verb": "mouse_up", "button": "right", "x": None, "y": None},
        ]


# ---- act-returns-observation (`observe`) ----


def test_click_with_observe_returns_the_observation_dict(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        obs = env.click(x=1, y=2, observe=True)
        # the observation dict has the screenshot shape, not the ack shape
        assert obs["png"][:8] == b"\x89PNG\r\n\x1a\n"
        assert obs["bytes"] == obs["png"]
        assert obs["format"] == "png"
        assert (obs["w"], obs["h"]) == (1, 1)
        # ...and the click itself landed (the mock recorded it before the follow-up)
        assert env.query("state")["clicks"] == [
            {"verb": "click", "kind": "point_px", "x": 1, "y": 2}
        ]


def test_observe_capture_levers_travel_the_wire(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        obs = env.type_text("hi", observe={"format": "jpeg", "quality": 70})
        # the mock echoes the requested codec, proving the observe params reached it
        assert obs["format"] == "jpeg"
        assert "png" not in obs  # the alias must be absent for non-PNG codecs
        # a plain ack path is unchanged when observe is not requested
        assert env.key("ctrl+s")["ok"] is True


def test_observe_works_on_the_text_frame_path(mock_shinkend_text_only):
    with shinken.connect(mock_shinkend_text_only) as env:
        obs = env.move(x=5, y=6, observe=True)
        assert obs["png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_observe_rejects_unknown_keys_before_the_wire(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(ValueError, match="unknown observe keys"):
            env.click(x=1, y=2, observe={"fps": 30})
        with pytest.raises(ValueError, match="not advertised"):
            env.click(x=1, y=2, observe={"format": "webp"})


def test_observe_requires_the_runtime_capability(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        # simulate an old runtime: welcome without observe_after_act
        env._inner.capabilities.observe_after_act = False
        with pytest.raises(ValueError, match="observe-after-act not advertised"):
            env.click(x=1, y=2, observe=True)
        # old runtimes are otherwise unaffected: the plain ack path still works
        assert env.click(x=1, y=2)["ok"] is True


# ---- list_windows (EWMH window enumeration) ----


def test_list_windows_returns_the_window_entries(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        windows = env.list_windows()
        assert [w["title"] for w in windows] == ["xterm", "xclock"]
        focused = [w for w in windows if w["focused"]]
        assert len(focused) == 1 and focused[0]["id"] == 0x1A
        # every entry carries the full shape, id usable as a window:<id> scope
        for w in windows:
            assert set(w) == {"id", "title", "pid", "x", "y", "w", "h", "focused"}
