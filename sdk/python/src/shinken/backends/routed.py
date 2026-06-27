"""Route one agent loop across multiple operation-layer backends — the CU↔BU composition.

A host like Codex.app runs desktop (CU) and browser (BU) side by side and picks per action;
:class:`RoutedSession` is that host layer for Shinken. It holds named surfaces (duck-typed
Sandboxes from :mod:`shinken.backends` or a native shinkend ``Sandbox``), dispatches each ACI
action to the chosen surface, and tags every action + observation with ``source`` provenance
so a trajectory records *which* backend served it. It quacks like a Sandbox (``observe`` +
``act_batch``), so :func:`shinken.operator.drive` runs over it unchanged.

Routing is explicit + honest, not magic: an action carries an optional ``"surface"`` key
(``"cu"`` / ``"bu"`` / any registered name); otherwise the active surface is used (switched by
``use(name)`` or by a ``navigate``/``eval`` action, which implies BU). :func:`route_for_target`
is the convenience classifier (a URL ⇒ the browser surface). Requesting an unregistered
surface, or an action whose surface does not advertise the verb, fails LOUDLY — the same
capability-degradation contract the backends use.

:func:`dispatch_action` is the standalone glue that turns a canonical ACI action dict (what
``shinken.dialect`` emits) into a duck-typed backend's method call; it is what lets
``operator.drive`` run over ANY backend, routed or single.
"""

from __future__ import annotations

from typing import Any

from ..providers.base import UnsupportedProviderOperation

__all__ = ["dispatch_action", "RoutedSession", "route_for_target"]

# canonical ACI verbs that imply the browser surface when no explicit surface is given
_BROWSER_VERBS = {"navigate", "eval"}


def _point(target: dict | None) -> tuple[int | None, int | None]:
    if not target:
        return None, None
    if target.get("kind") == "point_px":
        return int(target["x"]), int(target["y"])
    return None, None


def _ref(target: dict | None) -> str | None:
    return target.get("ref") if target and target.get("kind") == "element_ref" else None


def dispatch_action(env: Any, action: dict) -> dict:
    """Execute one canonical ACI action dict on a duck-typed backend Sandbox, returning the
    backend's result. Maps verb+target onto the backend's typed methods; a verb the backend's
    ``capabilities.verbs`` does not list raises ``UnsupportedProviderOperation`` (loud)."""
    verb = action.get("verb")
    caps = getattr(env, "capabilities", None)
    declared_verbs = getattr(caps, "verbs", None) if caps is not None else None
    advertised = set(declared_verbs or [])
    # Click variants map to click() with a button/count. ``move`` is deliberately NOT a
    # click variant: falling back to click would turn a harmless pointer move into an
    # unintended mutating action.
    base = {"double_click": "click", "right_click": "click"}.get(verb, verb)
    # A declared capability list is authoritative even when it is empty.  Only legacy
    # duck-typed backends that do not expose ``capabilities.verbs`` retain method-based
    # dispatch compatibility.
    if declared_verbs is not None and base not in advertised and verb not in advertised:
        raise UnsupportedProviderOperation(
            f"backend does not advertise verb {verb!r} (has: {sorted(advertised)})"
        )
    tgt = action.get("target")
    x, y = _point(tgt)
    ref = _ref(tgt)

    if verb in ("click", "double_click", "right_click"):
        button = "right" if verb == "right_click" else action.get("button", "left")
        count = 2 if verb == "double_click" else action.get("count", 1)
        return env.click(x=x, y=y, ref=ref, button=button, count=count)
    if verb == "move":
        move = getattr(env, "move", None)
        if not callable(move):
            raise UnsupportedProviderOperation("backend does not implement advertised verb 'move'")
        return move(x=x, y=y)
    if verb == "drag":
        tx, ty = _point(action.get("to"))
        return env.drag(x=x, y=y, to_x=tx, to_y=ty)
    if verb == "type_text":
        return env.type_text(action.get("text", ""))
    if verb == "key":
        return env.key(action.get("keys", action.get("combo", "")))
    if verb == "scroll":
        return env.scroll(dx=action.get("dx", 0), dy=action.get("dy", 0))
    if verb == "screenshot":
        return env.screenshot()
    if verb == "observe":
        return env.observe(structured=action.get("structured", False))
    if verb == "exec":
        return env.exec(action.get("argv"), shell=action.get("shell"))
    if verb in ("invoke_action", "set_value"):
        if not ref:
            raise ValueError(f"{verb} needs an element_ref target")
        return (
            env.invoke_action(ref, action.get("text"))
            if verb == "invoke_action"
            else env.set_value(ref, action.get("text", ""))
        )
    if verb == "launch_app":
        return env.launch_app(action.get("app", action.get("name", "")), args=action.get("args"))
    if verb == "navigate":
        return env.navigate(action["url"])
    if verb == "eval":
        return {"ok": True, "value": env.eval(action["expression"])}
    if verb in ("clipboard_get", "clipboard_set"):
        return (
            {"ok": True, "text": env.clipboard_get()}
            if verb == "clipboard_get"
            else env.clipboard_set(action.get("text", ""))
        )
    if verb in ("done", "wait"):
        return {"ok": True}
    raise UnsupportedProviderOperation(f"dispatch_action has no mapping for verb {verb!r}")


