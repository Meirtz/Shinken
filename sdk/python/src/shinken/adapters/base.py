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


def screenshot_image_block(png: bytes) -> dict:
    """PNG bytes → a base64 image content block — the shape both Anthropic and OpenAI
    use to carry a screenshot back to the model."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def image_size(observation: dict) -> dict:
    """Pull ``{w, h, scope}`` out of a screenshot observation for coordinate-space
    metadata (recorded so model pixels can be mapped back to the display)."""
    image = observation.get("image", {}) or {}
    return {"w": image.get("w"), "h": image.get("h"), "scope": image.get("scope", "screen")}
