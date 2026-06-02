"""OSWorld DesktopEnv compat shim: reset/step over the Shinken SDK."""

from __future__ import annotations

import pytest

from shinken.osworld import DesktopEnv, osworld_smoke, parse_model_actions


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


def test_osworld_smoke_reaches_done(mock_shinkend):
    # Plumbing smoke: both action spaces dispatch and the terminal is reached. It does NOT
    # fabricate a pass/score (no task evaluator is wired) — real scoring is the official
    # OSWorld evaluator path.
    result = osworld_smoke(address=mock_shinkend)
    assert result["terminal"] == "DONE"
    assert result["steps"] == 4  # CLICK, typewrite, PRESS, DONE
    assert "passed" not in result and "score" not in result


def test_evaluate_done_with_checker(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset({"instruction": "x"})
        _, _, done, info = env.step("DONE")
        assert done and info["terminal"] == "DONE"
        # A bare DONE is NOT a pass on its own — unverified without a task evaluator.
        res = env.evaluate()
        assert res["passed"] is False and res["evaluated"] is False
        # With a checker (the task evaluator), DONE + checker decides the verdict.
        assert env.evaluate(checker=lambda e: True)["passed"] is True
        assert env.evaluate(checker=lambda e: False)["passed"] is False
    finally:
        env.close()


def test_evaluate_fail_terminal_scores_zero(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        _, _, done, info = env.step({"action_type": "FAIL"})
        assert done and info["terminal"] == "FAIL"
        res = env.evaluate()
        assert res["passed"] is False and res["score"] == 0.0 and res["evaluated"] is True
        # FAIL is terminal-fail even if a checker would pass.
        assert env.evaluate(checker=lambda e: True)["passed"] is False
    finally:
        env.close()


def test_scroll_sign_and_dx_consistent_across_action_spaces(mock_shinkend):
    # OSWorld/pyautogui use +y=up / +x=right; ACI uses +dy=down / +dx=right. So vertical is
    # negated and horizontal passes through — and both action spaces must agree (the bug fix).
    env = DesktopEnv(address=mock_shinkend)
    try:
        env.reset()
        env.step({"action_type": "SCROLL", "dy": 5, "dx": 2})  # computer_13
        env.step("pyautogui.scroll(5)")  # vertical, +5 = up → ACI dy = -5
        env.step("pyautogui.hscroll(3)")  # horizontal, +3 = right → ACI dx = +3
        scrolls = env._env.query("state")["scrolls"]
        assert scrolls[0] == {"dx": 2, "dy": -5}  # computer_13: dy negated, dx forwarded
        assert scrolls[1] == {"dx": None, "dy": -5}  # pyautogui scroll matches computer_13 sign
        assert scrolls[2] == {"dx": 3, "dy": 0}  # hscroll → dx, not caught by scroll() regex
    finally:
        env.close()


def test_parse_model_actions_matches_osworld_format():
    # a fenced pyautogui code block (the format OSWorld prompts a chat model like K2.6 for)
    assert parse_model_actions("Reflection.\n```python\npyautogui.click(960, 540)\n```") == [
        "pyautogui.click(960, 540)"
    ]
    # control tokens, fenced or bare
    assert parse_model_actions("```DONE```") == ["DONE"]
    assert parse_model_actions("DONE") == ["DONE"]
    # multi-statement split on ';' (OSWorld normalizes ';' to newlines)
    assert parse_model_actions("```python\npyautogui.click(1, 2); pyautogui.write('hi')\n```") == [
        "pyautogui.click(1, 2)\npyautogui.write('hi')"
    ]
    # code then a trailing DONE on its own line → two actions
    assert parse_model_actions("```python\npyautogui.click(1, 2)\nDONE\n```") == [
        "pyautogui.click(1, 2)",
        "DONE",
    ]
    # prose with no code/token → nothing parseable (a stuck turn)
    assert parse_model_actions("I am not sure what to do here.") == []


def test_parsed_pyautogui_drives_the_shim(mock_shinkend):
    # End-to-end: a K2.6-style response → parse_model_actions → the shim actuates each.
    env = DesktopEnv(address=mock_shinkend, action_space="pyautogui")
    try:
        env.reset()
        actions = parse_model_actions("```python\npyautogui.click(300, 200)\n```")
        for a in actions:
            env.step(a)
        clicks = env._env.query("state")["clicks"]
        assert clicks and clicks[-1]["x"] == 300 and clicks[-1]["y"] == 200
    finally:
        env.close()


def test_pyautogui_dragto_is_explicitly_unsupported(mock_shinkend):
    env = DesktopEnv(address=mock_shinkend, action_space="pyautogui")
    try:
        env.reset()
        with pytest.raises(ValueError, match="dragTo"):
            env.step("pyautogui.dragTo(10, 20)")
    finally:
        env.close()


def test_pyautogui_keydown_keyup_becomes_one_chord(mock_shinkend):
    # pyautogui's manual chord idiom (keyDown ×N then keyUp) → one ACI key chord.
    env = DesktopEnv(address=mock_shinkend, action_space="pyautogui")
    try:
        env.reset()
        env.step(
            "pyautogui.keyDown('ctrl')\npyautogui.keyDown('alt')\npyautogui.keyDown('t')\n"
            "pyautogui.keyUp('t')\npyautogui.keyUp('alt')\npyautogui.keyUp('ctrl')"
        )
        assert env._env.query("state")["keys"][-1] == "ctrl+alt+t"
    finally:
        env.close()


def test_pyautogui_normalized_coords_scale_to_pixels(mock_shinkend):
    # Float coords in [0,1] are normalized → scaled by the screen; integer coords are pixels.
    env = DesktopEnv(address=mock_shinkend, action_space="pyautogui")
    try:
        env.reset()
        env._screen_wh = (1280, 800)  # deterministic screen for the test
        env.step("pyautogui.click(0.1, 0.1)")  # normalized → (128, 80)
        env.step("pyautogui.click(300, 400)")  # pixels pass through
        clicks = env._env.query("state")["clicks"]
        assert (clicks[-2]["x"], clicks[-2]["y"]) == (128, 80)
        assert (clicks[-1]["x"], clicks[-1]["y"]) == (300, 400)
    finally:
        env.close()
