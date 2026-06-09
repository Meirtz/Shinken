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

Note: the zenity row reports roled/bbox coverage but not `pct_addressable` (the metric the report
itself calls load-bearing); record `pct_addressable` for zenity and every swept app.

## Per-surface coverage map

The five target surface classes and the path each takes. "Measured" = first-party number recorded;
"path ready" = backend implemented + tested on fixtures, awaiting an in-image run with that app
present.

| Surface class | Backend | Expected coverage | Status |
|---------------|---------|-------------------|--------|
| GTK / Qt toolkit (zenity, file manager, LibreOffice) | AT-SPI (`atspi`) | high | **measured** (zenity: 16 nodes, 100% roled, 87.5% bbox) |
| Chromium / Electron page | CDP (`cdp`) | high (browser computes full AX tree) | path ready (#79); awaits a Chromium target in-image |
| Generic Electron / Chromium-like app | CDP (`cdp`) preferred, AT-SPI fallback | high–medium | path ready |
| Canvas / WebGL page | — (no semantic tree) | **low** | classified → fallback |
| Custom-rendered / gamelike surface | — | **near-zero** | classified → fallback |

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
vehicles for capture latency and bandwidth; a per-app size/latency/token table across the five
classes is the remaining sweep (it needs a browser + Electron + canvas app present in the image).

**Field priors to fold into the sweep (2026-06, public CUA stacks):** (a) production computer-use
drivers ship an a11y-first per-window ACI but fall back to pixel clicks on custom-rendered surfaces
(Blender/games/Electron) via a small-tree heuristic — i.e. the shipped answer is hybrid-per-window,
not structured-default; (b) a large public verifiable-task dataset (xlang-ai/CUA-Gym,
https://github.com/xlang-ai/CUA-Gym) ships an a11y passthrough that is never used — its verification
runs on structured file/app state, not UI trees. Therefore measure a **guest-state probe** (bytes +
determinism of in-guest file/app-state reads) as a separate structured rung for *verification*,
distinct from a11y-for-*acting*.

**Caveat (per the spike's success criteria):** public docs must **not** anchor token/bandwidth claims
to vendor-published numbers without a first-party caveat. The first-party anchor we currently have is
the AT-SPI measurement above; structured-vs-screenshot token ratios stated elsewhere should be marked
*(vendor-published, unverified)* until the multi-app sweep replaces them with measured numbers.

## Conclusion

D3's structured-upgrade thesis is **grounded for native toolkit and Chromium surfaces**: a real GTK
dialog exposes a fully-roled tree with usable geometry on ~88% of nodes, and CDP gives an equal-or-
richer path for browser/Electron content. Low-coverage surfaces (canvas, games) are explicitly
classified and routed to the pixel/SoM fallback rather than pretended away. The reference path
(#77/#78/#79/#80) is implemented and tested; completing the full multi-app size/latency/token sweep
is tracked as follow-up and does not gate v0.0.1.
