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

Malformed output raises :class:`DialectError` with a teaching message, so an Operator
loop can hand the error back to the model rather than execute unsupported output.
"""

from __future__ import annotations

import re

__all__ = ["DialectError", "parse_actions", "DONE"]

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


def parse_actions(text: str) -> list[dict]:
    """Parse model dialect output into a list of canonical ACI actions.

    Accepts either an ``<actions>...</actions>`` block or bare top-level tags. A
    ``<done/>``/``<stop/>`` tag yields the :data:`DONE` control action. Raises
    :class:`DialectError` (a teaching error, never executed) on unknown tags/attributes,
    missing required fields, or invalid coordinates.
    """
    if not isinstance(text, str):
        raise DialectError("dialect output must be a string")
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
