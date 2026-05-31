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
is a small hand-written tag scanner, so there is no XML entity/DTD attack surface). It is
the adapter boundary's reference grammar — vendor adapters may use their own
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
    if "button" in attrs and attrs["button"] not in _BUTTONS:
        raise DialectError(f"<{verb}>: button must be one of {sorted(_BUTTONS)}")

    if verb in _POINTING:
        target = _target(verb, attrs)
        action: dict = {"verb": verb}
        if target is not None:
            action["target"] = target
        return action
    if verb == "scroll":
        action = {"verb": "scroll", "dy": _num(verb, "dy", attrs["dy"])}
        if "dx" in attrs:
            action["dx"] = _num(verb, "dx", attrs["dx"])
        target = _target(verb, attrs)
        if target is not None:
            action["target"] = target
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
    for m in _TAG_RE.finditer(body):
        verb = m.group(1)
        if verb == "actions":
            continue
        attrs: dict[str, str] = {}
        for a in _ATTR_RE.finditer(m.group(2) or ""):
            attrs[a.group(1)] = a.group(2) if a.group(2) is not None else (a.group(3) or "")
        actions.append(_one(verb, attrs))
    if not actions:
        raise DialectError("no actions found in dialect output")
    return actions
