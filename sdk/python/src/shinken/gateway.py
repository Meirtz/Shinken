"""Local Action Gateway shim (#84) — the policy seam, client-side for v0.

The full Control-Plane Action Gateway comes later; v0.0.1 lands the *seam* locally so
clients never learn to bypass the policy/replay boundary. Each ACI verb maps to a
capability in the session envelope (#83); an action whose capability is not granted is
**denied before dispatch** (it never reaches ``shinkend``) and the decision is recorded.

This is reference semantics — a small capability map, not Cedar/ocap/OS enforcement.
"""

from __future__ import annotations

#: ACI verb → the capability-envelope flag it requires (None = always allowed).
VERB_CAPABILITY: dict[str, str | None] = {
    "click": "input_automation",
    "double_click": "input_automation",
    "right_click": "input_automation",
    "move": "input_automation",
    "scroll": "input_automation",
    "type_text": "input_automation",
    "key": "input_automation",
    "screenshot": "screenshot",
    "start_screencast": "screenshot",
    "stop_screencast": "screenshot",
    "wait": None,
}


class CapabilityDenied(RuntimeError):
    """Raised when the gateway denies an action because its capability isn't granted."""


def check_action(verb: str, capabilities: dict) -> tuple[bool, str | None, str]:
    """Decide whether ``verb`` is permitted by the capability envelope.

    Returns ``(allowed, capability, reason)``. Unknown verbs require the conservative
    ``input_automation`` capability (deny-by-default for anything unrecognised)."""
    cap = VERB_CAPABILITY.get(verb, "input_automation")
    if cap is None:
        return (True, None, "no capability required")
    allowed = bool(capabilities.get(cap, False))
    reason = "permitted by envelope" if allowed else f"capability '{cap}' not granted"
    return (allowed, cap, reason)
