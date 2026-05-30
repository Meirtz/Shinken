"""Accessibility-coverage harness (#80 / Spike A #2).

The load-bearing question behind the structured-first thesis (D3): *how much usable
structure do real apps expose over accessibility?* This measures it — a normalized
node tree plus coverage metrics (how many nodes carry a role, a name, a bounding box,
and are actionable / addressable as an `element_ref` target). The pure metrics work on
any tree, so they're unit-tested offline; :class:`AtspiSource` reads a live AT-SPI tree
(Linux, via ``python3-gi``) and is exercised by the in-image coverage run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

#: Roles an agent can typically act on (used for the "actionable" coverage metric).
ACTIONABLE_ROLES = {
    "push button",
    "button",
    "toggle button",
    "menu item",
    "menu",
    "link",
    "text",
    "entry",
    "password text",
    "check box",
    "radio button",
    "combo box",
    "list item",
    "page tab",
    "tab",
    "slider",
    "spin button",
}


@dataclass
class A11yNode:
    """A normalized accessibility node — the cross-toolkit shape a real AT-SPI/UIA/AX
    walk reduces to (mirrors the ACI `Element` contract)."""

    role: str
    name: str = ""
    bbox: tuple[int, int, int, int] | None = None  # (x, y, w, h)
    children: list[A11yNode] = field(default_factory=list)


def iter_nodes(root: A11yNode) -> Iterable[A11yNode]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.children)


def _depth(node: A11yNode, d: int = 1) -> int:
    return max((_depth(c, d + 1) for c in node.children), default=d)


def coverage_metrics(root: A11yNode) -> dict:
    """Coverage of one app's accessibility tree — the spike's per-app measurement.

    ``pct_addressable`` is the key number: the fraction of nodes that are actionable
    **and** carry both a name and a usable bounding box, i.e. usable as stable
    ``element_ref`` targets without falling back to pixels."""
    nodes = list(iter_nodes(root))
    total = len(nodes)

    def pct(k: int) -> float:
        return round(k / total, 4) if total else 0.0

    named = sum(1 for n in nodes if n.name.strip())
    roled = sum(1 for n in nodes if n.role and n.role != "unknown")
    boxed = sum(1 for n in nodes if n.bbox and n.bbox[2] > 0 and n.bbox[3] > 0)
    actionable = sum(1 for n in nodes if n.role in ACTIONABLE_ROLES)
    addressable = sum(
        1
        for n in nodes
        if n.role in ACTIONABLE_ROLES and n.name.strip() and n.bbox and n.bbox[2] > 0
    )
    return {
        "nodes": total,
        "named": named,
        "pct_named": pct(named),
        "roled": roled,
        "pct_roled": pct(roled),
        "with_bbox": boxed,
        "pct_bbox": pct(boxed),
        "actionable": actionable,
        "pct_actionable": pct(actionable),
        "addressable": addressable,
        "pct_addressable": pct(addressable),
        "max_depth": _depth(root),
    }


def aggregate(reports: dict[str, dict]) -> dict:
    """Roll up per-app coverage into a single spike summary (means across apps)."""
    apps = [r for r in reports.values() if r.get("nodes")]
    n = len(apps)

    def mean(key: str) -> float:
        return round(sum(a[key] for a in apps) / n, 4) if n else 0.0

    return {
        "apps_measured": n,
        "total_nodes": sum(a["nodes"] for a in apps),
        "mean_pct_roled": mean("pct_roled"),
        "mean_pct_named": mean("pct_named"),
        "mean_pct_bbox": mean("pct_bbox"),
        "mean_pct_actionable": mean("pct_actionable"),
        "mean_pct_addressable": mean("pct_addressable"),
    }


class AtspiSource:
    """Live AT-SPI tree source (Linux) via ``python3-gi`` (``gi.repository.Atspi``).

    Imported lazily so this module loads anywhere; only the in-image coverage run needs
    ``gi`` + a running accessibility bus. ``tree()`` walks the desktop, optionally
    filtered to one application by name, into normalized :class:`A11yNode`s.
    """

    def __init__(self, app_name: str | None = None, max_nodes: int = 5000):
        self.app_name = app_name
        self.max_nodes = max_nodes

    def tree(self) -> A11yNode:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore

        Atspi.init()
        desktop = Atspi.get_desktop(0)
        root = A11yNode(role="desktop frame", name="desktop")
        self._count = 0
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is None:
                continue
            if self.app_name and self.app_name.lower() not in (app.get_name() or "").lower():
                continue
            root.children.append(self._walk(app))
        return root

    def _walk(self, acc) -> A11yNode:
        node = A11yNode(role=_role(acc), name=acc.get_name() or "", bbox=_bbox(acc))
        if self._count >= self.max_nodes:
            return node
        for i in range(acc.get_child_count()):
            child = acc.get_child_at_index(i)
            if child is None:
                continue
            self._count += 1
            node.children.append(self._walk(child))
            if self._count >= self.max_nodes:
                break
        return node


def _role(acc) -> str:
    try:
        return acc.get_role_name() or "unknown"
    except Exception:
        return "unknown"


def _bbox(acc) -> tuple[int, int, int, int] | None:
    try:
        comp = acc.get_component_iface()
        if comp is None:
            return None
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi  # type: ignore

        ext = comp.get_extents(Atspi.CoordType.SCREEN)
        return (ext.x, ext.y, ext.width, ext.height)
    except Exception:
        return None
