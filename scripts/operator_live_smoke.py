"""Live Operator smoke (#6 / M3): an off-the-shelf vision model drives Shinken end-to-end.

Connects to a running ``shinkend`` sandbox, and on each turn sends the live screenshot to
a vision model, asks for ONE next ACI action as JSON, executes it through the SDK, and
repeats until the model says done or ``max_steps``. The whole run is recorded as one
``.skn`` bundle — i.e. an unmodified off-the-shelf model driving Shinken purely through
the ACI.

Provider-neutral + secret-safe: model creds come from ``SHK_SMOKE_MODEL_*`` env (never
printed; map them from your ignored local config at runtime). The model is treated as a
generic OpenAI-compatible chat/vision endpoint, so this uses a tiny JSON action protocol
rather than a vendor computer-use tool format (for vendor formats, use the
``shinken.adapters`` Anthropic/OpenAI adapters instead).

Env:
  SHK_ADDR (default 127.0.0.1:8765), SHK_TOKEN (bearer token for the sandbox)
  SHK_SMOKE_MODEL_BASE_URL / SHK_SMOKE_MODEL_API_KEY / SHK_SMOKE_MODEL_NAME
  SHK_MAX_STEPS (default 6), SHK_SKN (output bundle path), SHK_GOAL (task text)

Usage: PYTHONPATH=sdk/python/src python3 scripts/operator_live_smoke.py ["task goal"]
"""

import base64
import json
import os
import re
import sys
import urllib.request

import shinken
from shinken.operator import Decision, drive

GOAL = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get(
        "SHK_GOAL",
        "Look at the screen. If a dialog with an OK/Cancel button is visible, click OK. "
        "When nothing more is needed, say done.",
    )
)
ADDR = os.environ.get("SHK_ADDR", "127.0.0.1:8765")
TOKEN = os.environ.get("SHK_TOKEN")
BASE = (os.environ.get("SHK_SMOKE_MODEL_BASE_URL") or "").rstrip("/")
KEY = os.environ.get("SHK_SMOKE_MODEL_API_KEY") or ""
MODEL = os.environ.get("SHK_SMOKE_MODEL_NAME") or ""

VERBS = (
    "click|double_click|right_click|move (need x,y); type_text (need text); "
    "key (need keys, e.g. 'ctrl+s'); wait (need ms); done"
)


def _parse_action(text: str) -> dict | None:
    """Pull the last JSON object out of a (possibly reasoning-heavy) model reply."""
    for m in reversed(re.findall(r"\{[^{}]*\}", text or "")):
        try:
            return json.loads(m)
        except Exception:
            continue
    return None


def _to_aci(a: dict) -> dict | None:
    v = a.get("verb")
    if v in ("click", "double_click", "right_click", "move") and "x" in a and "y" in a:
        return {"verb": v, "target": {"kind": "point_px", "x": int(a["x"]), "y": int(a["y"])}}
    if v == "type_text" and a.get("text") is not None:
        return {"verb": "type_text", "text": str(a["text"])}
    if v == "key" and a.get("keys"):
        return {"verb": "key", "keys": str(a["keys"])}
    if v == "wait":
        return {"verb": "wait", "ms": int(a.get("ms", 200))}
    return None


class VisionAgent:
    """A generic vision model wrapped as an Operator agent (#6): screenshot → JSON action.

    Stateful: it carries a short history of the actions it has already taken, so a
    stateless reasoning model doesn't loop on the same step (e.g. re-clicking to focus
    instead of progressing to typing)."""

    def __init__(self) -> None:
        self.history: list[str] = []

    def decide(self, observation: dict) -> Decision:
        png = observation.get("png")
        b64 = base64.b64encode(png).decode() if png else ""
        size = observation.get("image", {}) or {}
        done_so_far = "; ".join(self.history) if self.history else "(none yet)"
        prompt = (
            f"You are driving a Linux GUI by issuing one discrete action at a time.\n"
            f"Goal: {GOAL}\n"
            f"The screen is {size.get('w', '?')}x{size.get('h', '?')} pixels; (0,0) is top-left.\n"
            f"Actions you have ALREADY taken this run (do not repeat finished steps): {done_so_far}.\n"
            f"Available verbs: {VERBS}.\n"
            f"Pick the NEXT action toward the goal. Reply with ONLY a single JSON object as "
            f'the last line, e.g. {{"verb":"click","x":640,"y":400}} or '
            f'{{"verb":"type_text","text":"hi"}} or {{"verb":"key","keys":"Return"}} or {{"done":true}}.'
        )
        content = [{"type": "text", "text": prompt}]
        if b64:
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}})
        # reasoning models spend many tokens thinking before the final JSON line, so the
        # budget must be generous (a low cap returns empty content mid-reasoning).
        max_tokens = int(os.environ.get("SHK_MAX_TOKENS", "4000"))
        body = {"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request(
            BASE + "/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            txt = (data["choices"][0]["message"].get("content")) or ""
            if isinstance(txt, list):
                txt = " ".join(p.get("text", "") for p in txt if isinstance(p, dict))
        except Exception:
            return Decision(actions=[], done=True, note="model call failed (sanitized)")
        action = _parse_action(txt)
        if not action or action.get("done"):
            return Decision(actions=[], done=True, note="model signaled done / no parseable action")
        aci = _to_aci(action)
        if aci is None:
            return Decision(actions=[], done=True, note="action not mappable to ACI; stopping")
        desc = aci["verb"]
        if "text" in aci:
            desc += f" {aci['text']!r}"
        elif "keys" in aci:
            desc += f" {aci['keys']}"
        elif aci.get("target"):
            desc += f" @({aci['target']['x']},{aci['target']['y']})"
        self.history.append(desc)
        if os.environ.get("SHK_DEBUG"):
            print("turn:", desc, file=sys.stderr)
        return Decision(actions=[aci], done=False)


def main() -> None:
    if not (TOKEN and BASE and KEY and MODEL):
        print(json.dumps({"status": "skipped", "reason": "missing SHK_ADDR/SHK_TOKEN or SHK_SMOKE_MODEL_* config"}))
        return
    env = shinken.connect(ADDR, token=TOKEN, record=True)
    try:
        res = drive(env, VisionAgent(), max_steps=int(os.environ.get("SHK_MAX_STEPS", "6")))
        path = env.save_replay(os.environ.get("SHK_SKN", "/tmp/operator_live.skn"))
    finally:
        env.close()
    print(json.dumps({"status": "ran", "goal": GOAL, "drive": res.to_dict(), "bundle": path}))


if __name__ == "__main__":
    main()
