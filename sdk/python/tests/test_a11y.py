"""a11y coverage metrics (#80 / Spike A #2) — pure metrics on synthetic trees (offline)."""

from __future__ import annotations

from shinken.a11y import A11yNode, aggregate, coverage_metrics, iter_nodes


def _tree() -> A11yNode:
    return A11yNode(
        "frame",
        "Win",
        (0, 0, 800, 600),
        children=[
            A11yNode("push button", "OK", (10, 10, 80, 30)),
            A11yNode("entry", "Search", (10, 50, 200, 30)),
            A11yNode("label", "Status", (10, 90, 200, 20)),  # named, not actionable
            A11yNode("push button", "", (10, 120, 80, 30)),  # actionable, unnamed
            A11yNode(
                "panel",
                "",
                None,  # no bbox
                children=[A11yNode("menu item", "File", (0, 0, 40, 20))],
            ),
        ],
    )


def test_iter_nodes_counts_whole_tree():
    assert len(list(iter_nodes(_tree()))) == 7


def test_coverage_metrics():
    m = coverage_metrics(_tree())
    assert m["nodes"] == 7
    assert m["roled"] == 7  # all have a real role
    assert m["named"] == 5  # Win, OK, Search, Status, File
    assert m["actionable"] == 4  # OK, Search(entry), unnamed button, File(menu item)
    assert m["with_bbox"] == 6  # all but the panel
    assert m["addressable"] == 3  # OK, Search, File (unnamed button excluded — no name)
    assert m["max_depth"] == 3  # frame -> panel -> menu item
    assert 0.0 <= m["pct_addressable"] <= 1.0


def test_empty_and_leaf_trees():
    leaf = coverage_metrics(A11yNode("button", "Go", (0, 0, 10, 10)))
    assert leaf["nodes"] == 1 and leaf["pct_addressable"] == 1.0 and leaf["max_depth"] == 1
    bare = coverage_metrics(A11yNode("unknown"))
    assert bare["roled"] == 0 and bare["pct_named"] == 0.0


def test_aggregate_means_across_apps():
    a = coverage_metrics(_tree())
    b = coverage_metrics(A11yNode("button", "Go", (0, 0, 10, 10)))
    agg = aggregate({"app_a": a, "app_b": b, "empty": {"nodes": 0}})
    assert agg["apps_measured"] == 2  # the empty app is excluded
    assert agg["total_nodes"] == a["nodes"] + b["nodes"]
    assert 0.0 <= agg["mean_pct_addressable"] <= 1.0
