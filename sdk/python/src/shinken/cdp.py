"""CDP (Chrome DevTools Protocol) browser observation backend (#79).

Browsers and Electron apps are a major computer-use surface, and they often expose a
*richer* structured tree over CDP's ``Accessibility`` domain than they do over the OS
accessibility bus (AT-SPI/UIA/AX) — so for Chromium targets CDP is the preferred
structured source, while AT-SPI remains the cross-toolkit default for native apps.

This module reduces a CDP ``Accessibility.getFullAXTree`` response (optionally enriched
with box bounds from ``DOMSnapshot.captureSnapshot``) into the same normalized
:class:`~shinken.a11y.A11yNode` tree the AT-SPI path produces, so element_ref resolution
(#78) and coverage metrics (#80) share one vocabulary. The pure
normalization (:func:`parse_ax_tree`) is unit-tested against CDP fixtures offline;
:class:`CdpSource` attaches to a live Chromium over a debugger websocket and is exercised
by ``scripts/cdp_smoke.py``. Backend AX/DOM node ids are carried as Element
``provenance`` so a node can be re-resolved against the live page.
"""

from __future__ import annotations

import json
import urllib.request

from .a11y import A11yNode

#: CDP AX property name -> normalized state token (matches the AT-SPI vocabulary where
#: it overlaps, so downstream consumers see one state set regardless of backend).
_STATE_PROPS = {
    "focusable": "focusable",
    "focused": "focused",
    "selected": "selected",
    "checked": "checked",
    "editable": "editable",
    "expanded": "expanded",
    "required": "required",
}


def _prop_truthy(value: dict) -> bool:
    """A CDP property value (``{"type": ..., "value": ...}``) is truthy when its value
    is ``True`` or a token like ``"true"`` / ``"mixed"`` (tristate checkboxes)."""
    v = value.get("value") if isinstance(value, dict) else value
    return v is True or (isinstance(v, str) and v.lower() in ("true", "mixed"))


def _states(node: dict) -> list[str]:
    states: list[str] = []
    disabled = False
    for prop in node.get("properties", []) or []:
        name = prop.get("name")
        val = prop.get("value", {})
        if name == "disabled":
            disabled = _prop_truthy(val)
        elif name in _STATE_PROPS and _prop_truthy(val):
            states.append(_STATE_PROPS[name])
    if not disabled:
        states.append("enabled")
    return states


def _str(field: dict | None) -> str:
    """Read a CDP ``AXValue`` (``{"value": "..."}``) as a plain string."""
    if isinstance(field, dict):
        v = field.get("value")
        if isinstance(v, str):
            return v
    return ""


def _provenance(node: dict) -> dict:
    prov: dict = {"ax_node_id": str(node.get("nodeId"))}
    if "backendDOMNodeId" in node:
        prov["backend_dom_node_id"] = node["backendDOMNodeId"]
    return prov


def bounds_from_snapshot(snapshot: dict) -> dict[int, tuple[int, int, int, int]]:
    """Extract ``{backendNodeId: (x, y, w, h)}`` from a ``DOMSnapshot.captureSnapshot``
    result, so AX nodes (which carry a ``backendDOMNodeId``) can be given a bbox.

    Bounds are document/CSS pixels (best-effort; a device-pixel-ratio scale may be
    needed to line up exactly with screenshots — left to the operator)."""
    out: dict[int, tuple[int, int, int, int]] = {}
    for doc in snapshot.get("documents", []) or []:
        backend = doc.get("nodes", {}).get("backendNodeId", []) or []
        layout = doc.get("layout", {}) or {}
        node_index = layout.get("nodeIndex", []) or []
        boxes = layout.get("bounds", []) or []
        for i, ni in enumerate(node_index):
            if i >= len(boxes):
                break
            if 0 <= ni < len(backend) and len(boxes[i]) >= 4:
                x, y, w, h = boxes[i][:4]
                out[backend[ni]] = (int(x), int(y), int(w), int(h))
    return out


