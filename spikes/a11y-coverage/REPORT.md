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

A second pass extended the sweep with the two rows the first pass explicitly flagged as missing:
a **canvas-UI page** (the whole interactive UI drawn inside one `<canvas>`, no DOM/ARIA — the
blind-spot case) and a real **Electron** app (npm-prebuilt Electron 35.7.5, measured over *both*
AT-SPI and CDP). The canvas zero is now a **measured zero**, with a paired blind-spot probe showing
pixels changing while the structured diff reports nothing.

All numbers below were produced in a single throwaway container (`shinken/sandbox-a11y`, a separate
spike image layered on the lean `shinken/sandbox-linux` base) with a live Xvfb display + AT-SPI bus +
`shinkend`, on a Docker host (arm64). Raw machine output is checked in at
[`evidence.json`](evidence.json); the exact run is [`run.sh`](run.sh). The original five rows
reproduced **bit-identically** on the extended re-run.

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
| Electron app, renderer over **AT-SPI** (`--force-renderer-accessibility`) | Electron 35.7.5 → AT-SPI | 31 | **0.323** | 1.00 | 0.871 | — | yes |
| Electron **page content** | **CDP** (`--remote-debugging-port`) | 22 | **0.227** | 1.00 | 0.682 | — | yes |
| **Canvas-UI page** (5 controls drawn in one `<canvas>`) | **CDP** | 2 | **0.000** | 1.00 | 1.00 | **38 / 333 / 43 577**† | yes (**measured zero**) |
| Game / custom-rendered surface | (no semantic tree) | — | — | — | — | — | **NO — unmeasured** |

`*` xterm reports `nodes: 1` because the harness counts its synthetic `desktop frame` root; xterm
contributes **zero** real AT-SPI nodes (classic X11 `xterm` exposes no accessibility tree at all). The
desktop walk never lists `xterm` as an application — confirmed directly against the AT-SPI desktop
(the recorded bus registry is `calculator, zenity, Chromium, gnome-text-editor, Chromium, electron`).

`†` the canvas byte triple is the **blind-spot probe** (next section): structured diff bytes /
full-tree bytes / screenshot bytes around a real click on a canvas-drawn button.

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
- **Electron: measurable on BOTH paths — but only because accessibility was forced on.** A real
  Electron 35.7.5 app (one `BrowserWindow` loading the same labeled form as the chromium row,
  prebuilt linux binary pulled by `npm install` at image build, launched with `--no-sandbox`)
  worked first try inside the container — no missing-library or sandboxing blocker. Over **CDP**
  (`--remote-debugging-port=9224`) it is byte-for-byte the chromium-page shape: 22 nodes, 100%
  roled, 68% boxed, `pct_addressable` **0.227**, and again **every labeled control resolved** with
  exact bboxes + stable `backendDOMNodeId`s. Over **AT-SPI** — *with* `--force-renderer-accessibility`
  — Electron registers on the bus (as `electron`) and, unlike the plain Chromium shell row, publishes
  the **renderer DOM content** too: 31 nodes, 100% roled, 87.1% bbox, **0.323 addressable** (all 10
  actionable nodes addressable). The honest caveat cuts the other way from canvas: an Electron app
  launched *without* that flag (the default for most shipped Electron apps, absent an AT
  trigger) would look like the 3-node Chromium shell — so the AT-SPI number is an upper bound that
  the runtime can only reach when it controls the launch or an equivalent accessibility trigger.
- **Canvas-UI page: the measured zero.** A self-contained page ([`canvas_app.html`](canvas_app.html))
  draws a full interactive account form — 2 text fields, a checkbox, 2 buttons, a click counter —
  entirely inside one `<canvas>` with the 2D context, click regions hit-tested in JS, no DOM/ARIA
  fallback (the honest default shape of games, design tools, and CanvasKit-style apps). The CDP AX
  tree for that page is **2 nodes**: the `RootWebArea` and one **unnamed** `Canvas` element covering
  the viewport. Zero actionable, zero named controls, `pct_addressable` **0.000** — while the page
  visually renders 5 working controls. The expected prior is now a first-party number.

## Canvas blind spot — pixels change, the tree does not

The zero coverage row understates the problem, so the run probes it directly: capture the CDP
element list, **click the canvas-drawn "Sign in" button** (a real XTEST click through the live
display), and capture again. The UI visibly repaints (click counter increments, status line appears):

| Channel | Before | After | Change detected? |
|---|---:|---:|---|
| Screenshot (PNG, kiosk 1280×800) | 43 577 B | 45 477 B | **yes** (pixel hash differs) |
| Structured element list (CDP) | 2 nodes / 333 B | 2 nodes / 333 B | **no** — diff = 0 added / 0 removed / 0 changed (38 B empty payload) |

