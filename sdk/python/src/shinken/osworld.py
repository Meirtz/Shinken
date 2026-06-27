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

import ast
import re
import time
from typing import Any

from .client import connect


def _first_str_arg(line: str, fn_pattern: str) -> str | None:
    """Extract the first quoted string argument of a ``fn(...)`` call, quote-aware and
    escape-aware (so ``write("don't")`` yields ``don't``, not ``don``). Handles triple-quoted
    strings (``write(\"\"\"cmd\"\"\")`` — real K2.6 output) BEFORE single/double, since a
    triple quote starts with a single/double quote and a naive matcher would read it as an
    empty string. Returns None if no such call/string is present."""
    call = rf"(?:pyautogui\.)?{fn_pattern}\(\s*"
    # Try triple-quoted first (''' or """), then single/double; both escape-aware.
    for pat in (
        rf"{call}(\"\"\"|''')((?:\\.|(?!\1).)*)\1",
        rf"{call}(['\"])((?:\\.|(?!\1).)*)\1",
    ):
        m = re.search(pat, line, re.DOTALL)
        if m:
            quote, body = m.group(1), m.group(2)
            try:
                return ast.literal_eval(f"{quote}{body}{quote}")
            except (ValueError, SyntaxError):
                return body
    return None


def _split_top_level_semicolons(text: str) -> list[str]:
    """Split on ``;`` only OUTSIDE string literals, so ``write("a;b")`` is not torn apart
    (OSWorld splits compound statements, but must not corrupt semicolons in typed text)."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in text:
        if quote is not None:
            buf.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
        elif ch == ";":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


_NUM = r"(\d+(?:\.\d+)?)"  # int pixels OR a float (normalized [0,1] coords get scaled)
_PYAUTOGUI_PATTERNS = [
    (re.compile(rf"(?:pyautogui\.)?moveTo\(\s*{_NUM}\s*,\s*{_NUM}"), "move"),
    (re.compile(rf"(?:pyautogui\.)?doubleClick\(\s*{_NUM}\s*,\s*{_NUM}"), "double_click"),
    (re.compile(rf"(?:pyautogui\.)?rightClick\(\s*{_NUM}\s*,\s*{_NUM}"), "right_click"),
    (re.compile(rf"(?:pyautogui\.)?click\(\s*{_NUM}\s*,\s*{_NUM}"), "click"),
]


class DesktopEnv:
    """An OSWorld-`DesktopEnv`-shaped wrapper over a Shinken session."""

    def __init__(
        self,
        address: str = "127.0.0.1:8765",
        observation_type: str = "screenshot",
        action_space: str = "pyautogui",
        token: str | None = None,
    ):
        self.address = address
        self.observation_type = observation_type
        self.action_space = action_space
        # Mandatory bearer token for the runtime (including loopback). Runtime injection
        # generates one and returns it alongside the reachable address.
        self.token = token
        self._env: Any = None
        self._instruction = ""
        self._terminal: str | None = None  # "DONE" / "FAIL" once the agent terminates
        self._screen_wh: tuple[int, int] | None = None  # cached for normalized-coord scaling
        # last pointer position — pyautogui dragTo()/computer_13 DRAG_TO drag FROM the current
        # cursor (set by a prior moveTo/click), so we track it to form the ACI drag's source.
        self._last_xy: tuple[int, int] | None = None

    def reset(self, task_config: dict | None = None) -> dict:
        if self._env is None:
            self._env = connect(self.address, token=self.token)
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
            self._last_xy = (a["x"], a["y"])
        elif t == "CLICK":
            self._click(a, double=a.get("num_clicks", 1) >= 2, button=a.get("button", "left"))
        elif t == "RIGHT_CLICK":
            self._click(a, button="right")
        elif t == "DOUBLE_CLICK":
            self._click(a, double=True)
        elif t == "DRAG_TO":
            self._drag_to(a["x"], a["y"])
        elif t == "TYPING":
            self._env.type_text(a.get("text", ""))
        elif t == "PRESS":
            self._env.key(str(a.get("key", "")))
        elif t == "SCROLL":
            self._scroll(dx=a.get("dx", 0), dy=a.get("dy", 0), x=a.get("x"), y=a.get("y"))
        else:
            raise ValueError(f"unsupported computer_13 action_type: {t!r}")
        return False

    def _drag_to(self, x: int, y: int) -> None:
        """Drag from the current cursor (last move/click) to (x, y) — the ACI ``drag`` verb.
        Raises if no prior position is known (a bare drag has no source to anchor)."""
        if self._last_xy is None:
            raise ValueError("DRAG_TO needs a prior MOVE_TO/CLICK to set the drag source")
        sx, sy = self._last_xy
        self._env.drag(x=sx, y=sy, to_x=x, to_y=y)
        self._last_xy = (x, y)

    def _scroll(self, *, dx: int = 0, dy: int = 0, x: int | None = None, y: int | None = None):
        """OSWorld/pyautogui scroll → ACI scroll. OSWorld's wheel counts **clicks** with
        ``+y = up`` / ``+x = right`` (it feeds ``pyautogui.vscroll(dy)`` / ``hscroll(dx)``);
        the ACI wire contract is **pixels** with ``+dy = down`` / ``+dx = right`` (see the
        Anthropic/OpenAI adapters and ``executor.rs``). So the vertical axis is **negated**
        and both axes are converted clicks → pixels at this boundary — same as every other
        click-denominated producer. A position target is forwarded when present."""
        from .adapters.base import SCROLL_PX_PER_CLICK

        # The ACI schema + X11 executor require a scroll target; pyautogui-origin scrolls
        # carry no coordinate, so default to the screen centre (matching the adapter/dialect
        # behavior) instead of emitting a targetless scroll that fails at the wire.
        target = (
            {"kind": "point_px", "x": x, "y": y}
            if x is not None and y is not None
            else {"kind": "point_norm", "x": 0.5, "y": 0.5}
        )
        kwargs: dict[str, Any] = {"dy": -dy * SCROLL_PX_PER_CLICK}
        if dx:
            kwargs["dx"] = dx * SCROLL_PX_PER_CLICK
        self._env.act("scroll", target, **kwargs)

    def _screen(self) -> tuple[int, int]:
        """Guest screen size (cached), for scaling normalized coordinates to pixels."""
        if self._screen_wh is None:
            shot = self._env.screenshot()
            self._screen_wh = (int(shot.get("w") or 1280), int(shot.get("h") or 800))
        return self._screen_wh

    def _to_px(self, gx: str, gy: str) -> tuple[int, int]:
        """Map a matched (x, y) to pixels. Integer args are pixels; float args in [0,1] are
        normalized coordinates (a habit of some models) and get scaled by the screen size."""
        fx, fy = float(gx), float(gy)
        if ("." in gx or "." in gy) and fx <= 1.0 and fy <= 1.0:
            w, h = self._screen()
            return round(fx * w), round(fy * h)
        return round(fx), round(fy)

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
        self._last_xy = (x, y)

    def _pyautogui(self, code: str) -> bool:
        executed = False
        pending_down: list[str] = []
        for raw in code.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "import")):
                continue
            # pyautogui's manual chord idiom — keyDown('ctrl'); keyDown('alt'); keyDown('t');
            # keyUp(...)×N. Collect the held keys; emit ONE ACI chord on the first keyUp
            # (modifiers + final key, e.g. "ctrl+alt+t"); the remaining keyUps are no-ops.
            m = re.search(r"(?:pyautogui\.)?keyDown\(\s*['\"](.*?)['\"]", line)
            if m:
                pending_down.append(m.group(1))
                continue
            if re.search(r"(?:pyautogui\.)?keyUp\(", line):
                if pending_down:
                    self._env.key("+".join(pending_down))
                    pending_down = []
                    executed = True
                continue
            # dragTo FIRST: it substring-contains nothing of the patterns but a moveTo/click
            # pattern must not shadow it; resolve it before the generic pointer loop.
            m = re.search(r"(?:pyautogui\.)?dragTo\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)", line)
            if m:
                tx, ty = self._to_px(m.group(1), m.group(2))
                self._drag_to(tx, ty)
                executed = True
                continue
            for pat, verb in _PYAUTOGUI_PATTERNS:
                m = pat.search(line)
                if m:
                    x, y = self._to_px(m.group(1), m.group(2))
                    getattr(self._env, verb)(x=x, y=y)
                    self._last_xy = (x, y)
                    executed = True
                    break
            else:
                s = _first_str_arg(line, "(?:write|typewrite)")
                if s is not None:
                    self._env.type_text(s)
                    executed = True
                    continue
                s = _first_str_arg(line, "press")
                if s is not None:
                    self._env.key(s)
                    executed = True
                    continue
                m = re.search(r"(?:pyautogui\.)?hotkey\(([^)]*)\)", line)
                if m:
                    keys = [k.strip().strip("'\"") for k in m.group(1).split(",") if k.strip()]
                    self._env.key("+".join(keys))
                    executed = True
                    continue
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
        if pending_down:  # a keyDown run with no matching keyUp — still emit the chord
            self._env.key("+".join(pending_down))
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
    empty list if nothing parseable is found (a stuck/garbled turn, or a non-string/None
    model response)."""
    if not isinstance(text, str):
        return []
    text = "\n".join(s.strip() for s in _split_top_level_semicolons(text) if s.strip())
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
