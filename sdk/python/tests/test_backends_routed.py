"""Fixture tests for the CU↔BU routing layer — dispatch_action verb mapping, RoutedSession
routing + source provenance, loud degradation, and operator.drive over a routed session."""

from __future__ import annotations

import pytest

from shinken.backends import RoutedSession, dispatch_action, route_for_target
from shinken.operator import ScriptedAgent, drive
from shinken.providers.base import UnsupportedProviderOperation


class FakeCaps:
    def __init__(self, verbs, structured=False):
        self.verbs = verbs
        self.structured_observation = structured


class FakeSurface:
    """Minimal duck-typed backend: records calls, advertises a verb set."""

    def __init__(self, name, verbs, *, platform="x"):
        self.name = name
        self._verbs = verbs
        self._platform = platform
        self.calls: list[tuple] = []
        self.closed = False

    @property
    def capabilities(self):
        return FakeCaps(self._verbs, structured="observe" in self._verbs)

    @property
    def platform(self):
        return self._platform

    def observe(self, structured=False, **kw):
        self.calls.append(("observe", structured))
        return {"type": "observation", "tree_text": f"{self.name}-tree", "elements": []}

    def screenshot(self, **kw):
        self.calls.append(("screenshot",))
        return {"png": b"\x89PNG", "format": "png"}

    def click(self, x=None, y=None, *, ref=None, button="left", count=1, **kw):
        self.calls.append(("click", x, y, ref, button, count))
        return {"ok": True}

    def type_text(self, text, **kw):
        self.calls.append(("type_text", text))
        return {"ok": True}

    def key(self, keys, **kw):
        self.calls.append(("key", keys))
        return {"ok": True}

    def scroll(self, dx=0, dy=0, **kw):
        self.calls.append(("scroll", dx, dy))
        return {"ok": True}

    def launch_app(self, app, args=None, **kw):
        self.calls.append(("launch_app", app))
        return {"ok": True}

    def navigate(self, url):
        self.calls.append(("navigate", url))
        return {"ok": True, "url": url}

    def eval(self, expr):
        self.calls.append(("eval", expr))
        return "RESULT"

    def close(self):
        self.closed = True


def _cu():
    return FakeSurface(
        "cu", ["observe", "screenshot", "click", "type_text", "key", "scroll", "launch_app"]
    )


def _bu():
    return FakeSurface(
        "bu", ["observe", "screenshot", "click", "navigate", "eval"], platform="browser"
    )


# ---------------------------------------------------------------- dispatch_action


def test_dispatch_maps_pointer_family():
    s = _cu()
    dispatch_action(s, {"verb": "double_click", "target": {"kind": "point_px", "x": 1, "y": 2}})
    dispatch_action(s, {"verb": "right_click", "target": {"kind": "point_px", "x": 3, "y": 4}})
    assert ("click", 1, 2, None, "left", 2) in s.calls
    assert ("click", 3, 4, None, "right", 1) in s.calls


def test_dispatch_element_ref_and_type_and_key():
    s = _cu()
    dispatch_action(s, {"verb": "click", "target": {"kind": "element_ref", "ref": "e7"}})
    dispatch_action(s, {"verb": "type_text", "text": "hi"})
    dispatch_action(s, {"verb": "key", "keys": "ctrl+s"})
    assert ("click", None, None, "e7", "left", 1) in s.calls
    assert ("type_text", "hi") in s.calls and ("key", "ctrl+s") in s.calls


def test_dispatch_launch_app():
    s = _cu()
    dispatch_action(s, {"verb": "launch_app", "app": "xterm"})
    assert ("launch_app", "xterm") in s.calls


def test_dispatch_unadvertised_verb_raises():
    s = _cu()  # cu has no 'navigate'
    with pytest.raises(UnsupportedProviderOperation, match="navigate"):
        dispatch_action(s, {"verb": "navigate", "url": "https://x"})


# ---------------------------------------------------------------- route_for_target


def test_route_for_target():
    assert route_for_target("https://example.com") == "bu"
    assert route_for_target("about:blank") == "bu"
    assert route_for_target("Calculator") == "cu"
    assert route_for_target(None) == "cu"


# ---------------------------------------------------------------- RoutedSession


def test_routes_by_explicit_surface_and_tags_source():
    ws = RoutedSession({"cu": _cu(), "bu": _bu()}, default="cu")
    res = ws.act_batch(
        [
            {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}, "surface": "cu"},
            {"verb": "navigate", "url": "https://x", "surface": "bu"},
        ]
    )
    assert [r["source"] for r in res["results"]] == ["cu", "bu"]
    assert res["completed"] is True
    assert [(e["source"], e["verb"]) for e in ws.events] == [("cu", "click"), ("bu", "navigate")]


def test_navigate_implies_bu_without_explicit_surface():
    ws = RoutedSession({"cu": _cu(), "bu": _bu()}, default="cu")
    ws.act_batch([{"verb": "navigate", "url": "https://x"}])
    assert ws.events[-1]["source"] == "bu" and ws.active == "bu"


def test_active_surface_follows_last_action_for_observe():
    cu, bu = _cu(), _bu()
    ws = RoutedSession({"cu": cu, "bu": bu}, default="cu")
    ws.act_batch([{"verb": "navigate", "url": "https://x"}])  # active -> bu
    obs = ws.observe(structured=True)
    assert obs["source"] == "bu" and obs["tree_text"] == "bu-tree"


def test_observe_all_tags_every_surface():
    ws = RoutedSession({"cu": _cu(), "bu": _bu()})
    allobs = ws.observe_all(structured=True)
    assert allobs["cu"]["source"] == "cu" and allobs["bu"]["source"] == "bu"


def test_failed_action_stops_batch():
    ws = RoutedSession({"cu": _cu(), "bu": _bu()}, default="cu")
    # cu can't navigate -> dispatch raises -> entry ok=False, batch stops before the 2nd
    res = ws.act_batch(
        [
            {"verb": "navigate", "url": "https://x", "surface": "cu"},
            {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 1}, "surface": "cu"},
        ]
    )
    assert res["completed"] is False and len(res["results"]) == 1
    assert res["results"][0]["ok"] is False and "navigate" in res["results"][0]["error"]


def test_capabilities_union_and_per_source():
    ws = RoutedSession({"cu": _cu(), "bu": _bu()})
    cap = ws.capabilities
    assert "navigate" in cap.verbs and "scroll" in cap.verbs  # union
    assert set(cap.per_source) == {"cu", "bu"}
    assert "navigate" not in cap.per_source["cu"]  # honest per-surface breakdown


def test_unknown_surface_and_empty_raise():
    with pytest.raises(ValueError, match="at least one surface"):
        RoutedSession({})
    ws = RoutedSession({"cu": _cu()})
    with pytest.raises(KeyError, match="unknown surface"):
        ws.surface("bu")


def test_close_closes_all_surfaces():
    cu, bu = _cu(), _bu()
    RoutedSession({"cu": cu, "bu": bu}).close()
    assert cu.closed and bu.closed


def test_operator_drive_over_routed_session():
    cu, bu = _cu(), _bu()
    ws = RoutedSession({"cu": cu, "bu": bu}, default="cu")
    agent = ScriptedAgent(
        [
            [{"verb": "navigate", "url": "https://x"}],
            [{"verb": "click", "target": {"kind": "point_px", "x": 5, "y": 6}, "surface": "bu"}],
        ]
    )
    out = drive(ws, agent, max_steps=4)
    assert out.done and out.actions == 2
    assert ("navigate", "https://x") in bu.calls  # drove the BU surface through the loop
