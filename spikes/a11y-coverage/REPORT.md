# Spike #2 (E5) — a11y-coverage + tree-diff-bandwidth sweep (REPORT)

**Issue:** #2 · **Decision it gates:** [D3](../../docs/design/tech-decisions.md) (structured-default
observation) · **Harness:** `scripts/a11y_coverage.py`, `scripts/cdp_smoke.py`, `shinken.a11y`,
`shinken.cdp` · **Companion doc:** [`../../docs/engineering/spike-a11y-coverage.md`](../../docs/engineering/spike-a11y-coverage.md)

This is the multi-app, first-party measurement the companion doc said was *still owed*: real
accessibility-coverage numbers across several toolkit/source classes, plus the tree-diff-vs-full-tree
-vs-screenshot byte comparison that the D3/D4 bandwidth claim needs. **Honesty is the deliverable** —
the result is *uneven*, and that is reported as-is. It does **not** clear the bar for a clean
"structured-default"; it argues for **hybrid per-window with pixel fallback**, consistent with the
field priors already recorded in the companion doc.

All numbers below were produced in a single throwaway container (`shinken/sandbox-a11y`, a separate
spike image layered on the lean `shinken/sandbox-linux` base) with a live Xvfb display + AT-SPI bus +
`shinkend`, on a Docker host (arm64). Raw machine output is checked in at
[`evidence.json`](evidence.json); the exact run is [`run.sh`](run.sh).

## Per-surface results

