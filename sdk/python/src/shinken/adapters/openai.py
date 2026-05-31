"""OpenAI Computer Use adapter for ACI v0 (#76).

Translates OpenAI's computer-use tool calls into canonical ACI actions, renders Shinken
screenshots back into ``computer_call_output``, and maps ``pending_safety_checks`` into
Shinken permission events. Accepts either a single ``action`` (the live shape) or an
``actions[]`` batch — both normalize to an **ordered list** of ACI actions. Fixture-
tested; no live API calls. Public tool reference:
https://platform.openai.com/docs/guides/tools-computer-use
"""

from __future__ import annotations

from .base import AdapterError, data_uri_png, point_px

#: OpenAI button values that ACI v0 can model (others → structured AdapterError).
_BUTTON_VERB = {"left": "click", "right": "right_click"}

#: OpenAI action types ACI v0 does not model.
UNSUPPORTED = {"drag"}


def _xy(action: dict) -> dict:
    return point_px([action.get("x"), action.get("y")])


class OpenAIComputerUseAdapter:
    """Stateless translator between the OpenAI computer-use tool and ACI v0."""

    name = "openai-computer-use"
    tool = "computer_use_preview"

    def __init__(self, model: str = "computer-use-preview"):
        self.model = model

    def to_aci_actions(self, computer_call: dict) -> list[dict]:
        """An OpenAI ``computer_call`` → an **ordered list** of canonical ACI actions.

        Accepts ``actions: [...]`` (batch) or a single ``action: {...}``. Raises
        :class:`AdapterError` if neither is present or any action can't be mapped."""
        actions = computer_call.get("actions")
        if actions is None:
            one = computer_call.get("action")
            actions = [one] if one is not None else None
        if not actions:
            raise AdapterError("(missing)", "computer_call has no action(s)")
        return [self._one(a) for a in actions]

    def _one(self, a: dict) -> dict:
        t = a.get("type")
        if not t:
            raise AdapterError("(missing)", "action has no 'type'")
        if t in UNSUPPORTED:
            raise AdapterError(t, "unsupported by ACI v0")
        if t == "click":
            button = a.get("button", "left")
            verb = _BUTTON_VERB.get(button)
            if verb is None:
                raise AdapterError(f"click:{button}", "mouse button unsupported by ACI v0")
            return {"verb": verb, "target": _xy(a)}
        if t == "double_click":
            return {"verb": "double_click", "target": _xy(a)}
        if t == "move":
            return {"verb": "move", "target": _xy(a)}
        if t == "type":
            text = a.get("text")
            if text is None:
                raise AdapterError("type", "missing 'text'")
            return {"verb": "type_text", "text": str(text)}
        if t == "keypress":
            keys = a.get("keys")
            if not keys:
                raise AdapterError("keypress", "missing 'keys'")
            return {"verb": "key", "keys": "+".join(str(k).lower() for k in keys)}
        if t == "scroll":
            act: dict = {"verb": "scroll", "target": _xy(a)}
            # preserve an explicit zero delta (key presence, not truthiness) — #146
            if "scroll_x" in a:
                act["dx"] = a["scroll_x"]
            if "scroll_y" in a:
                act["dy"] = a["scroll_y"]
            return act
        if t == "screenshot":
            return {"verb": "screenshot"}
        if t == "wait":
            ms = a.get("ms")
            if isinstance(ms, int | float) and not isinstance(ms, bool):
                return {"verb": "wait", "ms": int(ms)}
            return {"verb": "wait"}
        raise AdapterError(t, "unrecognized OpenAI computer action")

    def safety_check_events(self, computer_call: dict) -> list[dict]:
        """``pending_safety_checks`` → Shinken permission-event payloads (decision
        ``ask``) — a safety check is a boundary decision the host must acknowledge."""
        checks = computer_call.get("pending_safety_checks") or []
        return [
            {
                "decision": "ask",
                "capability": "safety_check",
                "id": c.get("id"),
                "code": c.get("code"),
                "message": c.get("message"),
            }
            for c in checks
        ]

    def to_computer_call_output(
        self,
        observation: dict,
        *,
        call_id: str | None = None,
        acknowledged_safety_checks: list | None = None,
    ) -> dict:
        """A Shinken screenshot observation → an OpenAI ``computer_call_output``."""
        png = observation.get("png")
        if not png:
            raise AdapterError("screenshot", "observation has no PNG bytes")
        out: dict = {
            "type": "computer_call_output",
            "output": {"type": "computer_screenshot", "image_url": data_uri_png(png)},
        }
        if call_id is not None:
            out["call_id"] = call_id
        if acknowledged_safety_checks:
            out["acknowledged_safety_checks"] = acknowledged_safety_checks
        return out

    def run_metadata(self) -> dict:
        """Provider / model / tool version metadata for callers that need run context."""
        return {
            "adapter": self.name,
            "provider": "openai",
            "tool": self.tool,
            "model": self.model,
            "coordinate_space": "point_px",
        }
