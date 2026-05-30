"""OSWorld DesktopEnv.step honors the pause parameter (#162)."""

from __future__ import annotations

import time

from shinken.osworld import DesktopEnv


def test_step_honors_pause(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        t0 = time.perf_counter()
        env.step("WAIT", pause=0.25)
        paused = time.perf_counter() - t0
        assert paused >= 0.2  # the pause is applied (with timing tolerance)

        t1 = time.perf_counter()
        env.step("WAIT", pause=0.0)
        assert time.perf_counter() - t1 < 0.2  # no extra delay when pause is 0
    finally:
        env.close()
