"""Anthropic Computer Use adapter for ACI v0 (#75).

Translates the Anthropic ``computer`` tool (a canonical screenshot-first, versioned
computer-use contract) into canonical ACI actions, and renders Shinken screenshot
observations as Anthropic ``tool_result`` content. Fixture-tested end to end; no live
API calls. Public tool reference:
https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/computer-use-tool
"""

from __future__ import annotations

from .base import (
    SCROLL_PX_PER_CLICK,
    AdapterError,
    image_size,
    point_px,
    screenshot_image_block,
)

#: Anthropic computer-use tool version this adapter targets (public, versioned contract).
TOOL_VERSION = "computer_20250124"

#: Anthropic actions ACI v0 does not model — reported as structured AdapterErrors rather
#: than silently dropped (drag/middle/triple-click and the low-level mouse/key holds are
#: post-v0 ACI verbs; cursor_position is a read with no ACI equivalent).
UNSUPPORTED = {
    "left_click_drag",
    "middle_click",
    "triple_click",
    "cursor_position",
    "hold_key",
    "left_mouse_down",
    "left_mouse_up",
}


class AnthropicComputerUseAdapter:
    """Stateless translator between the Anthropic ``computer`` tool and ACI v0."""

    name = "anthropic-computer-use"
    tool_version = TOOL_VERSION

    def to_aci_action(self, tool_input: dict) -> dict:
        """Anthropic ``computer`` tool_use input → a canonical ACI action dict.

        Raises :class:`AdapterError` for missing fields or actions ACI v0 cannot model."""
        action = tool_input.get("action")
        if not action:
            raise AdapterError("(missing)", "tool_use input has no 'action'")
        if action in UNSUPPORTED:
            raise AdapterError(action, "unsupported by ACI v0")

        coord = tool_input.get("coordinate")
        text = tool_input.get("text")
        if action == "key":
            if not text:
                raise AdapterError("key", "missing 'text' key sequence")
            return {"verb": "key", "keys": str(text)}
        if action == "type":
            if text is None:
                raise AdapterError("type", "missing 'text'")
            return {"verb": "type_text", "text": str(text)}
        # Anthropic permits a `text` modifier (a key combo held during the action) on
        # clicks and scroll. ACI v0 has no hold semantics, so a modifier-click would
        # silently degrade to a plain click — surface it as unsupported instead (matching
        # the UNSUPPORTED policy) rather than changing the model's intent.
        if text and action in ("left_click", "right_click", "double_click", "scroll"):
            raise AdapterError(action, "modifier key held during click/scroll unsupported")
        if action == "mouse_move":
            return {"verb": "move", "target": point_px(coord)}
        if action == "left_click":
            return {"verb": "click", "target": point_px(coord)}
        if action == "right_click":
            return {"verb": "right_click", "target": point_px(coord)}
        if action == "double_click":
            return {"verb": "double_click", "target": point_px(coord)}
        if action == "screenshot":
            return {"verb": "screenshot"}
        if action == "wait":
            return {"verb": "wait", "ms": _duration_ms(tool_input.get("duration"))}
        if action == "scroll":
            return _scroll(coord, tool_input)
        raise AdapterError(action, "unrecognized Anthropic computer action")

    def to_tool_result(self, observation: dict) -> dict:
        """A Shinken screenshot observation → Anthropic ``tool_result`` content.

        Preserves coordinate space + image size as ``metadata`` so model pixels map back
        to the display."""
        png = observation.get("png")
        if not png:
            raise AdapterError("screenshot", "observation has no PNG bytes")
        size = image_size(observation)
        return {
            "content": [screenshot_image_block(png)],
            "metadata": {
                "coordinate_space": "point_px",
                "image_size": {"w": size["w"], "h": size["h"]},
                "scope": size["scope"],
            },
        }

    def run_metadata(self) -> dict:
        """Adapter identity metadata for callers that need run context."""
        return {
            "adapter": self.name,
            "tool_version": self.tool_version,
            "coordinate_space": "point_px",
        }


def _duration_ms(duration: object) -> int:
    if duration is None:
        return 0
    if isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0:
        raise AdapterError("wait", f"invalid duration {duration!r}")
    return int(duration * 1000)


def _scroll(coord: object, tool_input: dict) -> dict:
    direction = tool_input.get("scroll_direction")
    amount = tool_input.get("scroll_amount", 0)
    if direction not in ("up", "down", "left", "right"):
        raise AdapterError("scroll", f"invalid scroll_direction {direction!r}")
    if isinstance(amount, bool) or not isinstance(amount, int | float) or amount < 0:
        raise AdapterError("scroll", f"invalid scroll_amount {amount!r}")
    # scroll_amount is a count of wheel clicks; the ACI wire contract is pixels.
    px = amount * SCROLL_PX_PER_CLICK
    act: dict = {"verb": "scroll", "target": point_px(coord)}
    if direction in ("left", "right"):
        act["dx"] = -px if direction == "left" else px
    else:
        act["dy"] = -px if direction == "up" else px
    return act
