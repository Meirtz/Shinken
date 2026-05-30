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

import os
import re
import tempfile
import time
from typing import Any

from .client import connect
from .skn import Replay

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
        record: bool = False,
    ):
        self.address = address
        self.observation_type = observation_type
        self.action_space = action_space
        self.record = record
        self._env: Any = None
        self._instruction = ""
        self._terminal: str | None = None  # "DONE" / "FAIL" once the agent terminates

    def reset(self, task_config: dict | None = None) -> dict:
        if self._env is None:
            self._env = connect(self.address, record=self.record)
        self._instruction = (task_config or {}).get("instruction", "")
        self._terminal = None
        return self._observation()

    def step(self, action: Any, pause: float = 0.0) -> tuple[dict, float, bool, dict]:
        done = self._dispatch(action)
        if pause and pause > 0:
            time.sleep(pause)  # OSWorld pacing: let the UI settle before observing (#162)
        return self._observation(), 0.0, done, {"terminal": self._terminal}

    def evaluate(self, checker: Any = None) -> dict:
        """Return the terminal evaluator result for the episode. ``passed`` requires a
        ``DONE`` terminal and, if a ``checker(env) -> bool`` is given, that it holds —
        e.g. a verifier over the recorded `.skn` trace. Mirrors OSWorld's final score."""
        ok = self._terminal == "DONE"
        if ok and checker is not None:
            ok = bool(checker(self))
        return {"terminal": self._terminal, "passed": ok, "score": 1.0 if ok else 0.0}

    def save_replay(self, path: str) -> str:
        """Write the session's `.skn` replay (requires ``record=True``)."""
        return self._env.save_replay(path)

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
                self._terminal = s
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
            self._terminal = t
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


def _smoke_checker(bundle: str):
    """Verify the first-party smoke trajectory from its `.skn`: a click, the typed
    text, and an Enter keypress all present in the recorded action stream."""

    def check(_env: Any) -> bool:
        rp = Replay.load(bundle)
        actions = [e for e in rp.events if e["kind"] == "action"]
        clicked = any(a["src"] == "click" for a in actions)
        typed = any(
            a["src"] == "type_text" and "hello shinken" in (a["payload"].get("text") or "")
            for a in actions
        )
        pressed = any(a["src"] == "key" and a["payload"].get("keys") == "enter" for a in actions)
        return clicked and typed and pressed

    return check


def osworld_smoke(address: str = "127.0.0.1:8765", record: bool = True) -> dict:
    """First-party minimal **public-OSWorld-style** smoke (no private assets / harness).

    Drives a short trajectory mixing the two public action spaces — a ``computer_13``
    CLICK, a ``pyautogui`` typewrite, a ``PRESS`` — through :class:`DesktopEnv`,
    terminates with ``DONE``, and evaluates from the recorded `.skn` trace. Returns the
    evaluator result (``terminal``/``passed``/``score``) plus the step count and replay
    bundle path. Run it locally or in CI to prove the OSWorld compatibility path; pass
    means every expected typed action reached the runtime and was recorded.
    """
    env = DesktopEnv(
        address=address, observation_type="screenshot", action_space="pyautogui", record=record
    )
    bundle: str | None = None
    steps = 0
    try:
        env.reset({"instruction": "click a target, type text, then finish"})
        for action in (
            {"action_type": "CLICK", "x": 100, "y": 40},  # computer_13 dict
            "pyautogui.typewrite('hello shinken')",  # pyautogui code string
            {"action_type": "PRESS", "key": "enter"},
            "DONE",
        ):
            _, _, done, _ = env.step(action)
            steps += 1
            if done:
                break
        if record:
            bundle = env.save_replay(
                os.path.join(tempfile.mkdtemp(prefix="osworld-smoke-"), "osworld-smoke.skn")
            )
        result = env.evaluate(checker=_smoke_checker(bundle) if bundle else None)
        result.update(steps=steps, bundle=bundle)
        return result
    finally:
        env.close()
