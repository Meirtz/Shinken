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


#: ``fs_scope`` values that mean "no filesystem access is granted".
_NO_FS_SCOPE = {None, "", "none", "false", False}


class CapabilityDenied(RuntimeError):
    """Raised when the gateway denies an action because its capability isn't granted."""


def check_file_transfer(direction: str, capabilities: dict) -> tuple[bool, str, str]:
    """Decide whether a file transfer (#85) is permitted by the capability envelope.

    File transfer gates on ``fs_scope`` (a scope *string* such as ``"session"``, not a
    boolean); ``none``/empty/false means no filesystem access. Returns
    ``(allowed, "fs_scope", reason)``. Path containment (no ``..`` / absolute escape) is
    enforced separately and unconditionally by the artifact store."""
    scope = capabilities.get("fs_scope")
    allowed = scope not in _NO_FS_SCOPE
    reason = (
        f"{direction} permitted within fs_scope '{scope}'"
        if allowed
        else "capability 'fs_scope' not granted"
    )
    return (allowed, "fs_scope", reason)


def decide_action(verb: str, capabilities: dict) -> tuple[str, str | None, str]:
    """Three-state gateway decision for ``verb`` against the capability envelope (#7).

    Returns ``(decision, capability, reason)`` where ``decision`` is ``"allow"``,
    ``"deny"``, or ``"ask"``. A capability whose value is the string ``"ask"`` means the
    verb is **risky** and must pause for approval; any other truthy value allows it, and
    a falsy/absent value denies it. Unknown verbs require the conservative
    ``input_automation`` capability (deny-by-default for anything unrecognised)."""
    cap = VERB_CAPABILITY.get(verb, "input_automation")
    if cap is None:
        return ("allow", None, "no capability required")
    val = capabilities.get(cap, False)
    if isinstance(val, str) and val.lower() == "ask":
        return ("ask", cap, f"capability '{cap}' requires approval")
    if val:
        return ("allow", cap, "permitted by envelope")
    return ("deny", cap, f"capability '{cap}' not granted")


def check_action(verb: str, capabilities: dict) -> tuple[bool, str | None, str]:
    """Binary view of :func:`decide_action` — ``(allowed, capability, reason)`` where an
    ``ask`` decision counts as *not yet allowed*. Retained for callers that don't handle
    the approval tier."""
    decision, cap, reason = decide_action(verb, capabilities)
    return (decision == "allow", cap, reason)
