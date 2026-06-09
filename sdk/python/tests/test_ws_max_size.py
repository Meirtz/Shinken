"""SDK bounds inbound WebSocket frame size (#136)."""

from __future__ import annotations

import shinken
from shinken import client as client_mod
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


def test_max_size_is_actually_passed_to_the_websocket(mock_shinkend, monkeypatch):
    # The #136 cap is only real if it reaches the websocket client. Assert the wiring
    # (this test fails if `max_size=_MAX_WS_MESSAGE` is dropped from the connect call),
    # which the prior tests did not exercise.
    seen: dict = {}
    real_connect = client_mod._ws_connect

    def spy(uri, **kwargs):
        seen.update(kwargs)
        return real_connect(uri, **kwargs)

    monkeypatch.setattr(client_mod, "_ws_connect", spy)
    with shinken.connect(mock_shinkend) as env:
        env.ping()
    assert seen.get("max_size") == _MAX_WS_MESSAGE
