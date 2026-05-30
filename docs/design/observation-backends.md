# Structured observation backends

Shinken's structured-upgrade thesis ([D3](tech-decisions.md)) says an agent should use a normalized
**element tree** (roles, names, bounding boxes, states, an addressable `ref`) whenever the UI exposes
one, while screenshots remain the universal baseline. That tree can come from several platform
backends; they all reduce to the same ACI [`Element`](../../schema/aci.schema.json) shape (`source`
records which backend produced it, `provenance` carries the backend-native node id so an element can
be re-resolved).

| `source` | Backend | Best for | Status |
|----------|---------|----------|--------|
| `atspi`  | AT-SPI 2 (Linux) | Native GTK/Qt/Electron apps, cross-toolkit default | implemented (SDK) |
| `cdp`    | Chrome DevTools Protocol `Accessibility` + `DOMSnapshot` | Chromium/Chrome/Edge pages, Electron renderers, headless browsers | implemented (SDK) |
| `uia`    | UI Automation (Windows) | Native Windows apps | planned |
| `ax`     | AXAPI (macOS) | Native macOS apps | planned |
| `som`    | Set-of-Marks (vision) | Anything with no usable tree (canvas, games, remote pixels) | planned fallback |

## When to prefer CDP over AT-SPI

For a **Chromium-based browser or an Electron app's web content**, prefer the `cdp` backend:

- **Richer, more reliable tree.** The browser computes the full ARIA/accessibility tree internally;
  CDP's `Accessibility.getFullAXTree` exposes it directly. The same content reaching AT-SPI is often
  thinner, lazily populated (only after a screen reader attaches), or missing entirely for offscreen
  / virtualized DOM.
- **Stable provenance.** CDP nodes carry a `backendDOMNodeId` (and AX node id), which Shinken stores
  as Element `provenance` — a durable handle for re-resolving or scripting a specific DOM node across
  observations. AT-SPI offers no comparable cross-capture id.
- **Bounds without a window manager.** `DOMSnapshot.captureSnapshot` yields layout boxes for every
  node in one call, so elements get a bbox even in `--headless` mode where there is no AT-SPI bus.
- **No a11y bus dependency.** CDP works over the debugger websocket; it does not need
  `at-spi2-core`/D-Bus running in the sandbox.

Prefer **AT-SPI** for everything that is *not* a Chromium surface — native toolkits (GTK, Qt),
the desktop shell, and non-Chromium apps — and as the cross-platform baseline. The two compose: a
session can observe native windows over AT-SPI and an embedded browser over CDP, normalized into one
element list.

CDP nodes marked `ignored` (presentational wrappers) are transparent by default — dropped, with their
children hoisted to the nearest kept ancestor — so the tree matches what assistive tech actually sees.

## Trying it

```bash
# start a Chromium target with a debug port (any Chromium-based browser works)
chromium --headless=new --remote-debugging-port=9222 https://example.com

# capture + normalize its accessibility tree into ACI Elements
python3 scripts/cdp_smoke.py
```

From the SDK, point `observe(structured=True, ...)` at the CDP backend instead of the default AT-SPI:

```python
import shinken
from shinken.cdp import CdpSource

with shinken.connect(url) as env:
    obs = env.observe(structured=True, source=CdpSource(http_url="http://127.0.0.1:9222"))
    ref = obs["elements"][0]["ref"]
    env.act_on(ref, "click")   # element_ref → bbox-centre pixel target (#78)
```

If the backend is unavailable (no browser, wrong port), `observe(structured=True)` returns
`available=False` with an empty element list rather than raising — the screenshot path is unaffected.

See also: [architecture](architecture.md) · [tech decisions / D3](tech-decisions.md) ·
AT-SPI coverage spike (`../../scripts/a11y_coverage.py`).
