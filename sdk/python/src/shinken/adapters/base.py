"""Shared base for off-the-shelf computer-use provider adapters (#75 / #76).

An adapter translates a frontier provider's computer-use tool contract into canonical
ACI actions, and renders Shinken observations back into that provider's result shape.
This proves the agent-native thesis: any CU agent can drive Shinken through a thin,
fully fixture-tested adapter — **no live API calls required**. Provider-specific
adapters (Anthropic, OpenAI) build on these shared primitives; they are independent of
the generic Shinken-native action dialect (#68/#74).
"""

from __future__ import annotations

import base64

#: Pixels per wheel "click". The ACI wire contract is that scroll ``dx``/``dy`` are
#: **pixels** (shinkend's X11 backend converts ~100 px → one wheel step). Providers that
#: count scroll in discrete wheel clicks (Anthropic ``scroll_amount``, Kimi/Aguvis
#: ``scroll(n)``) must convert at the adapter boundary; OpenAI is already pixel-denominated.
SCROLL_PX_PER_CLICK = 100


class AdapterError(ValueError):
    """A provider action the adapter cannot map onto ACI v0 (unsupported verb,
    malformed coordinate, …). Structured (``action`` + ``reason``) so a caller can
    surface it as a tool error instead of panicking — the contract every adapter owes
    its host."""

    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        super().__init__(f"{action}: {reason}")


def point_px(coordinate: object) -> dict:
    """An ``[x, y]`` pixel coordinate → an ACI ``point_px`` target. Raises
    :class:`AdapterError` on a missing or non-numeric coordinate."""
    if not isinstance(coordinate, list | tuple) or len(coordinate) != 2:
        raise AdapterError("coordinate", f"expected [x, y], got {coordinate!r}")
    x, y = coordinate
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in (x, y)):
        raise AdapterError("coordinate", f"non-numeric coordinate {coordinate!r}")
    return {"kind": "point_px", "x": int(x), "y": int(y)}


def point_norm(x: object, y: object) -> dict:
    """Normalized ``[0, 1]`` coordinates → an ACI ``point_norm`` target (origin top-left).
    Used by adapters whose model emits resolution-independent fractions (e.g. Kimi-VL /
    Aguvis). Raises :class:`AdapterError` on non-numeric or out-of-range values — notably
    catching a model that emits ``[0, 1000]`` integers (UI-TARS/Qwen-VL style) instead of
    ``[0, 1]`` floats, which would otherwise silently land every click in the top-left."""
    for v in (x, y):
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise AdapterError("coordinate", f"non-numeric coordinate {(x, y)!r}")
        if not (0.0 <= float(v) <= 1.0):
            raise AdapterError("coordinate", f"normalized coordinate out of [0, 1]: {(x, y)!r}")
    return {"kind": "point_norm", "x": float(x), "y": float(y)}


def _media_type(format: str | None) -> str:
    """Observation ``format`` → MIME type. Defaults to PNG (the wire default); JPEG is
    the bandwidth lever. The label must track the actual codec — sending JPEG bytes
    labeled image/png corrupts the model's decode."""
    return "image/jpeg" if format in ("jpeg", "jpg") else "image/png"


def screenshot_image_block(data: bytes, format: str | None = None) -> dict:
    """Encoded image bytes → a base64 image content block — the shape both Anthropic and
    OpenAI use to carry a screenshot back to the model. ``format`` is the observation's
    codec (``png`` default / ``jpeg``)."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _media_type(format),
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def data_uri(data: bytes, format: str | None = None) -> str:
    """Encoded image bytes → a ``data:image/…;base64,…`` URI — the shape OpenAI's
    ``computer_call_output`` uses to carry a screenshot back to the model."""
    return f"data:{_media_type(format)};base64," + base64.b64encode(data).decode("ascii")


def data_uri_png(png: bytes) -> str:
    """Back-compat alias of :func:`data_uri` for PNG bytes."""
    return data_uri(png, "png")


def actions_from_text(text: str) -> list[dict]:
    """Raw model text containing **string-form XML tool calls** (or the Shinken tag
    dialect) → an ordered list of canonical ACI actions. Many CU models emit their tool
    calls as text rather than structured ``tool_use`` JSON; this delegates to
    :func:`shinken.dialect.parse_actions` (``format="auto"``) and re-raises its teaching
    errors as structured :class:`AdapterError`\\ s so adapter hosts keep one exception
    contract. Unknown verbs raise — an action is never silently dropped."""
    from shinken.dialect import DialectError, parse_actions

    try:
        return parse_actions(text, format="auto")
    except DialectError as exc:
        raise AdapterError("from_text", str(exc)) from exc


def image_size(observation: dict) -> dict:
    """Pull ``{w, h, scope}`` out of a screenshot observation for coordinate-space
    metadata (recorded so model pixels can be mapped back to the display)."""
    image = observation.get("image", {}) or {}
    return {"w": image.get("w"), "h": image.get("h"), "scope": image.get("scope", "screen")}
