"""Kimi-VL computer-use adapter for ACI v0 (#75/#76 family).

Kimi-VL-A3B-Thinking is Moonshot's open-weight GUI agent. Unlike Anthropic's/OpenAI's
structured computer-use *tool calls*, it emits an Aguvis-style **pyautogui text DSL** — one
action per turn, on a ``Toolcall:`` (Kimi-VL) or ``Action: pyautogui.*`` (Aguvis) line —
with **normalized [0, 1] floating-point coordinates** (origin top-left), NOT pixels and NOT
UI-TARS's [0, 1000] integers. This adapter parses that text into a canonical ACI action,
mapping the normalized coordinates onto an ACI ``point_norm`` target (so it needs no screen
size). Fixture-tested; no live API calls.

``terminate``/``answer`` map to the Operator-loop ``done`` control action
(``{"verb": "done"}``, mirroring :data:`shinken.dialect.DONE`) — a control signal, not an
ACI wire verb — carrying ``status="fail"`` for a non-success terminate and an ``answer``
string for QA tasks. ``scroll`` carries no coordinate (Kimi emits none); a dispatcher whose
backend requires a scroll position supplies one (e.g. the current cursor / screen centre).

This adapter is for the **open-weight Kimi-VL / Aguvis-format** path (normalized coords, a
``Toolcall:`` line). The hosted **Kimi K2.6** agentic model is a *different* path: it drives
OSWorld in OSWorld's own **pixel-coordinate pyautogui code-block** format, parsed by
:func:`shinken.osworld.parse_model_actions` and actuated by the ``DesktopEnv`` shim — see
``scripts/osworld_single.py`` (the alpha OSWorld+Kimi runner). Do not use this adapter for K2.6.

Public references: Kimi-VL technical report (arXiv:2504.07491, Fig. 10: ``click(x=0.365,
y=0.317)``, ``scroll(-5)``); Aguvis (arXiv:2412.04454, Table 9 action space).
"""

from __future__ import annotations

import re

from .base import AdapterError, point_norm

#: A signed int or float literal.
_NUM = r"[-+]?(?:\d+\.\d*|\.\d+|\d+)"
#: A single- or double-quoted string literal (non-greedy body).
_STR = r"'([^']*)'|\"([^\"]*)\""
#: A line that contains a call ``name(`` — used to locate the parseable action.
_CALL_LINE = re.compile(r"[A-Za-z_][\w.]*\s*\(")
#: The Operator-loop terminal control action (mirrors :data:`shinken.dialect.DONE`).
_DONE: dict = {"verb": "done"}


