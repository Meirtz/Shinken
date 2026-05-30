"""M0 acceptance: the SDK connects, handshakes, and answers ping/query."""

from __future__ import annotations

import shinken


def test_connect_handshake_and_queries(mock_shinkend):
    env = shinken.connect(mock_shinkend)
    try:
        assert env.platform == "linux"
        caps = env.capabilities
        assert caps.schema_version == 0
        assert "click" in caps.verbs
        assert "element_ref" in caps.targets
        assert env.screen_size() == {"w": 1280, "h": 800}
        assert env.ping() >= 0.0
    finally:
        env.close()


def test_context_manager(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        assert env.platform == "linux"