def parse_ax_tree(
    ax_nodes: list[dict],
    bounds: dict[int, tuple[int, int, int, int]] | None = None,
    *,
    include_ignored: bool = False,
) -> A11yNode:
    """Normalize a CDP ``Accessibility.getFullAXTree`` node list into an
    :class:`A11yNode` tree (role/name/bbox/states + AX/DOM-id provenance).

    The tree is reconstructed from each node's ``childIds``. ``ignored`` AX nodes
    (presentational wrappers) are transparent by default — they are dropped and their
    children are hoisted to the nearest kept ancestor, matching how assistive tech sees
    the page; pass ``include_ignored=True`` to keep them. A bbox is attached when the
    node's ``backendDOMNodeId`` is present in ``bounds``."""
    bounds = bounds or {}
    by_id = {str(n.get("nodeId")): n for n in ax_nodes if n.get("nodeId") is not None}
    child_ids = {str(c) for n in ax_nodes for c in (n.get("childIds") or [])}
    roots = [str(n["nodeId"]) for n in ax_nodes if str(n.get("nodeId")) not in child_ids]

    def build(node_id: str, seen: set[str]) -> list[A11yNode]:
        if node_id in seen:  # cycle guard
            return []
        seen.add(node_id)
        node = by_id.get(node_id)
        if node is None:
            return []
        kids: list[A11yNode] = []
        for cid in node.get("childIds") or []:
            kids.extend(build(str(cid), seen))
        if node.get("ignored") and not include_ignored:
            return kids  # transparent: hoist children up
        bid = node.get("backendDOMNodeId")
        a = A11yNode(
            role=_str(node.get("role")) or "unknown",
            name=_str(node.get("name")),
            bbox=bounds.get(bid) if bid is not None else None,
            states=_states(node),
            children=kids,
            provenance=_provenance(node),
        )
        return [a]

    seen: set[str] = set()
    tops: list[A11yNode] = []
    for r in roots:
        tops.extend(build(r, seen))
    if len(tops) == 1:
        return tops[0]
    # forest (or the document root was ignored) — wrap in a synthetic document node
    return A11yNode(role="document", name="", children=tops)


class CdpSource:
    """Live CDP accessibility source for a Chromium-based target (an :class:`A11ySource`).

    Attaches to a page's debugger websocket (``ws_url``, or auto-discovered from the
    ``http://host:port/json`` endpoint of a browser started with
    ``--remote-debugging-port``), pulls the full AX tree, optionally enriches it with
    ``DOMSnapshot`` bounds, and normalizes via :func:`parse_ax_tree`. ``websockets`` is
    imported lazily so this module loads without a browser present; a failure to attach
    raises out of :meth:`tree`, which :func:`shinken.a11y.observe_structured` turns into a
    graceful ``available=False`` (the screenshot path is unaffected)."""

    source_name = "cdp"

    def __init__(
        self,
        ws_url: str | None = None,
        http_url: str = "http://127.0.0.1:9222",
        *,
        with_bounds: bool = True,
        include_ignored: bool = False,
        timeout: float = 5.0,
        max_size: int = 16 * 1024 * 1024,
    ):
        self.ws_url = ws_url
        self.http_url = http_url.rstrip("/")
        self.with_bounds = with_bounds
        self.include_ignored = include_ignored
        self.timeout = timeout
        self.max_size = max_size
        self._id = 0

    def discover_ws_url(self) -> str:
        """Resolve a page target's ``webSocketDebuggerUrl`` from ``{http_url}/json``."""
        with urllib.request.urlopen(f"{self.http_url}/json", timeout=self.timeout) as r:
            targets = json.loads(r.read().decode("utf-8"))
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        chosen = pages or [t for t in targets if t.get("webSocketDebuggerUrl")]
        if not chosen:
            raise RuntimeError("no CDP target with a webSocketDebuggerUrl")
        return chosen[0]["webSocketDebuggerUrl"]

    def _call(self, ws, method: str, params: dict | None = None) -> dict:
        import time as _time

        from websockets.exceptions import ConnectionClosed

        self._id += 1
        call_id = self._id
        ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        deadline = _time.monotonic() + self.timeout
        while True:  # skip CDP events (no matching id) until our response arrives
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} timed out after {self.timeout}s")
            try:
                raw = ws.recv(timeout=remaining)
            except ConnectionClosed as exc:  # dead/half-open debugger socket — fail fast
                raise RuntimeError(f"CDP websocket closed during {method}") from exc
            msg = json.loads(raw)
            if msg.get("id") != call_id:
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP {method} failed: {msg['error'].get('message')}")
            return msg.get("result", {})

    def tree(self) -> A11yNode:
        from websockets.sync.client import connect  # lazy: only needed for a live attach

        ws_url = self.ws_url or self.discover_ws_url()
        with connect(ws_url, open_timeout=self.timeout, max_size=self.max_size) as ws:
            self._call(ws, "Accessibility.enable")
            ax = self._call(ws, "Accessibility.getFullAXTree")
            bounds: dict[int, tuple[int, int, int, int]] = {}
            if self.with_bounds:
                try:
                    self._call(ws, "DOMSnapshot.enable")
                    snap = self._call(ws, "DOMSnapshot.captureSnapshot", {"computedStyles": []})
                    bounds = bounds_from_snapshot(snap)
                except Exception:
                    bounds = {}  # bounds are best-effort; role/name tree is still useful
        return parse_ax_tree(ax.get("nodes", []), bounds, include_ignored=self.include_ignored)
