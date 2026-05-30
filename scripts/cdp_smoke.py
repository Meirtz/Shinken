"""Smoke the CDP browser observation backend against a live Chromium (#79).

Attaches to a Chromium-based browser started with a remote debugging port, captures the
accessibility tree over CDP, normalizes it into ACI ``Element``s, and prints a coverage
report plus a few sample elements (role / name / bbox / provenance). This is the
browser analogue of ``a11y_coverage.py`` (AT-SPI). Start a target first, e.g.::

    chromium --headless=new --remote-debugging-port=9222 https://example.com

then::

    python3 scripts/cdp_smoke.py                      # default http://127.0.0.1:9222
    SHINKEN_CDP_HTTP_URL=http://127.0.0.1:9333 python3 scripts/cdp_smoke.py
    SHINKEN_CDP_WS_URL=ws://127.0.0.1:9222/devtools/page/ABC python3 scripts/cdp_smoke.py

Reads only a local debugger endpoint; no credentials or external services involved.
"""

import json
import os
import sys

from shinken.a11y import coverage_metrics, to_elements
from shinken.cdp import CdpSource

src = CdpSource(
    ws_url=os.environ.get("SHINKEN_CDP_WS_URL"),
    http_url=os.environ.get("SHINKEN_CDP_HTTP_URL", "http://127.0.0.1:9222"),
)

try:
    tree = src.tree()  # one CDP round-trip; derive both elements and coverage from it
except Exception as exc:  # no browser / wrong port — report cleanly, don't crash
    print(json.dumps({"source": src.source_name, "available": False, "error": type(exc).__name__}))
    sys.exit(1)

elements = to_elements(tree, source=src.source_name)
samples = [e for e in elements if e.get("name") and e["bbox"][2] > 0][:8]
report = {
    "source": src.source_name,
    "available": True,
    "node_count": len(elements),
    "coverage": coverage_metrics(tree),
    "sample_elements": [
        {
            "ref": e["ref"],
            "role": e["role"],
            "name": e.get("name"),
            "bbox": e["bbox"],
            "provenance": e.get("provenance"),
        }
        for e in samples
    ],
}
print(json.dumps(report, indent=2))
