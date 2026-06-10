# Spike A — accessibility / structured-observation coverage

**Issue:** #2 · **Decision it grounds:** [D3](../design/tech-decisions.md) ·
**Harness:** `scripts/a11y_coverage.py`, `shinken.a11y`, `shinken.cdp`

## The question

D3 says an agent should *upgrade* to a normalized **element tree** (role + name + bbox + an
addressable `ref`) whenever the UI exposes one, while screenshots remain the universal baseline.
That upgrade is only worth building if real apps expose *enough usable structure*. Spike A measures
that, and v0.0.1 ships the tested reference path so D3 is grounded in first-party data rather than
vendor anchors.

A low or uneven coverage result is a *valid* outcome: it scopes the structured fast path, it does
not fail v0.0.1.

## What was built (the reference path)

| Capability | Module | Issue |
|------------|--------|-------|
| AT-SPI tree → normalized ACI `Element` (role/name/bbox/states) | `shinken/a11y.py` (`AtspiSource`, `to_elements`) | #77 |
| `element_ref` resolution + semantic action routing (`observe → resolve → act_on`) | `shinken/client.py` | #78 |
| CDP browser backend (`Accessibility.getFullAXTree` + `DOMSnapshot` bounds → `Element`) | `shinken/cdp.py` (`CdpSource`) | #79 |
| Coverage harness + metrics (roled / named / bbox / actionable / **addressable**) | `shinken/a11y.py` (`coverage_metrics`), `scripts/a11y_coverage.py` | #80 |

All backends normalize to the **same** `Element` shape, so coverage metrics, `element_ref`
resolution, and `.skn` replay are backend-agnostic (`source` records which backend produced a node;
`provenance` carries the backend-native id). See
[observation-backends.md](../design/observation-backends.md) for when to prefer CDP over AT-SPI.

## The metric

`coverage_metrics(tree)` reports, per app, the fraction of nodes that are *roled*, *named*, carry a
*usable bbox*, are *actionable* (a clickable/editable role), and — the load-bearing number —
**`pct_addressable`**: actionable **and** named **and** box-positioned, i.e. usable as a stable
`element_ref` target without falling back to pixels. The machine-readable report is the JSON emitted
by `scripts/a11y_coverage.py`; this document is the Markdown summary the spike requires.

## First-party measurement

> **Multi-app sweep done (E5).** The single-app gap called out below has been filled: a multi-surface
> coverage + tree-diff-bandwidth sweep is recorded in
> [`../../spikes/a11y-coverage/REPORT.md`](../../spikes/a11y-coverage/REPORT.md) (raw JSON in
> `spikes/a11y-coverage/evidence.json`, runner `spikes/a11y-coverage/run.sh`). Headline first-party
> numbers: Qt widget app **87.1% addressable** (AT-SPI), Chromium page content rich over **CDP**
> (AT-SPI sees only the shell, 0% addressable), GTK fully *roled* but **~10% addressable**, `xterm`
> exposes **zero** AT-SPI nodes; a stable-frame tree-diff is ~**1–3%** of a screenshot's bytes.
> Result is *uneven* → **hybrid per-window + pixel fallback**; **D3 stays Provisional**, spike #2 run
> but not gated. The numbers below are the original single-`zenity` data point that sweep extends.

Run live in the local Linux Sandbox (Docker image, Xvfb + AT-SPI bus) against a GTK dialog (`zenity`):

```json
{
  "app": "zenity",
  "source": "atspi",
  "nodes": 16,
  "pct_roled": 1.0,
  "pct_bbox": 0.875,
  "note": "16 nodes; every node carries a role; 14/16 carry a usable bounding box"
}
```

Reproduce with `python3 scripts/a11y_coverage.py zenity` inside the image. The harness emits the full
metric set (`named/pct_named`, `actionable/pct_actionable`, `addressable/pct_addressable`,
`max_depth`, …) for any tree.

**Reading the result:** a GTK toolkit dialog exposes a dense, fully-roled tree with usable geometry
on ~88% of nodes — so the structured path is real *for one native GTK surface*. This is a single app
and is NOT yet 'the evidence D3 needs': the load-bearing numbers — `pct_addressable` across the full
surface set, the token/byte size of a tree-diff vs a screenshot, walk latency, and the
pixel-escalation fraction on browser/Electron/canvas/game surfaces — remain unmeasured. D3's
structured-default upgrade stays Provisional and spike #2 stays ungated until the multi-app sweep
below produces those numbers (see [tech-decisions.md](../design/tech-decisions.md) D3,
[status.md](status.md)).

Note: the zenity row above reports roled/bbox coverage but not `pct_addressable` (the metric the
report itself calls load-bearing). The E5 sweep records it: a plain `zenity --info` dialog is **10
nodes, 100% roled, 80% bbox, but only `pct_addressable` = 0.10** (one actionable button). The "16
nodes / 87.5% bbox" above is a richer dialog variant — node count and bbox coverage depend on the
dialog type, and a fully-*roled* tree is **not** the same as a highly *addressable* one. See
[`../../spikes/a11y-coverage/REPORT.md`](../../spikes/a11y-coverage/REPORT.md).

## Per-surface coverage map

