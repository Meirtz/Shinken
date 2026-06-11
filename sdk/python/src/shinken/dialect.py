"""Agent-native action dialect parser (#74).

Models emit a constrained, human-readable **action dialect**; this module parses that
dialect into canonical ACI typed actions (the same dicts ``Sandbox.act_batch`` accepts).
The Phase-0 dialect is the XML-like tag form documented in ``docs/design/aci-spec.md``::

    <actions>
      <click x="640" y="420"/>
      <type_text text="hello world"/>
      <key combo="ctrl+s"/>
      <scroll x="900" y="600" dy="-480"/>
      <wait ms="500"/>
      <done/>
    </actions>

The parser is **pure and side-effect-free**: it does no I/O and never evaluates code (it
is a small hand-written tag scanner, so there is no XML entity/DTD expansion to worry
about). It is the adapter boundary's reference grammar — vendor adapters may use their own
function-call/JSON grammars, but must normalize into the same canonical ACI actions.

**String-form XML tool calls are first-class input.** Many computer-use models emit their
tool calls as *text*, not structured ``tool_use`` JSON. :func:`parse_xml_actions` (and
``parse_actions(text, format="auto"|"xml")``) parses the wild-type grammars into the same
canonical ACI actions:

1. **JSON-in-XML** (Qwen / Hermes style)::

       <tool_call>
       {"name": "computer_use", "arguments": {"action": "left_click", "coordinate": [135, 742]}}
       </tool_call>

2. **invoke/parameter blocks** (Anthropic text-form function calls)::

       <invoke name="computer">
       <parameter name="action">left_click</parameter>
       <parameter name="coordinate">[640, 400]</parameter>
       </invoke>

3. **function/parameter element XML** (Seed / UI-TARS-2, qwen3.5-4b)::

       <tool_call><function=click>
       <parameter=point><point>400 300</point></parameter>
       </function></tool_call>

4. **attribute / element XML**::

       <left_click x="100" y="200"/>
       <action name="click"><param name="x">100</param><param name="y">200</param></action>

Parsing is *tolerant* (markdown fences, namespace prefixes, unclosed tags, unquoted
attributes, trailing-comma / truncated JSON) but **never silently drops**: an unknown verb,
an unsupported action (drag, middle-click, …), or an action-shaped tag the parser cannot
map raises a typed :class:`DialectError` carrying the offending snippet. Multiple calls in
one message yield an **ordered** action list. Argument normalization: pixel coordinates are
coerced to ints (``coordinate: [x, y]``, ``x``/``y``, ``point`` "x y", ``start_box``
"(x1,y1,x2,y2)" centre); fractional ``[0, 1]`` float pairs become ``point_norm``; key
chords are joined/lowercased (``["ctrl", "s"]`` → ``"ctrl+s"``); scroll magnitudes convert
per source semantics (pyautogui ``pixels`` sign-flips, OpenAI ``scroll_x/scroll_y`` pass
through, ``direction``+``amount`` wheel clicks × 100 px). Plain-text DSLs are NOT handled
here — Kimi-VL/Aguvis pyautogui lives in :mod:`shinken.adapters.kimi`, OSWorld code blocks
in :func:`shinken.osworld.parse_model_actions`.

Malformed output raises :class:`DialectError` with a teaching message, so an Operator
loop can hand the error back to the model rather than execute unsupported output.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

__all__ = ["DialectError", "parse_actions", "parse_xml_actions", "looks_like_xml", "DONE"]

#: Canonical terminal action — emitted for ``<done/>`` / ``<stop/>``. It is a control
#: signal for the Operator loop, not an ACI wire verb.
DONE = {"verb": "done"}


class DialectError(ValueError):
    """A model's dialect output was malformed or unsupported. The message is a teaching
    error intended to be returned to the agent loop, not executed."""


# Per-verb spec: required attributes and the full set of allowed attributes. Coordinate
# verbs accept px (``x``/``y``) or normalized (``nx``/``ny``) targets, validated below.
_POINTING = {"click", "double_click", "right_click", "move"}
_SPEC: dict[str, dict[str, set[str]]] = {
    "click": {"required": set(), "allowed": {"x", "y", "nx", "ny", "button"}},
    "double_click": {"required": set(), "allowed": {"x", "y", "nx", "ny", "button"}},
    "right_click": {"required": set(), "allowed": {"x", "y", "nx", "ny", "button"}},
    "move": {"required": set(), "allowed": {"x", "y", "nx", "ny"}},
    "scroll": {"required": {"dy"}, "allowed": {"x", "y", "nx", "ny", "dx", "dy"}},
    "type_text": {"required": {"text"}, "allowed": {"text"}},
    "key": {"required": {"combo"}, "allowed": {"combo"}},
    "screenshot": {"required": set(), "allowed": {"scope"}},
    "wait": {"required": set(), "allowed": {"ms"}},
    "done": {"required": set(), "allowed": set()},
    "stop": {"required": set(), "allowed": set()},
}
_BUTTONS = {"left", "right", "middle"}

# One self-closing tag: <verb .../> with optional `key="value"` (double or single quoted)
# attributes. Anchored + non-greedy so it can't span tags or run code.
_TAG_RE = re.compile(r"<\s*([a-z_]+)((?:\s+[a-z_]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*)\s*/?\s*>")
_ATTR_RE = re.compile(r"([a-z_]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_ACTIONS_RE = re.compile(r"<\s*actions\s*>(.*?)<\s*/\s*actions\s*>", re.DOTALL)
# An opening-tag candidate (NOT a closing `</tag>`): used to catch a malformed tag (e.g.
# unquoted `y=420` or uppercase attr) that _TAG_RE skips — which would otherwise drop the
# action silently and execute a partial plan.
_TAG_OPEN_RE = re.compile(r"<\s*[A-Za-z_]")


def _num(verb: str, attr: str, raw: str) -> float | int:
    try:
        return int(raw) if re.fullmatch(r"-?\d+", raw.strip()) else float(raw)
    except ValueError as exc:  # pragma: no cover - re guards the format
        raise DialectError(f"<{verb}>: attribute '{attr}' must be a number, got {raw!r}") from exc


def _target(verb: str, attrs: dict[str, str]) -> dict | None:
    """Resolve a pointing target from px (``x``/``y``) or normalized (``nx``/``ny``)."""
    has_px = "x" in attrs or "y" in attrs
    has_norm = "nx" in attrs or "ny" in attrs
    if has_px and has_norm:
        raise DialectError(f"<{verb}>: specify either x/y or nx/ny, not both")
    if has_px:
        if "x" not in attrs or "y" not in attrs:
            raise DialectError(f"<{verb}>: both x and y are required for a pixel target")
        return {
            "kind": "point_px",
            "x": _num(verb, "x", attrs["x"]),
            "y": _num(verb, "y", attrs["y"]),
        }
    if has_norm:
        if "nx" not in attrs or "ny" not in attrs:
            raise DialectError(f"<{verb}>: both nx and ny are required for a normalized target")
        nx, ny = float(attrs["nx"]), float(attrs["ny"])
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            raise DialectError(f"<{verb}>: nx/ny must be within [0, 1], got ({nx}, {ny})")
        return {"kind": "point_norm", "x": nx, "y": ny}
    return None


def _one(verb: str, attrs: dict[str, str]) -> dict:
    spec = _SPEC.get(verb)
    if spec is None:
        raise DialectError(f"unknown action '<{verb}>'")
    unknown = set(attrs) - spec["allowed"]
    if unknown:
        raise DialectError(f"<{verb}>: unknown attribute(s) {sorted(unknown)}")
    missing = spec["required"] - set(attrs)
    if missing:
        raise DialectError(f"<{verb}>: missing required attribute(s) {sorted(missing)}")

    if verb in ("done", "stop"):
        return dict(DONE)
    button = attrs.get("button")
    if button is not None and button not in _BUTTONS:
        raise DialectError(f"<{verb}>: button must be one of {sorted(_BUTTONS)}")

    if verb in _POINTING:
        target = _target(verb, attrs)
        # The ACI schema + X11 executor require a target for pointing verbs; a targetless
        # <click/> would validate here but fail at the wire. Require coordinates.
        if target is None:
            raise DialectError(f"<{verb}>: a coordinate (x/y or nx/ny) is required")
        # `button` is honored, never silently dropped: 'left' is the default, 'right' on a
        # <click> maps to the right_click verb, and anything contradictory ('middle', or a
        # non-left button on a verb that can't express it) is a teaching error.
        resolved = verb
        if button is not None:
            if button == "right" and verb == "click":
                resolved = "right_click"
            elif button != ("right" if verb == "right_click" else "left"):
                raise DialectError(
                    f"<{verb}>: button={button!r} is unsupported "
                    f"(use <right_click> for the right button; 'middle' has no ACI wire verb)"
                )
        return {"verb": resolved, "target": target}
    if verb == "scroll":
        action = {"verb": "scroll", "dy": _num(verb, "dy", attrs["dy"])}
        if "dx" in attrs:
            action["dx"] = _num(verb, "dx", attrs["dx"])
        # Scroll also requires a target on the wire; default to the screen centre when the
        # model gives none (matching the adapter behavior) so it doesn't fail at runtime.
        action["target"] = _target(verb, attrs) or {"kind": "point_norm", "x": 0.5, "y": 0.5}
        return action
    if verb == "type_text":
        return {"verb": "type_text", "text": attrs["text"]}
    if verb == "key":
        return {"verb": "key", "keys": attrs["combo"]}
    if verb == "screenshot":
        return {"verb": "screenshot", "scope": attrs.get("scope", "screen")}
    if verb == "wait":
        return (
            {"verb": "wait"}
            if "ms" not in attrs
            else {"verb": "wait", "ms": int(_num(verb, "ms", attrs["ms"]))}
        )
    raise DialectError(f"unknown action '<{verb}>'")  # pragma: no cover


def parse_actions(text: str, format: str = "auto") -> list[dict]:
    """Parse model output (Shinken tag dialect or string-form XML tool calls) into an
    ordered list of canonical ACI actions.

    ``format``:

    - ``"auto"`` (default) — route on content: text containing an XML tool-call grammar
      (``<tool_call>``, ``<invoke name=…>``, ``<function=…>``/``<function name=…>``,
      ``<action name=…>``, or a bare aliased verb tag like ``<left_click …/>``) goes to
      :func:`parse_xml_actions`; everything else uses the native Shinken tag dialect.
    - ``"xml"`` — force :func:`parse_xml_actions`.
    - ``"dialect"`` — force the native Shinken tag grammar (the pre-existing behavior).

    Accepts either an ``<actions>...</actions>`` block or bare top-level tags in dialect
    mode. A ``<done/>``/``<stop/>`` tag yields the :data:`DONE` control action. Raises
    :class:`DialectError` (a teaching error, never executed) on unknown tags/attributes,
    missing required fields, or invalid coordinates.
    """
    if format not in ("auto", "xml", "dialect"):
        raise DialectError(f"unknown format {format!r}: expected 'auto', 'xml', or 'dialect'")
    if not isinstance(text, str):
        raise DialectError("dialect output must be a string")
    if format == "xml" or (format == "auto" and looks_like_xml(text)):
        return parse_xml_actions(text)
    return _parse_dialect_actions(text)


def _parse_dialect_actions(text: str) -> list[dict]:
    """The native Shinken tag grammar (the Phase-0 reference dialect)."""
    block = _ACTIONS_RE.search(text)
    body = block.group(1) if block else text
    actions: list[dict] = []
    matched_starts: set[int] = set()
    for m in _TAG_RE.finditer(body):
        matched_starts.add(m.start())
        verb = m.group(1)
        if verb == "actions":
            continue
        attrs: dict[str, str] = {}
        for a in _ATTR_RE.finditer(m.group(2) or ""):
            attrs[a.group(1)] = a.group(2) if a.group(2) is not None else (a.group(3) or "")
        actions.append(_one(verb, attrs))
    # A tag-opener that _TAG_RE did not consume is a malformed tag (unquoted value,
    # uppercase name/attr, …). Raise a teaching error instead of silently dropping it,
    # which would execute only the well-formed siblings of a partial plan.
    for om in _TAG_OPEN_RE.finditer(body):
        if om.start() not in matched_starts:
            snippet = body[om.start() : om.start() + 40].splitlines()[0]
            raise DialectError(f"malformed tag near {snippet!r} (attributes must be quoted)")
    if not actions:
        raise DialectError("no actions found in dialect output")
    return actions


# =====================================================================================
# String-form XML tool calls — the grammars CU models actually emit as text.
# Tolerant regex+ElementTree hybrid; unknown verbs raise (never silently dropped).
# =====================================================================================

#: Pixels per wheel "click" for click-denominated scroll sources
#: (mirrors ``shinken.adapters.base.SCROLL_PX_PER_CLICK``).
_SCROLL_PX_PER_CLICK = 100

#: Model-emitted verb name → canonical ACI verb (names are lowercased and stripped of a
#: ``pyautogui.``-style namespace before lookup).
_XML_VERB_ALIASES: dict[str, str] = {
    "click": "click",
    "left_click": "click",
    "leftclick": "click",
    "tap": "click",
    "double_click": "double_click",
    "doubleclick": "double_click",
    "right_click": "right_click",
    "rightclick": "right_click",
    "context_click": "right_click",
    "move": "move",
    "mouse_move": "move",
    "move_to": "move",
    "moveto": "move",
    "hover": "move",
    "type": "type_text",
    "type_text": "type_text",
    "input_text": "type_text",
    "write": "type_text",
    "typewrite": "type_text",
    "key": "key",
    "keypress": "key",
    "press": "key",
    "press_key": "key",
    "hotkey": "key",
    "scroll": "scroll",
    "hscroll": "scroll",
    "vscroll": "scroll",
    "screenshot": "screenshot",
    "take_screenshot": "screenshot",
    "wait": "wait",
    "sleep": "wait",
}

#: Terminal verbs → the :data:`DONE` control action (Operator-loop signal, not a wire verb).
_XML_DONE_VERBS = {"done", "stop", "terminate", "finished", "finish", "complete", "answer"}

#: Verbs CU models emit that ACI v0 cannot model — typed teaching errors, never dropped.
_XML_UNSUPPORTED: dict[str, str] = {
    "middle_click": "the middle button has no ACI v0 wire verb",
    "triple_click": "triple-click has no ACI v0 wire verb",
    "left_click_drag": "drag has no ACI v0 wire verb",
    "drag": "drag has no ACI v0 wire verb",
    "cursor_position": "cursor_position is a read with no ACI equivalent",
    "hold_key": "key holds have no ACI v0 wire verb",
    "left_mouse_down": "raw mouse holds have no ACI v0 wire verb",
    "left_mouse_up": "raw mouse holds have no ACI v0 wire verb",
}

#: Tool names that *wrap* the verb in an ``action`` argument (Qwen ``computer_use``,
#: Anthropic ``computer``): unwrap, then normalize the inner action.
_XML_WRAPPERS = {
    "computer_use",
    "computer",
    "computer_tool",
    "computer_13",
    "computer_20241022",
    "computer_20250124",
}

_XML_KNOWN_NAMES = set(_XML_VERB_ALIASES) | _XML_DONE_VERBS | set(_XML_UNSUPPORTED)

#: Argument keys that can carry a pointing target.
_XML_TARGET_KEYS = {"x", "y", "nx", "ny", "coordinate", "coordinates", "point", "start_box"}
#: Per-canonical-verb allowed argument keys (union of the surveyed vendor vocabularies).
_XML_ALLOWED: dict[str, set[str]] = {
    "click": _XML_TARGET_KEYS | {"button"},
    "double_click": _XML_TARGET_KEYS | {"button"},
    "right_click": _XML_TARGET_KEYS | {"button"},
    "move": _XML_TARGET_KEYS,
    "scroll": _XML_TARGET_KEYS
    | {
        "pixels",
        "scroll_x",
        "scroll_y",
        "dx",
        "dy",
        "direction",
        "scroll_direction",
        "amount",
        "scroll_amount",
        "clicks",
    },
    "type_text": {"text", "content", "value"},
    "key": {"keys", "combo", "key", "text"},
    "screenshot": {"scope"},
    "wait": {"ms", "time", "duration", "seconds"},
}
_XML_DONE_ALLOWED = {"status", "answer", "text", "content", "value"}
#: Attribute keys that make an *unknown* bare tag look like an attempted action — those
#: raise instead of being ignored as prose markup (the never-silently-drop policy).
_XML_ACTIONISH_KEYS = _XML_TARGET_KEYS | {"keys", "combo", "button", "dx", "dy", "pixels"}

# --- grammar scanners (all tolerant: optional namespace prefix, optional close tag) ----
_FENCE_RE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_TOOL_CALL_RE = re.compile(
    r"<\s*(?:\w+:)?tool_call\s*>\s*(?P<body>.*?)\s*"
    r"(?:<\s*/\s*(?:\w+:)?tool_call\s*>|(?=<\s*(?:\w+:)?tool_call\s*>)|\Z)",
    re.DOTALL,
)
_INVOKE_RE = re.compile(
    r"<\s*(?:\w+:)?invoke\s+name\s*=\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<uq>[\w.\-]+))"
    r"\s*>(?P<body>.*?)(?:<\s*/\s*(?:\w+:)?invoke\s*>|\Z)",
    re.DOTALL,
)
_FUNCTION_RE = re.compile(
    r"<\s*(?:\w+:)?function"
    r"(?:\s*=\s*(?P<eq>[\w.\-]+)"
    r"|\s+name\s*=\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<uq>[\w.\-]+)))"
    r"\s*>(?P<body>.*?)(?:<\s*/\s*(?:\w+:)?function\s*>|\Z)",
    re.DOTALL,
)
_ACTION_EL_RE = re.compile(
    r"<\s*(?:\w+:)?action\s+name\s*=\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<uq>[\w.\-]+))"
    r"\s*(?:/\s*>|>(?P<body>.*?)(?:<\s*/\s*(?:\w+:)?action\s*>|\Z))",
    re.DOTALL,
)
_ANSWER_RE = re.compile(
    r"<\s*(?:\w+:)?answer\s*>\s*(?P<body>.*?)\s*(?:<\s*/\s*(?:\w+:)?answer\s*>|\Z)",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r"<\s*(?:\w+:)?param(?:eter)?"
    r"(?:\s*=\s*(?P<eq>[\w.\-]+)"
    r"|\s+name\s*=\s*(?:\"(?P<dq>[^\"]+)\"|'(?P<sq>[^']+)'|(?P<uq>[\w.\-]+)))"
    r"\s*>(?P<value>.*?)"
    r"(?:<\s*/\s*(?:\w+:)?param(?:eter)?\s*>|(?=<\s*(?:\w+:)?param(?:eter)?[\s=>])|\Z)",
    re.DOTALL,
)
_XML_BARE_TAG_RE = re.compile(
    r"<\s*(?P<name>[A-Za-z_][\w:.\-]*)"
    r"(?P<attrs>(?:\s+[\w\-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s/>]+))*)\s*/?\s*>"
)
_XML_BARE_ATTR_RE = re.compile(r"([\w\-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s/>]+))")
_CONTAINER_TAGS = {"tool_call", "invoke", "function", "action"}
_NUM_LITERAL_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)")

# Auto-detection: the call-container markers, plus bare verb tags whose names belong only
# to the XML alias vocabulary (native dialect verbs keep routing to the dialect grammar).
_XML_MARKER_RE = re.compile(
    r"<\s*(?:\w+:)?tool_call[\s>]"
    r"|<\s*(?:\w+:)?invoke\b"
    r"|<\s*(?:\w+:)?function(?:\s*=|\s+name\s*=)"
    r"|<\s*(?:\w+:)?action\s+name\s*="
    r"|<\s*(?:\w+:)?answer\b"
)
_ALIAS_ONLY_TAGS = sorted(
    (set(_XML_VERB_ALIASES) | _XML_DONE_VERBS | set(_XML_UNSUPPORTED) | _XML_WRAPPERS) - set(_SPEC)
)
_ALIAS_TAG_RE = re.compile(r"<\s*(?:" + "|".join(_ALIAS_ONLY_TAGS) + r")\b")


def looks_like_xml(text: str) -> bool:
    """True when ``text`` contains a string-form XML tool-call grammar that
    :func:`parse_xml_actions` understands (used by ``parse_actions(format='auto')``)."""
    return isinstance(text, str) and bool(_XML_MARKER_RE.search(text) or _ALIAS_TAG_RE.search(text))


def parse_xml_actions(text: str) -> list[dict]:
    """Parse string-form XML tool calls into an **ordered** list of canonical ACI actions.

    Grammars (see the module docstring): JSON-in-``<tool_call>`` (Qwen/Hermes),
    ``<invoke name=…>``/``<parameter name=…>`` blocks, ``<function=…>``/``<function
    name=…>`` parameter-element calls (Seed/UI-TARS-2), ``<action name=…>``/``<param>``
    elements, and bare attribute tags (``<left_click x="100" y="200"/>``). Tolerates
    markdown fences, namespace prefixes, unclosed tags, unquoted attribute values, and
    trailing-comma/truncated JSON. Terminal calls (``terminate``/``answer``/``done``)
    yield the :data:`DONE` control action. Raises :class:`DialectError` — carrying the
    offending snippet — on unknown or unsupported verbs, bad arguments, or when no call
    is found; an action-shaped tag is **never silently dropped**.
    """
    if not isinstance(text, str):
        raise DialectError("model output must be a string")
    body = _FENCE_RE.sub("", text)
    found: list[tuple[int, dict]] = []
    consumed: list[tuple[int, int]] = []

    for m in _TOOL_CALL_RE.finditer(body):
        consumed.append(m.span())
        for action in _parse_tool_call_body(m.group("body")):
            found.append((m.start(), action))
    for m in _INVOKE_RE.finditer(body):
        if _overlaps(consumed, m.span()):
            continue
        consumed.append(m.span())
        name = m.group("dq") or m.group("sq") or m.group("uq")
        found.append((m.start(), _normalize_call(name, _params(m.group("body")), m.group(0))))
    for m in _FUNCTION_RE.finditer(body):
        if _overlaps(consumed, m.span()):
            continue
        consumed.append(m.span())
        name = m.group("eq") or m.group("dq") or m.group("sq") or m.group("uq")
        found.append((m.start(), _normalize_call(name, _params(m.group("body")), m.group(0))))
    for m in _ACTION_EL_RE.finditer(body):
        if _overlaps(consumed, m.span()):
            continue
        consumed.append(m.span())
        name = m.group("dq") or m.group("sq") or m.group("uq")
        found.append((m.start(), _normalize_call(name, _params(m.group("body") or ""), m.group(0))))
    for m in _ANSWER_RE.finditer(body):
        if _overlaps(consumed, m.span()):
            continue
        consumed.append(m.span())
        done = dict(DONE)
        answer = m.group("body").strip()
        if answer:
            done["answer"] = answer
        found.append((m.start(), done))
    for m in _XML_BARE_TAG_RE.finditer(body):
        if _overlaps(consumed, m.span()):
            continue
        tag = m.group("name").rsplit(":", 1)[-1].lower()
        if tag in _CONTAINER_TAGS:
            # a call container none of the structured passes could parse — teach, not drop
            raise DialectError(f"malformed <{tag}> call near {_snip(m.group(0))!r}")
        attrs: dict[str, object] = {}
        for am in _XML_BARE_ATTR_RE.finditer(m.group("attrs") or ""):
            value = next(g for g in am.groups()[1:] if g is not None)
            attrs[am.group(1).lower()] = _coerce(value)
        if tag in _XML_KNOWN_NAMES or tag in _XML_WRAPPERS:
            consumed.append(m.span())
            found.append((m.start(), _normalize_call(tag, attrs, m.group(0))))
        elif set(attrs) & _XML_ACTIONISH_KEYS:
            raise DialectError(f"unknown action tag <{m.group('name')}> near {_snip(m.group(0))!r}")
        # else: prose/markup tag (<think>, <p>, …) — not an action, ignored

    found.sort(key=lambda item: item[0])  # stable: keeps within-block order
    actions = [action for _, action in found]
    if not actions:
        raise DialectError("no XML tool calls found in model output")
    return actions


# --- per-grammar helpers --------------------------------------------------------------


def _snip(s: str, limit: int = 80) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _overlaps(consumed: list[tuple[int, int]], span: tuple[int, int]) -> bool:
    start, end = span
    return any(cs < end and start < ce for cs, ce in consumed)


def _parse_tool_call_body(block: str) -> list[dict]:
    """One ``<tool_call>`` body → one or more ACI actions (JSON or function/invoke XML)."""
    if _FUNCTION_RE.search(block) or _INVOKE_RE.search(block):
        calls: list[dict] = []
        for fm in _FUNCTION_RE.finditer(block):
            name = fm.group("eq") or fm.group("dq") or fm.group("sq") or fm.group("uq")
            calls.append(_normalize_call(name, _params(fm.group("body")), fm.group(0)))
        for im in _INVOKE_RE.finditer(block):
            name = im.group("dq") or im.group("sq") or im.group("uq")
            calls.append(_normalize_call(name, _params(im.group("body")), im.group(0)))
        return calls
    if "{" in block:
        obj = _loose_json(block)
        if obj is None:
            raise DialectError(f"unparseable JSON inside <tool_call> near {_snip(block)!r}")
        return [_normalize_json_call(obj, block)]
    raise DialectError(
        f"<tool_call> contains neither JSON nor a recognizable call near {_snip(block)!r}"
    )


def _loose_json(block: str) -> object | None:
    """Tolerant JSON extraction: first object in the block, surviving trailing prose,
    trailing commas, and a truncated tail (unbalanced braces/brackets)."""
    start = block.find("{")
    if start == -1:
        return None
    candidate = block[start:]
    for attempt in (candidate, _json_repair(candidate)):
        try:
            obj, _ = json.JSONDecoder().raw_decode(attempt)
            return obj
        except ValueError:
            continue
    return None


def _json_repair(s: str) -> str:
    s = re.sub(r",\s*([}\]])", r"\1", s)  # trailing commas
    stack: list[str] = []
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    if in_str:
        s += '"'
    return s + "".join(reversed(stack))


def _normalize_json_call(obj: object, snippet: str) -> dict:
    if not isinstance(obj, dict):
        raise DialectError(f"tool-call JSON must be an object near {_snip(snippet)!r}")
    name = obj.get("name")
    args: object = None
    for key in ("arguments", "parameters", "input", "args"):
        if key in obj:
            args = obj[key]
            break
    if isinstance(args, str):  # double-encoded arguments
        args = _loose_json(args)
    if name is None and args is None and "action" in obj:
        # bare action object: {"action": "left_click", "coordinate": [x, y]}
        name, args = "computer_use", dict(obj)
    if not isinstance(name, str) or not name.strip():
        raise DialectError(f"tool-call JSON has no usable 'name' near {_snip(snippet)!r}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise DialectError(f"tool-call 'arguments' must be an object near {_snip(snippet)!r}")
    return _normalize_call(name, args, snippet)


def _et_fragment(block: str) -> ET.Element | None:
    """ElementTree parse of a (sanitized) XML fragment; None when it isn't well-formed
    enough — the caller falls back to regex extraction. DOCTYPE/entity payloads are
    refused outright (no DTD processing, ever)."""
    if "<!" in block:
        return None
    cleaned = re.sub(r"<(/?)\w+:", r"<\1", block)  # strip namespace prefixes
    cleaned = re.sub(  # <function=click> / <parameter=point> → name="…" attributes
        r"<(function|parameter|param)\s*=\s*([\w.\-]+)\s*>", r'<\1 name="\2">', cleaned
    )
    cleaned = re.sub(r"&(?!\w+;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", cleaned)
    try:
        return ET.fromstring(f"<shinken-root>{cleaned}</shinken-root>")
    except ET.ParseError:
        return None


def _params(block: str) -> dict:
    """``<parameter name="…">value</parameter>`` / ``<parameter=…>`` / ``<param …>``
    children → an argument dict (ElementTree first, regex fallback for malformed XML)."""
    args: dict[str, object] = {}
    root = _et_fragment(block)
    if root is not None:
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1].lower()
            if tag in ("parameter", "param"):
                key = (el.get("name") or el.get("key") or "").strip().lower()
                if key:
                    args[key] = _coerce("".join(el.itertext()).strip())
        if args:
            return args
    for pm in _PARAM_RE.finditer(block):
        key = (pm.group("eq") or pm.group("dq") or pm.group("sq") or pm.group("uq") or "").lower()
        if key:
            args[key] = _coerce(pm.group("value").strip())
    return args


def _coerce(value: str) -> object:
    """Best-effort typed coercion of an XML parameter value: JSON literal (number, list,
    bool, quoted string) when it parses, else the raw string."""
    try:
        return json.loads(value)
    except ValueError:
        return value


# --- normalization to canonical ACI actions --------------------------------------------


def _normalize_call(name: str, args: dict, snippet: str) -> dict:
    """One extracted (tool name, argument dict) call → a canonical ACI action."""
    verb = (name or "").strip().lower().rsplit(".", 1)[-1]
    args = {str(k).strip().lower(): v for k, v in args.items()}
    if verb in _XML_WRAPPERS:
        action = args.pop("action", None)
        if action is None:  # qwen3.5-4b XML emits the action under a `type` parameter
            maybe = args.get("type")
            if isinstance(maybe, str) and maybe.strip().lower() in _XML_KNOWN_NAMES:
                action = args.pop("type")
        if not isinstance(action, str) or not action.strip():
            raise DialectError(
                f"'{name}' call has no usable 'action' argument near {_snip(snippet)!r}"
            )
        verb = action.strip().lower()
    if verb in _XML_UNSUPPORTED:
        raise DialectError(f"<{verb}>: {_XML_UNSUPPORTED[verb]} (near {_snip(snippet)!r})")
    if verb in _XML_DONE_VERBS:
        unknown = set(args) - _XML_DONE_ALLOWED
        if unknown:
            raise DialectError(
                f"<{verb}>: unknown argument(s) {sorted(unknown)} (near {_snip(snippet)!r})"
            )
        return _xml_done(verb, args)
    canon = _XML_VERB_ALIASES.get(verb)
    if canon is None:
        raise DialectError(
            f"unknown action verb {verb!r} in XML tool call (near {_snip(snippet)!r})"
        )
    unknown = set(args) - _XML_ALLOWED[canon]
    if unknown:
        raise DialectError(
            f"<{verb}>: unknown argument(s) {sorted(unknown)} (near {_snip(snippet)!r})"
        )
    return _build_xml_action(canon, verb, args, snippet)


def _build_xml_action(canon: str, verb: str, args: dict, snippet: str) -> dict:
    if canon in _POINTING:
        target = _xml_target(verb, args, snippet)
        if target is None:
            raise DialectError(
                f"<{verb}>: a coordinate (coordinate/point/x,y) is required "
                f"(near {_snip(snippet)!r})"
            )
        resolved = canon
        button = args.get("button")
        if button is not None and canon != "move":
            b = {"1": "left", "2": "middle", "3": "right"}.get(str(button).strip().lower())
            b = b or str(button).strip().lower()
            if b == "right" and canon == "click":
                resolved = "right_click"
            elif b != ("right" if canon == "right_click" else "left"):
                raise DialectError(
                    f"<{verb}>: button={button!r} is unsupported "
                    f"(use right_click for the right button; 'middle' has no ACI wire verb)"
                )
        return {"verb": resolved, "target": target}
    if canon == "scroll":
        return _xml_scroll(verb, args, snippet)
    if canon == "type_text":
        for key in ("text", "content", "value"):
            if key in args:
                return {"verb": "type_text", "text": str(args[key])}
        raise DialectError(f"<{verb}>: missing text argument (near {_snip(snippet)!r})")
    if canon == "key":
        return _xml_key(verb, args, snippet)
    if canon == "screenshot":
        scope = args.get("scope")
        return {"verb": "screenshot", "scope": str(scope)} if scope else {"verb": "screenshot"}
    if canon == "wait":
        return _xml_wait(verb, args, snippet)
    # pragma: no cover — every _XML_VERB_ALIASES value is handled above
    raise DialectError(f"unknown action verb {verb!r} (near {_snip(snippet)!r})")


def _xml_done(verb: str, args: dict) -> dict:
    done = dict(DONE)
    status = args.get("status")
    if status is not None and str(status).strip().lower() not in (
        "success",
        "succeeded",
        "done",
        "complete",
        "completed",
        "ok",
    ):
        done["status"] = "fail"
    for key in ("answer", "text", "content", "value"):
        if key in args and args[key] is not None:
            done["answer"] = str(args[key])
            break
    return done


def _xml_key(verb: str, args: dict, snippet: str) -> dict:
    keys: object = None
    for key in ("keys", "combo", "key", "text"):
        if key in args:
            keys = args[key]
            break
    if isinstance(keys, list | tuple):
        keys = "+".join(str(k).strip().lower() for k in keys if str(k).strip())
    elif keys is not None:
        keys = str(keys).strip().lower()
        if " " in keys and "+" not in keys:  # 'ctrl shift t' → 'ctrl+shift+t'
            keys = "+".join(keys.split())
    if not keys:
        raise DialectError(f"<{verb}>: missing key sequence (near {_snip(snippet)!r})")
    return {"verb": "key", "keys": keys}


def _xml_wait(verb: str, args: dict, snippet: str) -> dict:
    if "ms" in args:
        ms = int(_xml_num(verb, "ms", args["ms"], snippet))
    else:
        for key in ("time", "duration", "seconds"):
            if key in args:
                ms = int(round(_xml_num(verb, key, args[key], snippet) * 1000))
                break
        else:
            return {"verb": "wait"}
    if ms < 0:
        raise DialectError(f"<{verb}>: wait duration must be >= 0 (near {_snip(snippet)!r})")
    return {"verb": "wait", "ms": ms}


def _xml_scroll(verb: str, args: dict, snippet: str) -> dict:
    dx: float | None = None
    dy: float | None = None
    if "pixels" in args:  # Qwen/pyautogui semantics: positive = up; ACI: +dy = down
        dy = -_xml_num(verb, "pixels", args["pixels"], snippet)
    if "scroll_y" in args:  # OpenAI semantics: pixel-denominated, +y = down (pass through)
        dy = _xml_num(verb, "scroll_y", args["scroll_y"], snippet)
    if "scroll_x" in args:
        dx = _xml_num(verb, "scroll_x", args["scroll_x"], snippet)
    if "dy" in args:
        dy = _xml_num(verb, "dy", args["dy"], snippet)
    if "dx" in args:
        dx = _xml_num(verb, "dx", args["dx"], snippet)
    direction = args.get("direction", args.get("scroll_direction"))
    if direction is not None:
        amount = args.get("amount", args.get("scroll_amount", args.get("clicks", 3)))
        px = _xml_num(verb, "amount", amount, snippet) * _SCROLL_PX_PER_CLICK
        d = str(direction).strip().lower()
        if d == "up":
            dy = -px
        elif d == "down":
            dy = px
        elif d == "left":
            dx = -px
        elif d == "right":
            dx = px
        else:
            raise DialectError(
                f"<{verb}>: unknown scroll direction {direction!r} (near {_snip(snippet)!r})"
            )
    if dx is None and dy is None:
        raise DialectError(
            f"<{verb}>: no scroll magnitude (dy/dx, pixels, scroll_x/scroll_y, or "
            f"direction+amount) (near {_snip(snippet)!r})"
        )
    # The ACI schema + X11 executor require a scroll target; default to the screen centre.
    action: dict = {
        "verb": "scroll",
        "target": _xml_target(verb, args, snippet) or {"kind": "point_norm", "x": 0.5, "y": 0.5},
    }
    if dy is not None:
        action["dy"] = dy
    if dx is not None:
        action["dx"] = dx
    return action


def _xml_num(verb: str, key: str, value: object, snippet: str) -> float | int:
    if isinstance(value, bool):
        pass
    elif isinstance(value, int | float):
        return value
    elif isinstance(value, str):
        v = value.strip()
        if re.fullmatch(r"[-+]?\d+", v):
            return int(v)
        try:
            return float(v)
        except ValueError:
            pass
    raise DialectError(
        f"<{verb}>: argument {key!r} must be a number, got {value!r} (near {_snip(snippet)!r})"
    )


def _xml_target(verb: str, args: dict, snippet: str) -> dict | None:
    """Resolve a pointing target from the surveyed coordinate spellings. Integer (or
    integral) pairs are pixels; fractional pairs within [0, 1] are normalized."""
    if "nx" in args or "ny" in args:
        if "nx" not in args or "ny" not in args:
            raise DialectError(f"<{verb}>: both nx and ny are required for a normalized target")
        nx = float(_xml_num(verb, "nx", args["nx"], snippet))
        ny = float(_xml_num(verb, "ny", args["ny"], snippet))
        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            raise DialectError(f"<{verb}>: nx/ny must be within [0, 1], got ({nx}, {ny})")
        return {"kind": "point_norm", "x": nx, "y": ny}
    pair: tuple[float | int, float | int] | None = None
    if "x" in args or "y" in args:
        if "x" not in args or "y" not in args:
            raise DialectError(f"<{verb}>: both x and y are required for a pixel target")
        pair = (_xml_num(verb, "x", args["x"], snippet), _xml_num(verb, "y", args["y"], snippet))
    else:
        for key in ("coordinate", "coordinates", "point", "start_box"):
            if key in args:
                pair = _coord_pair(verb, key, args[key], snippet)
                break
    if pair is None:
        return None
    x, y = pair
    fx, fy = float(x), float(y)
    if (isinstance(x, float) or isinstance(y, float)) and 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
        return {"kind": "point_norm", "x": fx, "y": fy}
    return {"kind": "point_px", "x": int(round(fx)), "y": int(round(fy))}


def _coord_pair(
    verb: str, key: str, value: object, snippet: str
) -> tuple[float | int, float | int]:
    """A coordinate argument value → an (x, y) pair. Accepts a 2-list, '[x, y]'/'(x, y)'/
    'x y'/'x, y' strings (inner tags like ``<point>…</point>`` stripped), and a 4-number
    box whose centre is taken."""
    nums: list[float | int]
    if isinstance(value, list | tuple):
        nums = [v for v in value if isinstance(v, int | float) and not isinstance(v, bool)]
    else:
        text = re.sub(r"<[^>]+>", " ", str(value))
        nums = [
            int(tok) if re.fullmatch(r"[-+]?\d+", tok) else float(tok)
            for tok in _NUM_LITERAL_RE.findall(text)
        ]
    if len(nums) == 2:
        return nums[0], nums[1]
    if len(nums) == 4:  # a box: take the centre
        return (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2
    raise DialectError(
        f"<{verb}>: could not parse a coordinate from {key}={value!r} (near {_snip(snippet)!r})"
    )
