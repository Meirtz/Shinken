"""OSWorld DesktopEnv compat shim: reset/step over the Shinken SDK."""

from __future__ import annotations

import pytest

from shinken.osworld import DesktopEnv, osworld_smoke


def test_reset_returns_instruction_and_screenshot(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend, observation_type="screenshot")
    try:
        obs = env.reset({"instruction": "Open the file menu"})
        assert obs["instruction"] == "Open the file menu"
        assert obs["screenshot"][:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        env.close()


def test_computer13_actions_and_done(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        obs, reward, done, info = env.step({"action_type": "CLICK", "x": 100, "y": 40})
        assert reward == 0.0 and done is False and "screenshot" in obs
        assert info == {"terminal": None}  # not terminated yet
        assert env.step({"action_type": "TYPING", "text": "hi"})[2] is False
        assert env.step({"action_type": "PRESS", "key": "enter"})[2] is False
        assert env.step({"action_type": "DONE"})[2] is True
    finally:
        env.close()


def test_pyautogui_action_translation(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend, action_space="pyautogui")
    try:
        env.reset()
        assert env.step("pyautogui.click(100, 200)")[2] is False
        assert env.step("pyautogui.hotkey('ctrl', 's')")[2] is False
        assert env.step("pyautogui.write('hello')")[2] is False
        assert env.step("WAIT")[2] is False
        assert env.step("DONE")[2] is True
    finally:
        env.close()


def test_unsupported_action_raises(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        with pytest.raises(ValueError):
            env.step({"action_type": "CLICK"})  # missing x,y
        with pytest.raises(ValueError):
            env.step("totally.not.an.action()")
    finally:
        env.close()


def test_osworld_smoke_passes(mock_shinkend):
    from pathlib import Path

    result = osworld_smoke(address=mock_shinkend, record=True)
    assert result["terminal"] == "DONE"
    assert result["passed"] is True and result["score"] == 1.0
    assert result["steps"] == 4  # CLICK, typewrite, PRESS, DONE
    assert result["bundle"] and Path(result["bundle"]).exists()


def test_evaluate_done_with_checker(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset({"instruction": "x"})
        _, _, done, info = env.step("DONE")
        assert done and info["terminal"] == "DONE"
        assert env.evaluate()["passed"] is True  # DONE, no checker → pass
        assert env.evaluate(checker=lambda e: False)["passed"] is False  # checker can fail it
    finally:
        env.close()


def test_evaluate_fail_terminal_scores_zero(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        _, _, done, info = env.step({"action_type": "FAIL"})
        assert done and info["terminal"] == "FAIL"
        res = env.evaluate()
        assert res["passed"] is False and res["score"] == 0.0
    finally:
        env.close()
