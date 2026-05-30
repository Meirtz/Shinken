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


def test_target_builder():
    assert _target(None, 5, 6) == {"kind": "point_px", "x": 5, "y": 6}
    assert _target("e1", None, None) == {"kind": "element_ref", "ref": "e1"}
    assert _target({"kind": "point_norm", "x": 0.5, "y": 0.5}, None, None)["kind"] == "point_norm"
    assert _target(None, None, None) is None
    with pytest.raises(TypeError):
        _target(object(), None, None)