The target surface classes and the path each takes. "Measured" = first-party number recorded (E5 sweep,
[`../../spikes/a11y-coverage/REPORT.md`](../../spikes/a11y-coverage/REPORT.md)); "path ready" = backend
implemented + tested on fixtures, awaiting an in-image run with that app present.

| Surface class | Backend | Expected coverage | Status |
|---------------|---------|-------------------|--------|
| Qt widget toolkit (calculator) | AT-SPI (`atspi`) | high | **measured** — 31 nodes, 100% roled, 93.5% bbox, **87.1% addressable** (the clean structured win) |
| GTK toolkit (zenity dialog, gnome-text-editor / GTK 4) | AT-SPI (`atspi`) | high → **mixed** | **measured** — 100% roled but **9–10% addressable** (containers/labels dominate; GTK 4 widgets often lack screen bbox) |
| Chromium / Electron page content | CDP (`cdp`) | high (browser computes full AX tree) | **measured** (browser AND a real Electron 35 app — identical 22-node shape) — 100% roled, 68% bbox, stable DOM ids; addressable page-authoring-sensitive (`0.23` here) |
| Electron renderer over AT-SPI | AT-SPI (`atspi`) | medium, launch-gated | **measured** — 31 nodes, **0.32 addressable**, but only under `--force-renderer-accessibility`; without it, expect the Chromium-shell shape below |
| Chromium **shell** over AT-SPI | AT-SPI (`atspi`) | **low** | **measured** — only 3–5 chrome-shell nodes, **0% addressable**, no page DOM → use CDP for browser content |
| Terminal (xterm, VTE/X11) | AT-SPI (`atspi`) | **near-zero** | **measured** — **zero** AT-SPI nodes → pixel fallback |
| Canvas page (2D context) | CDP (`cdp`) | **near-zero** | **measured zero** — 5 drawn controls → 2 inert AX nodes, 0% addressable; a real click changes pixels while the tree diff reports **0 changes** (the blind spot is quantified) |
| WebGL / custom-rendered / gamelike surface | — | **near-zero** | classified → fallback (**unmeasured** in E5; the canvas row is the measured proxy) |

## Fallback thresholds (how the router chooses)

Coverage is uneven by design, so the structured path is a *fast path*, not the only path:

1. **`pct_addressable` high (toolkit, browser via CDP):** act on `element_ref` directly; cheapest and
   most robust. Capture the tree (full, or a diff once implemented) instead of pixels.
2. **Partial coverage (mixed native + custom widgets):** use `element_ref` where present; fall back to
   `point_px` from a screenshot for the unstructured regions.
3. **Low / near-zero coverage (canvas, WebGL, games, remote pixels):** skip the tree; use the
   screenshot baseline, region/zoom crops, or a Set-of-Marks / detector overlay (`som`, planned) to
   synthesize targets. The screenshot path is always available and never depends on a11y.

## Cost (size / latency / tokens)

`capture_ms` (in `observe_structured`) and the serialized element-list size are the measurement
vehicles for capture latency and bandwidth; the browser, Electron, and canvas surfaces are now in
the spike image and measured for *coverage* (E5), but the per-app size/latency/**token** table
across the classes is still the remaining sweep.

**Field priors to fold into the sweep (2026-06, public CUA stacks):** (a) production computer-use
drivers ship an a11y-first per-window ACI but fall back to pixel clicks on custom-rendered surfaces
(Blender/games/Electron) via a small-tree heuristic — i.e. the shipped answer is hybrid-per-window,
not structured-default; (b) a large public verifiable-task dataset (xlang-ai/CUA-Gym,
https://github.com/xlang-ai/CUA-Gym) ships an a11y passthrough that is never used — its verification
runs on structured file/app state, not UI trees. Therefore measure a **guest-state probe** (bytes +
determinism of in-guest file/app-state reads) as a separate structured rung for *verification*,
distinct from a11y-for-*acting*.

**Caveat (per the spike's success criteria):** public docs must **not** anchor token/bandwidth claims
to vendor-published numbers without a first-party caveat. The E5 sweep now supplies a first-party
bandwidth anchor: a stable-frame structured **tree-diff is ~1–3% of a screenshot's bytes** (2 043 diff
/ 10 611 full-tree / 76 517 screenshot bytes, gnome-text-editor type interaction;
[REPORT.md](../../spikes/a11y-coverage/REPORT.md)). Structured-vs-screenshot ratios stated elsewhere
should still be marked *(vendor-published, unverified)* unless they cite that measurement.

## Conclusion

The E5 multi-app sweep ([REPORT.md](../../spikes/a11y-coverage/REPORT.md)) makes the picture concrete
and **uneven**: the structured fast path is **real and strong** for Qt widget apps (87% addressable
over AT-SPI) and for Chromium **page content over CDP** (rich roled+boxed+stable-id tree), **weak**
for GTK (fully roled but ~10% addressable; GTK 4 often omits screen bboxes), and **absent** for
terminals (`xterm`: zero nodes) and the browser *shell* over AT-SPI. Canvas/WebGL and game surfaces
remain **unmeasured** (expected near-zero, but not first-party). The honest read is **hybrid
per-window with pixel fallback**, not a clean structured-default — so **D3 stays Provisional** and
spike #2 is *run but not gated*. The reference path (#77/#78/#79/#80) is implemented and tested; the
remaining gates are canvas/game measurement and carrying `value` in the element schema so value-only
edits show up in the diff.
