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
returns ``None`` until the AT-SPI observation engine lands (M1b). reward/done are left to
the eval layer (M4): the ``DONE``/``FAIL`` sentinels set ``done``, and ``evaluate(checker)``
scores the episode — a bare ``DONE`` is the agent's *claim* of success, never a pass on its
own. This shim has no built-in metric/getter pipeline; real task scoring is the official
OSWorld evaluator (see ``scripts/osworld_single.py``).
"""

from __future__ import annotations

import re
import time
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
        self._terminal: str | None = None  # "DONE" / "FAIL" once the agent terminates

    def reset(self, task_config: dict | None = None) -> dict:
        if self._env is None:
            self._env = connect(self.address)
        self._instruction = (task_config or {}).get("instruction", "")
        self._terminal = None
        return self._observation()

    def step(self, action: Any, pause: float = 0.0) -> tuple[dict, float, bool, dict]:
        done = self._dispatch(action)
        if pause and pause > 0:
            time.sleep(pause)  # OSWorld pacing: let the UI settle before observing (#162)
        return self._observation(), 0.0, done, {"terminal": self._terminal}

    def evaluate(self, checker: Any = None) -> dict:
        """Score the episode. Unlike OSWorld's metric/getter pipeline (which this shim does
        not implement), a real verdict REQUIRES a ``checker(env) -> bool`` — the task's
        evaluator. Semantics, matching OSWorld rather than inverting it:

        * a ``FAIL`` terminal always scores 0;
        * with no ``checker`` the episode is **unverified** (``evaluated=False``, not a
          pass) — a bare ``DONE`` is the agent's claim, never a pass on its own;
        * with a ``checker``, ``passed`` is ``DONE and checker(self)``.

        For genuine OSWorld task scoring use the official evaluator
        (``scripts/osworld_single.py``)."""
        if self._terminal == "FAIL":
            return {"terminal": "FAIL", "passed": False, "score": 0.0, "evaluated": True}
        if checker is None:
            return {"terminal": self._terminal, "passed": False, "score": 0.0, "evaluated": False}
        ok = self._terminal == "DONE" and bool(checker(self))
        return {
            "terminal": self._terminal,
            "passed": ok,
            "score": 1.0 if ok else 0.0,
            "evaluated": True,
        }

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
            self._scroll(dx=a.get("dx", 0), dy=a.get("dy", 0), x=a.get("x"), y=a.get("y"))
        else:
            raise ValueError(f"unsupported computer_13 action_type: {t!r}")
        return False

    def _scroll(self, *, dx: int = 0, dy: int = 0, x: int | None = None, y: int | None = None):
        """OSWorld/pyautogui scroll → ACI scroll. OSWorld's wheel uses ``+y = up`` /
        ``+x = right`` (it feeds ``pyautogui.vscroll(dy)`` / ``hscroll(dx)``); the ACI and
        shinkend convention is ``+dy = down`` / ``+dx = right`` (see the Anthropic/OpenAI
        adapters and ``executor.rs``). So the vertical axis is **negated** and the
        horizontal axis passes through — this is the bug the two action spaces previously
        disagreed on. ``dx`` is forwarded for wire fidelity even though the current X11
        backend actuates only ``dy``; a position target is forwarded when present."""
        target = {"kind": "point_px", "x": x, "y": y} if x is not None and y is not None else None
        kwargs: dict[str, Any] = {"dy": -dy}
        if dx:
            kwargs["dx"] = dx
        self._env.act("scroll", target, **kwargs)

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
                if re.search(r"(?:pyautogui\.)?dragTo\(", line):
                    raise ValueError("pyautogui.dragTo is unsupported by ACI v0 (no drag verb)")
                m = re.search(r"(?:pyautogui\.)?hscroll\(\s*(-?\d+)", line)
                if m:  # horizontal wheel — check before scroll() (which substring-matches)
                    self._scroll(dx=int(m.group(1)))
                    executed = True
                    continue
                m = re.search(r"(?:pyautogui\.)?vscroll\(\s*(-?\d+)", line)
                if m:
                    self._scroll(dy=int(m.group(1)))
                    executed = True
                    continue
                m = re.search(r"(?:pyautogui\.)?scroll\(\s*(-?\d+)", line)
                if m:
                    self._scroll(dy=int(m.group(1)))
                    executed = True
        if not executed:
            raise ValueError(f"no supported pyautogui call found in: {code!r}")
        return False


#: OSWorld's terminal control tokens (agent signals, not pyautogui).
_TERMINAL_TOKENS = ("WAIT", "DONE", "FAIL")


def parse_model_actions(text: str) -> list[str]:
    """Extract OSWorld-format actions from a **chat model's** raw response — the format
    OSWorld's own ``PromptAgent`` prompts for, and the one a hosted agentic model
    (e.g. Kimi **K2.6**) emits when driving OSWorld: fenced ```python ...``` pyautogui code
    blocks (pixel coordinates) plus the bare ``WAIT``/``DONE``/``FAIL`` control tokens.

    Mirrors OSWorld's ``mm_agents.agent.parse_code_from_string`` so the *same* model output
    drives Shinken unchanged; each returned string is directly consumable by
    :meth:`DesktopEnv.step` (which routes pyautogui code through the typed ACI). Returns an
    empty list if nothing parseable is found (a stuck/garbled turn)."""
    text = "\n".join(s.strip() for s in text.split(";") if s.strip())
    if text.strip() in _TERMINAL_TOKENS:
        return [text.strip()]
    actions: list[str] = []
    for block in re.findall(r"```(?:\w+\s+)?(.*?)```", text, re.DOTALL):
        block = block.strip()
        if block in _TERMINAL_TOKENS:
            actions.append(block)
        elif block.split("\n")[-1] in _TERMINAL_TOKENS:  # trailing DONE after code
            lines = block.split("\n")
            if len(lines) > 1:
                actions.append("\n".join(lines[:-1]))
            actions.append(lines[-1])
        elif block:
            actions.append(block)
    return actions


def osworld_smoke(address: str = "127.0.0.1:8765") -> dict:
    """First-party minimal **public-OSWorld-style** plumbing smoke (no private assets /
    harness).

    Drives a short trajectory mixing the two public action spaces — a ``computer_13``
    CLICK, a ``pyautogui`` typewrite, a ``PRESS`` — through :class:`DesktopEnv` and
    terminates with ``DONE``. It proves the *action plumbing* (both spaces dispatch and the
    terminal is reached), not task success: it returns ``{"terminal", "steps"}`` and
    deliberately does **not** fabricate a pass/score, since no task evaluator is wired here.
    Real scoring is the official OSWorld evaluator (``scripts/osworld_single.py``).
    """
    env = DesktopEnv(address=address, observation_type="screenshot", action_space="pyautogui")
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
        return {"terminal": env._terminal, "steps": steps}
    finally:
        env.close()
