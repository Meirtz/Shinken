"""OSWorld `DesktopEnv` compatibility shim.

Lets existing OSWorld tasks/agents drive Shinken unchanged: a gym-like
``reset()/step()`` env backed by the Shinken SDK, translating OSWorld's two action
spaces — ``computer_13`` dicts and ``pyautogui`` code strings — onto the typed ACI.
This is how Shinken subsumes OSWorld's runtime + message-passing (docs/03, docs/11).

    from shinken.osworld import DesktopEnv

    env = DesktopEnv(address="127.0.0.1:8765", observation_type="screenshot")
    obs = env.reset({"instruction": "Open the file menu"})
    obs, reward, done, info = env.step({"action_type": "CLICK", "x": 100, "y": 40})
    env.close()

Observation parity: ``screenshot`` is live; the ``a11y_tree`` channel is wired but
returns ``None`` until the AT-SPI observation engine lands (M1b). reward/done are
left to the eval layer (M4) — ``DONE``/``FAIL`` sentinels set ``done``.
"""

from __future__ import annotations

import re
from typing import Any

from .client import connect

_PYAUTOGUI_PATTERNS = [
    (re.compile(r"(?:pyautogui\.)?moveTo\(\s*(\d+)\s*,\s*(\d+)"), "move"),
    (re.compile(r"(?:pyautogui\.)?doubleClick\(\s*(\d+)\s*,\s*(\d+)"), "double_click"),
    (re.compile(r"(?:pyautogui\.)?rightClick\(\s*(\d+)\s*,\s*(\d+)"), "right_click"),
    (re.compile(r"(?:pyautogui\.)?click\(\s*(\d+)\s*,\s*(\d+)"), "click"),
]


class DesktopEnv:
    """An OSWorld-`DesktopEnv`-shaped wrapper over a Shinken session."""

    def __init__(
        self,
        address: str = "127.0.0.1:8765",
        observation_type: str = "screenshot",
        action_space: str = "pyautogui",
    ):
        self.address = address
        self.observation_type = observation_type
        self.action_space = action_space
        self._env: Any = None
        self._instruction = ""

    def reset(self, task_config: dict | None = None) -> dict:
        if self._env is None:
            self._env = connect(self.address)
        self._instruction = (task_config or {}).get("instruction", "")
        return self._observation()

    def step(self, action: Any, pause: float = 0.0) -> tuple[dict, float, bool, dict]:
        done = self._dispatch(action)
        return self._observation(), 0.0, done, {}

    def close(self) -> None:
        if self._env is not None:
            self._env.close()
            self._env = None

    # --- observation ---

    def _observation(self) -> dict:
        obs: dict[str, Any] = {"instruction": self._instruction}
        if "screenshot" in self.observation_type or self.observation_type == "som":
            obs["screenshot"] = self._env.screenshot()["png"]
        if "a11y_tree" in self.observation_type:
            obs["accessibility_tree"] = None  # AT-SPI engine: M1b
        return obs

    # --- action translation ---

    def _dispatch(self, action: Any) -> bool:
        if isinstance(action, str):
            s = action.strip()
            if s in ("DONE", "FAIL"):
                return True
            if s == "WAIT":
                self._env.act("wait")
                return False
            return self._pyautogui(s)
        if isinstance(action, dict):
            return self._computer13(action)
        raise TypeError(f"unsupported action: {action!r}")

    def _computer13(self, a: dict) -> bool:
        t = str(a.get("action_type", "")).upper()
        if t in ("DONE", "FAIL"):
            return True
        if t == "WAIT":
            self._env.act("wait")
            return False
        if t == "MOVE_TO":
            self._env.move(x=a["x"], y=a["y"])
        elif t == "CLICK":
            self._click(a, double=a.get("num_clicks", 1) >= 2, button=a.get("button", "left"))
        elif t == "RIGHT_CLICK":
            self._click(a, button="right")
        elif t == "DOUBLE_CLICK":
            self._click(a, double=True)
        elif t == "TYPING":
            self._env.type_text(a.get("text", ""))
        elif t == "PRESS":
            self._env.key(str(a.get("key", "")))
        elif t == "SCROLL":
            self._env.scroll(dy=a.get("dy", 0))
        else:
            raise ValueError(f"unsupported computer_13 action_type: {t!r}")
        return False

    def _click(self, a: dict, *, double: bool = False, button: str = "left") -> None:
        x, y = a.get("x"), a.get("y")
        if x is None or y is None:
            raise ValueError("CLICK requires x,y in P0 (targetless click is not yet supported)")
        if button == "right":
            self._env.right_click(x=x, y=y)
        elif double:
            self._env.double_click(x=x, y=y)
        else:
            self._env.click(x=x, y=y)

    def _pyautogui(self, code: str) -> bool:
        executed = False
        for raw in code.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "import")):
                continue
            for pat, verb in _PYAUTOGUI_PATTERNS:
                m = pat.search(line)
                if m:
                    getattr(self._env, verb)(x=int(m.group(1)), y=int(m.group(2)))
                    executed = True
                    break
            else:
                m = re.search(r"(?:pyautogui\.)?(?:write|typewrite)\(\s*['\"](.*?)['\"]", line)
                if m:
                    self._env.type_text(m.group(1))
                    executed = True
                    continue
                m = re.search(r"(?:pyautogui\.)?press\(\s*['\"](.*?)['\"]", line)
                if m:
                    self._env.key(m.group(1))
                    executed = True
                    continue
                m = re.search(r"(?:pyautogui\.)?hotkey\(([^)]*)\)", line)
                if m:
                    keys = [k.strip().strip("'\"") for k in m.group(1).split(",") if k.strip()]
                    self._env.key("+".join(keys))
                    executed = True
                    continue
                m = re.search(r"(?:pyautogui\.)?scroll\(\s*(-?\d+)", line)
                if m:
                    self._env.scroll(dy=-int(m.group(1)))
                    executed = True
        if not executed:
            raise ValueError(f"no supported pyautogui call found in: {code!r}")
        return False