class KimiVLAdapter:
    """Stateless translator from Kimi-VL / Aguvis pyautogui-DSL output to ACI v0."""

    name = "kimi-vl"
    model = "moonshotai/Kimi-VL-A3B-Thinking"
    coordinate_space = "point_norm"

    def to_aci_action(self, output: str) -> dict:
        """One Kimi-VL step's text output (or a bare action call) → a canonical ACI action.

        Returns the ``done`` control action for ``terminate``/``answer``. Raises
        :class:`AdapterError` on empty, unparseable, or unsupported output."""
        return self._dispatch(self._action_call(output))

    def run_metadata(self) -> dict:
        """Adapter / model / coordinate-space metadata for callers that need run context."""
        return {
            "adapter": self.name,
            "provider": "moonshot",
            "model": self.model,
            "coordinate_space": self.coordinate_space,
        }

    # --- extraction ----------------------------------------------------------------
    @staticmethod
    def _action_call(output: str) -> str:
        """Pull the single action expression out of a model turn. Prefers an explicit
        ``Toolcall:``/``Action:`` line whose content looks like a call; otherwise the last
        call-like line (Kimi's ``Action:`` is often a natural-language description)."""
        if not isinstance(output, str) or not output.strip():
            raise AdapterError("(empty)", "no model output to parse")
        for label in ("Toolcall:", "Action:"):
            idx = output.lower().rfind(label.lower())
            if idx != -1:
                tail = output[idx + len(label) :].splitlines()
                line = tail[0].strip() if tail else ""
                if line and _CALL_LINE.search(line):
                    return line
        for line in reversed(output.splitlines()):
            if _CALL_LINE.search(line):
                return line.strip()
        raise AdapterError("(missing)", "no action call found in output")

    # --- parsing -------------------------------------------------------------------
    def _dispatch(self, call: str) -> dict:
        m = re.match(r"\s*(?:pyautogui\.)?([A-Za-z_][\w.]*)\s*\((.*)\)\s*;?\s*$", call, re.DOTALL)
        if not m:
            raise AdapterError(call, "not a recognizable action call")
        fn = m.group(1).split(".")[-1]  # strip pyautogui./mobile./browser. namespace
        handler = _HANDLERS.get(fn)
        if handler is None:
            raise AdapterError(fn, "unsupported Kimi/Aguvis action")
        return handler(self, m.group(2).strip())

    # --- per-action handlers -------------------------------------------------------
    def _click(self, arg: str) -> dict:
        return {"verb": "click", "target": _coords(arg)}

    def _double_click(self, arg: str) -> dict:
        return {"verb": "double_click", "target": _coords(arg)}

    def _right_click(self, arg: str) -> dict:
        return {"verb": "right_click", "target": _coords(arg)}

    def _move(self, arg: str) -> dict:
        return {"verb": "move", "target": _coords(arg)}

    def _type(self, arg: str) -> dict:
        return {"verb": "type_text", "text": _one_string(arg, "write")}

    def _press(self, arg: str) -> dict:
        return {"verb": "key", "keys": _one_string(arg, "press").lower()}

    def _hotkey(self, arg: str) -> dict:
        keys = _all_strings(arg)
        if not keys:
            raise AdapterError("hotkey", f"no keys in {arg!r}")
        return {"verb": "key", "keys": "+".join(k.lower() for k in keys)}

    def _scroll(self, arg: str) -> dict:
        m = re.search(_NUM, arg)
        if not m:
            raise AdapterError("scroll", f"no scroll magnitude in {arg!r}")
        # Aguvis/pyautogui: +amount = up; ACI/executor: +dy = down → negate.
        return {"verb": "scroll", "dy": -int(round(float(m.group(0))))}

    def _wait(self, arg: str) -> dict:
        m = re.search(_NUM, arg)
        if m:  # mobile.wait(seconds) → ms
            return {"verb": "wait", "ms": int(round(float(m.group(0)) * 1000))}
        return {"verb": "wait"}

    def _terminate(self, arg: str) -> dict:
        status = (_first_string(arg) or "success").lower()
        done = dict(_DONE)
        if status != "success":
            done["status"] = "fail"
        return done

    def _answer(self, arg: str) -> dict:
        done = dict(_DONE)
        text = _first_string(arg)
        if text is not None:
            done["answer"] = text
        return done


#: Kimi/Aguvis action name → handler. Names are matched after stripping any
#: ``pyautogui.``/``mobile.``/``browser.`` namespace.
_HANDLERS = {
    "click": KimiVLAdapter._click,
    "doubleClick": KimiVLAdapter._double_click,
    "rightClick": KimiVLAdapter._right_click,
    "moveTo": KimiVLAdapter._move,
    "write": KimiVLAdapter._type,
    "typewrite": KimiVLAdapter._type,
    "press": KimiVLAdapter._press,
    "hotkey": KimiVLAdapter._hotkey,
    "scroll": KimiVLAdapter._scroll,
    "vscroll": KimiVLAdapter._scroll,
    "wait": KimiVLAdapter._wait,
    "terminate": KimiVLAdapter._terminate,
    "answer": KimiVLAdapter._answer,
}


def _coords(arg: str) -> dict:
    """Parse ``x=.., y=..`` (kwargs) or the first two positional numbers → ``point_norm``."""
    xm = re.search(r"\bx\s*=\s*(" + _NUM + r")", arg)
    ym = re.search(r"\by\s*=\s*(" + _NUM + r")", arg)
    if xm and ym:
        return point_norm(float(xm.group(1)), float(ym.group(1)))
    nums = re.findall(_NUM, arg)
    if len(nums) >= 2:
        return point_norm(float(nums[0]), float(nums[1]))
    raise AdapterError("coordinate", f"could not parse x, y from {arg!r}")


def _all_strings(arg: str) -> list[str]:
    return [a or b for a, b in re.findall(_STR, arg)]


def _first_string(arg: str) -> str | None:
    found = _all_strings(arg)
    return found[0] if found else None


def _one_string(arg: str, action: str) -> str:
    s = _first_string(arg)
    if s is None:
        raise AdapterError(action, f"missing string argument in {arg!r}")
    return s
