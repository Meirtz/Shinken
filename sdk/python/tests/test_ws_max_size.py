"""SDK bounds inbound WebSocket frame size (#136)."""

from __future__ import annotations

import shinken
from shinken.client import _MAX_WS_MESSAGE


def test_max_ws_message_is_a_sane_bound():
    # bounded (not unbounded) yet generous enough for 4K screenshots / large a11y trees
    assert isinstance(_MAX_WS_MESSAGE, int)
    assert 4 * 1024 * 1024 <= _MAX_WS_MESSAGE <= 64 * 1024 * 1024


def test_connect_and_screenshot_under_cap(mock_shinkend):
    # regression: the explicit max_size doesn't break the handshake or normal-size frames
    with shinken.connect(mock_shinkend) as env:
        shot = env.screenshot()
        assert shot["png"][:8] == b"\x89PNG\r\n\x1a\n"
