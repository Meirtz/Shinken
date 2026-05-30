"""Measure live AT-SPI accessibility coverage (#80 / Spike A #2).

Runs inside the Shinken Linux image (Linux + python3-gi + a running AT-SPI bus). Walks
each named application's accessibility tree (or the whole desktop) and prints a JSON
coverage report + an aggregate — the first-party measurement that gates the
structured-first thesis (D3). Usage:

    python3 scripts/a11y_coverage.py [app_name ...]

With no args it measures the whole desktop; otherwise each arg filters to one app by
name (substring match).
"""

import json
import sys

from shinken.a11y import AtspiSource, aggregate, coverage_metrics

targets = sys.argv[1:] or [None]
reports: dict = {}
for app in targets:
    key = app or "desktop"
    try:
        reports[key] = coverage_metrics(AtspiSource(app_name=app).tree())
    except Exception as exc:  # report cleanly; the harness must not crash the run
        reports[key] = {"error": type(exc).__name__, "nodes": 0}
print(json.dumps({"per_app": reports, "summary": aggregate(reports)}, indent=2))