`pct_addressable` is the load-bearing number (the report's own framing): the fraction of nodes that
are *actionable* **and** carry **both** a name and a usable bounding box — i.e. usable as a stable
`element_ref` target without falling back to pixels.

| Surface | Toolkit / source | node_count | **pct_addressable** | pct_roled | pct_bbox | diff / full / screenshot bytes | measured? |
|---|---|---:|---:|---:|---:|---|:--:|
| GTK dialog (`zenity --info`) | GTK 3 → AT-SPI | 10 | **0.100** | 1.00 | 0.800 | — | yes |
| GTK app (`gnome-text-editor`, GTK 4) | GTK 4 → AT-SPI | 117 | **0.094** | 1.00 | 0.333 | **2 043 / 10 611 / 76 517** | yes |
| Qt app (`qtbase5-examples` calculator) | Qt 5 → AT-SPI | 31 | **0.871** | 1.00 | 0.935 | — | yes |
| Terminal (`xterm`, VTE/X11) | AT-SPI | 0* | **0.000** | — | 0.000 | — | yes (absent) |
| Chromium **shell** window | AT-SPI | 3 | **0.000** | 1.00 | 0.333 | — | yes |
| Chromium **page content** | **CDP** `Accessibility.getFullAXTree` + `DOMSnapshot` | 22 | **0.227** | 1.00 | 0.682 | — | yes |
| Canvas / WebGL page | (no semantic tree) | — | — | — | — | — | **NO — unmeasured** |
| Game / custom-rendered surface | (no semantic tree) | — | — | — | — | — | **NO — unmeasured** |

`*` xterm reports `nodes: 1` because the harness counts its synthetic `desktop frame` root; xterm
contributes **zero** real AT-SPI nodes (classic X11 `xterm` exposes no accessibility tree at all). The
desktop walk never lists `xterm` as an application — confirmed directly against the AT-SPI desktop.

### Reading each surface

- **Qt (calculator): the clean structured win.** 31 nodes, 100% roled, 93.5% with a usable bbox,
  **87.1% addressable** — almost every button is a named, box-positioned, actionable `element_ref`
  target. For a Qt widget app the structured fast path is unambiguously real. Caveat: Qt's AT-SPI
  bridge registers *lazily* — the tree is empty for several seconds after window-map and only
  publishes after a settle delay (`run.sh` sleeps 10 s; a shorter wait yields the empty 1-node root).
  This is a measurement-robustness footnote, not a coverage finding.
- **GTK (gnome-text-editor / zenity): roled but thinly addressable.** Both are 100% *roled* and
  zenity is 80% boxed, but `pct_addressable` is low: **9.4%** (editor) and **10.0%** (zenity). Two
  honest reasons: (1) most nodes are containers/labels/static text — named and boxed but not in the
  *actionable* role set, so they fail the addressable definition by design; (2) in GTK 4 many widgets
  do not expose screen `extents` until shown/interacted, so `pct_bbox` on the editor is only 33%. The
  *actionable controls that are present* are largely addressable (editor: 11 of 16 actionable nodes),
  but as a fraction of the whole tree the addressable share is small. The earlier companion-doc data
  point ("zenity: 16 nodes, 100% roled, 87.5% bbox") was a **richer dialog variant**; a plain
  `--info` dialog here is 10 nodes / 80% bbox. Crucially the prior doc reported roled/bbox but **not**
  `pct_addressable` for zenity — measured here it is **0.10**, far below what "fully-roled tree"
  rhetoric implied.
- **Chromium over AT-SPI: poor, as expected.** AT-SPI sees only the **chrome shell** (3–5 nodes:
  window/menubar frames), **0% addressable**, and **none of the page DOM**. This is the textbook
  reason CDP is the right browser path.
- **Chromium over CDP: rich tree, moderate addressable.** 22 page nodes, 100% roled, 68% boxed, with
  stable `backendDOMNodeId` provenance per node. `pct_addressable` is **0.227** — lower than Qt
  because the metric counts the whole tree (many `StaticText`/`RootWebArea` nodes are named+boxed but
  not *actionable*), but **every actionable control that the page labels is addressable** (the form's
  textboxes, checkbox, and buttons all resolved with exact bboxes + stable ids; see
  `cdp_coverage.sample_elements` in `evidence.json`). On a bare/unlabeled page (no `<label>`s),
  addressable drops further — `pct_addressable` is page-authoring-sensitive, not a browser ceiling.
  Note: forcing the AX tree on (CDP `Accessibility.enable`, or the `--force-renderer-accessibility`
  Chromium flag) is what made Chromium subsequently appear in AT-SPI at all.

## Tree-diff bandwidth (the D3/D4 number)

Two structured observations were captured around a real UI change (typing "Hello Shinken spike" into
`gnome-text-editor`), diffed with `shinken.a11y.diff_elements` / sized with `diff_size`, and compared
to a real `shinkend` screenshot of the same desktop:

| Payload | Bytes | vs full tree | vs screenshot |
|---|---:|---:|---:|
| **Diff** (added+removed+changed) | **2 043** | 19.3% | **2.7%** |
| **Full element tree** (124 elements) | **10 611** | 100% | **13.9%** |
| **Screenshot** (PNG, 1280×800, busy desktop) | **76 517** | 721% | 100% |

Diff composition: 10 added / 3 removed / 11 changed / 103 unchanged. So when a UI is *stable* between
turns, the structured diff is a small fraction of even the full tree, and ~**1–3%** of a screenshot.

A second diff over **Chromium/CDP** around a DOM mutation (set a field value + append a node) gave
**3 453 diff / 6 675 full bytes (51.7%)** — a *larger* relative diff, because the value-set forced a
relayout that shifted many bboxes and churned `StaticText` nodes (recorded in the interactive log,
not in `evidence.json`).

**Honest limitation found in the diff path:** clicking a Qt calculator button (which changes the
*displayed value*) produced a **38-byte / zero-change diff** — because `to_elements` captures
role/name/bbox/states but **not** the AT-SPI Text/Value interface, so a value-only change is
*invisible* to the current diff. The bandwidth win is real for structural/label changes; value-cell
edits would need the element schema to carry `value` before the diff reflects them.

## Verdict — what this means for D3

- **Structured fast path is REAL and strong for: Qt widget apps (AT-SPI, 87% addressable) and
  Chromium page content (CDP, rich roled+boxed+stable-id tree).** Where it applies, the bandwidth case
  holds decisively — a stable-frame diff is single-digit-% of a screenshot.
- **It is WEAK / partial for GTK** (fully roled but low addressable share + GTK 4 missing bboxes) —
  usable for the actionable controls present, not as a whole-tree substitute for pixels.
- **It is ABSENT for: classic terminals (xterm — zero nodes), the browser *shell* over AT-SPI, and
  (by prior, here UNMEASURED) canvas/WebGL and game/custom-rendered surfaces.** Those are structure-
  poor by construction → pixel fallback is mandatory.

This is the **hybrid per-window with pixel fallback** picture, *not* a clean structured-default. It is
consistent with the field priors in the companion doc (production CUA stacks ship a11y-first
per-window but fall back to pixel clicks on custom-rendered surfaces).

### Could not measure (explicitly)

- **Canvas / WebGL** and **game / custom-rendered** surfaces: not added to the spike image (no cheap,
  license-clean, headless-friendly sample on hand). Expected prior stands — structure-poor → pixel
  fallback — but it is **unmeasured here**; do not treat the prior as a first-party number.
- **Walk latency / `capture_ms`** across surfaces and a **token** (vs byte) comparison: not tabulated
  in this run; bytes are the measured proxy.
- **LibreOffice / file-manager / Electron** AT-SPI: not installed (kept the spike image lean); only
  the three toolkit classes above were swept.

## Decision status

**Spike #2 run; D3 stays *Provisional*.** The numbers do **not** clear a net "structured-default":
addressable coverage is excellent on one surface class (Qt) and the browser-via-CDP path, but low on
GTK and zero on terminal/canvas/game. The defensible decision is the **hybrid router** (structured
fast path where `pct_addressable` is high; pixel/SoM fallback elsewhere) that D3 already describes as
its fallback ladder — now backed by first-party numbers rather than a single dialog. Promoting D3 from
Provisional would require (a) canvas/game surfaces measured (even to confirm the zero), and (b) the
diff path carrying `value` so value-edits are not invisible.

## Reproduce

```bash
# from repo root; Docker required, no model endpoint, no network beyond the image build
docker build -f images/linux/Dockerfile      -t shinken/sandbox-linux .   # lean base
docker build -f images/linux/Dockerfile.a11y -t shinken/sandbox-a11y  .   # + chromium/GTK/Qt
bash spikes/a11y-coverage/run.sh                                          # full sweep -> JSON

# or by hand, inside a running container (Xvfb :0 + AT-SPI bus + shinkend):
PYTHONPATH=/opt/shinken/src python3 scripts/a11y_coverage.py zenity gnome-text-editor calculator xterm Chromium
# Chromium page content over CDP (start chromium with --remote-debugging-port=9222 first):
PYTHONPATH=/opt/shinken/src SHINKEN_CDP_HTTP_URL=http://127.0.0.1:9222 python3 scripts/cdp_smoke.py
```

The spike image installs `websockets>=13` via pip (the SDK's pinned floor; Debian's apt
`python3-websockets` is 10.4 and lacks `websockets.sync`/`asyncio`, which both the SDK client import
and the live CDP attach need). It is the **spike image only** — production `images/linux/Dockerfile`
is untouched and stays lean.