This is the quantified blind spot: on a canvas surface the structured channel is not merely
*sparse*, it is **silent across real state changes** — an agent relying on tree diffs would never
see the click land. Pixel observation is not a fallback here; it is the only channel.

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
  Chromium-family page content (CDP — both the browser and Electron, rich roled+boxed+stable-id
  trees).** Where it applies, the bandwidth case holds decisively — a stable-frame diff is
  single-digit-% of a screenshot.
- **Electron is a CDP surface, not an AT-SPI surface, in practice.** Its renderer tree does reach
  AT-SPI (0.32 addressable) — but only under `--force-renderer-accessibility`, which a runtime can
  only guarantee when it owns the launch; CDP needs the same kind of launch control
  (`--remote-debugging-port`) but returns the richer, stable-id tree.
- **It is WEAK / partial for GTK** (fully roled but low addressable share + GTK 4 missing bboxes) —
  usable for the actionable controls present, not as a whole-tree substitute for pixels.
- **It is ABSENT for: classic terminals (xterm — zero nodes), the browser *shell* over AT-SPI, and
  — now MEASURED, not just a prior — canvas-rendered UI (2 inert nodes for 5 working controls, and
  a click that repaints the screen produces a zero-entry tree diff).** Structure-poor by
  construction → pixel fallback is mandatory, and on canvas it is the *only* channel that even
  detects change.

This is the **hybrid per-window with pixel fallback** picture, *not* a clean structured-default. It is
consistent with the field priors in the companion doc (production CUA stacks ship a11y-first
per-window but fall back to pixel clicks on custom-rendered surfaces).

### Could not measure (explicitly)

- **Game / custom-rendered (non-browser) surfaces**: still no license-clean, headless-friendly
  sample in the image. The canvas row is the closest measured proxy (and confirms the zero), but a
  native game/GL surface remains a prior, not a first-party number.
- **WebGL specifically**: the canvas row uses the 2D context; a WebGL context is expected to be
  identical from the AX tree's perspective (same lone `<canvas>` element) but was not separately
  measured.
- **Walk latency / `capture_ms`** across surfaces and a **token** (vs byte) comparison: not tabulated
  in this run; bytes are the measured proxy.
- **LibreOffice / file-manager** AT-SPI: not installed (kept the spike image lean).

## Decision status

**Spike #2 run (incl. the canvas + Electron extension); D3 stays *Provisional*.** The numbers do
**not** clear a net "structured-default": addressable coverage is excellent on one surface class (Qt)
and the Chromium-family-via-CDP path (browser and Electron alike), but low on GTK and **zero —
measured, with a change-blind diff — on terminal/canvas**. The defensible decision is the **hybrid
router** (structured fast path where `pct_addressable` is high; pixel/SoM fallback elsewhere) that D3
already describes as its fallback ladder — now backed by first-party numbers rather than a single
dialog. Of the two conditions previously named for promoting D3 from Provisional, (a) is now
substantially met — the canvas zero is measured (a game/native-GL surface remains the open sliver) —
while (b), the diff path carrying `value` so value-edits are not invisible, is still open.

## Reproduce

```bash
# from repo root; Docker required, no model endpoint, no network beyond the image build
docker build -f images/linux/Dockerfile      -t shinken/sandbox-linux .   # lean base
docker build -f images/linux/Dockerfile.a11y -t shinken/sandbox-a11y  .   # + chromium/GTK/Qt/Electron
bash spikes/a11y-coverage/run.sh                                          # full sweep -> JSON

# or by hand, inside a running container (Xvfb :0 + AT-SPI bus + shinkend):
PYTHONPATH=/opt/shinken/src python3 scripts/a11y_coverage.py zenity gnome-text-editor calculator xterm Chromium electron
# Chromium-family page content over CDP (chromium :9222, canvas page :9223, Electron :9224):
PYTHONPATH=/opt/shinken/src SHINKEN_CDP_HTTP_URL=http://127.0.0.1:9222 python3 scripts/cdp_smoke.py
```

The spike image installs `websockets>=13` via pip (the SDK's pinned floor; Debian's apt
`python3-websockets` is 10.4 and lacks `websockets.sync`/`asyncio`, which both the SDK client import
and the live CDP attach need), plus `nodejs`/`npm` and an `npm install`ed Electron 35.7.5 (the
prebuilt binary downloads at image build, so the run itself stays network-free). It is the **spike
image only** — production `images/linux/Dockerfile` is untouched and stays lean.