def route_for_target(target: str | None) -> str:
    """Convenience classifier: a URL (http/https/about:) ⇒ ``'bu'``, anything else ⇒ ``'cu'``.
    The host can override; routing is never tool-enforced."""
    if target and (target.startswith(("http://", "https://", "about:")) or "://" in target):
        return "bu"
    return "cu"


class RoutedSession:
    """Compose named surfaces (e.g. ``{"cu": desktop, "bu": browser}``) behind one
    Sandbox-shaped object the Operator loop drives, with per-action ``source`` provenance."""

    def __init__(self, surfaces: dict[str, Any], *, default: str | None = None) -> None:
        if not surfaces:
            raise ValueError("RoutedSession needs at least one surface")
        self._surfaces = dict(surfaces)
        self._default = default or next(iter(surfaces))
        if self._default not in self._surfaces:
            raise KeyError(f"default surface {self._default!r} not in {list(surfaces)}")
        self._active = self._default
        self.events: list[dict] = []  # provenance: one entry per dispatched action

    # -- surface selection -----------------------------------------------------------
    def surface(self, name: str) -> Any:
        try:
            return self._surfaces[name]
        except KeyError:
            raise KeyError(
                f"unknown surface {name!r}; registered: {sorted(self._surfaces)}"
            ) from None

    def use(self, name: str) -> None:
        """Set the active surface (used when an action carries no explicit ``surface``)."""
        self.surface(name)  # validate
        self._active = name

    @property
    def active(self) -> str:
        return self._active

    def _route(self, action: dict) -> str:
        name = action.get("surface")
        if name is None and action.get("verb") in _BROWSER_VERBS:
            name = "bu" if "bu" in self._surfaces else self._active
        return name or self._active

    # -- Sandbox-shaped surface (drive() drives this) --------------------------------
    @property
    def capabilities(self):
        """Union only the capabilities explicitly advertised by the routed surfaces.

        ``per_source`` carries each surface's own verb set. Targets and observation types are
        unions too: a routed session must not invent ``element_ref`` or ``tree`` support when
        every underlying surface is pixel-only.
        """
        from ..client import Capabilities

        verbs: set[str] = set()
        targets: set[str] = set()
        observation_types: set[str] = set()
        structured = False
        per_source: dict[str, list[str]] = {}
        for name, env in self._surfaces.items():
            c = getattr(env, "capabilities", None)
            sv = list(getattr(c, "verbs", []) or [])
            per_source[name] = sv
            verbs.update(sv)
            targets.update(getattr(c, "targets", []) or [])
            observation_types.update(getattr(c, "observation_types", []) or [])
            structured = structured or bool(getattr(c, "structured_observation", False))
        cap = Capabilities(
            schema_version=1,
            verbs=sorted(verbs),
            targets=sorted(targets),
            observation_types=sorted(observation_types),
            structured_observation=structured,
        )
        cap.per_source = per_source  # type: ignore[attr-defined]
        return cap

    @property
    def platform(self) -> str:
        return getattr(self._surfaces[self._active], "platform", "routed")

    def observe(self, structured: bool = False, *, surface: str | None = None, **kw: Any) -> dict:
        name = surface or self._active
        obs = self.surface(name).observe(structured=structured, **kw)
        obs = {**obs, "source": name}
        return obs

    def observe_all(self, structured: bool = False) -> dict[str, dict]:
        """Observe every surface (a CU+BU host often shows both)."""
        return {
            name: {**env.observe(structured=structured), "source": name}
            for name, env in self._surfaces.items()
        }

    def act_batch(self, actions: list[dict], *, batch_id: str | None = None) -> dict:
        """Dispatch an ordered batch, routing each action to its surface and tagging the
        result + provenance event with ``source``. A failed action stops the batch
        (``completed=False``), mirroring the shinkend act_batch contract."""
        results = []
        completed = True
        for i, action in enumerate(actions):
            name = self._route(action)
            self._active = name  # the acted surface becomes active (so next observe follows)
            clean = {k: v for k, v in action.items() if k != "surface"}
            try:
                res = dispatch_action(self.surface(name), clean)
                ok = res.get("ok", True) if isinstance(res, dict) else True
                entry = {
                    **(res if isinstance(res, dict) else {"result": res}),
                    "ok": ok,
                    "source": name,
                    "verb": action.get("verb"),
                }
            except Exception as exc:  # noqa: BLE001 — record + stop the batch, never crash the loop
                entry = {
                    "ok": False,
                    "source": name,
                    "verb": action.get("verb"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.events.append(
                {
                    "i": i,
                    "source": name,
                    "verb": action.get("verb"),
                    "ok": entry["ok"],
                    "batch_id": batch_id,
                }
            )
            results.append(entry)
            if not entry["ok"]:
                completed = False
                break
        return {"results": results, "completed": completed, "batch_id": batch_id}

    def close(self) -> None:
        for env in self._surfaces.values():
            try:
                env.close()
            except Exception:  # noqa: BLE001
                pass
