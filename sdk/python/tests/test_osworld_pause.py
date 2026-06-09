"""OSWorld DesktopEnv.step honors the pause parameter (#162)."""

from __future__ import annotations

from shinken import osworld
from shinken.osworld import DesktopEnv


def test_step_honors_pause(mock_shinkend, monkeypatch):
    # Record time.sleep calls instead of asserting a wall-clock upper bound (which flakes
    # under CI load): step(pause>0) must sleep exactly that long, step(pause=0) must not.
    slept: list[float] = []
    monkeypatch.setattr(osworld.time, "sleep", lambda s: slept.append(s))
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        env.step("WAIT", pause=0.25)
        assert slept == [0.25]
        env.step("WAIT", pause=0.0)
        assert slept == [0.25]  # no additional sleep for pause=0
    finally:
        env.close()
