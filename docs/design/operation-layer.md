# 12 — The Operation Layer (deep act/observe contract)

> Status: drafted · 2026-06-11 · Owning decisions: **[D13](tech-decisions.md#d13--operation-layer-one-observe-contract-stable-element-identity-and-an-element-verb-family)**
> (operation-layer contract) and **[D14](tech-decisions.md#d14--macos-engine-substrate-screencapturekit--axuielement--cgevent-under-tcc)** (macOS engine
> substrate), extending **D2** (actions) and **D3** (observation).
> Siblings: [ACI spec](aci-spec.md) · [ADRs](tech-decisions.md) · [Architecture](architecture.md) ·
> [Observation backends](observation-backends.md) · [a11y spike report](../engineering/spike-a11y-coverage.md) ·
> wire schema [`../../schema/aci.schema.json`](../../schema/aci.schema.json)
>
> **Reality check:** the §12 table is the per-piece truth and
> [status.md](../engineering/status.md) the authoritative map. The Linux v1 core of this contract
> is **built** (pixel loop; stable-id/diff/settle observe; act-returns-observation; element verbs;
> the G1–G3 desktop verbs), as is the **operation-layer backend contract** (§13/D15 — incl. the
> SDK-local `browser-runtime` CDP backend). The macOS engine is a capture+input slice (local-only,
> D14); the in-guest **Browser Runtime**, the remaining D13 verbs (`set_text_selection`,
> `scroll_element`, per-app observe selector, hint packs), and the Windows engine are
> **designed-only, not built** — all reconciled to the measured evidence from the a11y-coverage
> spike (#2/E5).

The ACI spec defines Shinken's north-star surface; this document specifies the **operation layer**
beneath it — how an agent *actually* perceives and manipulates a real desktop, step by step, with
token cost, identity stability, and timing treated as first-class design problems. The a11y spike
(E5) settled the strategic question: real coverage is **uneven** (strong Qt/CDP, weak GTK, zero
canvas/terminals), so the answer is a **hybrid per-window operation layer** — both perception tiers
delivered together, structured operations where structure exists, pixels always available — not a
structured-default or a pixels-only loop.

---

## 1. Two perception tiers, one observe primitive

**Decision (D13).** Shinken exposes **one observe primitive** that returns both perception tiers in
a single observation:

1. the **screenshot** of the target surface (the universal pixel tier), and
2. the **structured element tree** of the same surface (roles, states, titles/values, stable
   element ids), plus a **focus pointer** — which element currently holds keyboard focus.

The **model picks the tier per step**: read the tree and act by element id when the tree is rich;
read the pixels and act by coordinates when it is not (canvas, games, terminals — the measured
zero-coverage surfaces). Nothing in the protocol forces a mode switch, and no heuristic silently
withholds a tier; the agent sees both and chooses. **Pixels are the universal fallback** — every
surface can always be observed and clicked as pixels, so a missing or lying tree degrades cost, not
correctness.

Rationale: the spike measured that neither tier wins everywhere (Qt 0.87 addressable, canvas 0.00,
and a *change-blind* canvas diff — the tree reported nothing while pixels changed). A primitive
that returns one tier forces the harness to guess; a primitive that returns both lets the model —
the only component that knows what it is trying to do — choose per step. The cost of carrying both
is controlled by the diff layer (§2) and the fidelity knobs (§5).

Sketch (wire detail in [aci-spec §4.1](aci-spec.md)):

```jsonc
// action
{ "verb": "observe",
  "app": { "name": "Text Editor" },        // §5: app/window scoping; omitted = focused app
  "include": ["screenshot", "tree"],        // default: both
  "max_long_edge": 1280, "format": "jpeg", "quality": 80,   // existing fidelity knobs, reused
  "tree_mode": "auto" }                     // auto = diff when a baseline exists (§2)

// observation (abridged)
{ "kind": "view",
  "app":   { "name": "Text Editor", "pid": 4242, "window": { "id": "w:18", "title": "Untitled" } },
  "image": { "format": "jpeg", "w": 1280, "h": 800, "ref": "<bytes>" },
  "tree":  { "mode": "full", "elements": [ /* Element[] */ ], "focus": "e14",
             "serialized": "e1 window \"Untitled\" (active)\n…" },
  "settle": { "quiesced": true, "waited_ms": 140 },          // §3
  "space": { "w": 1280, "h": 800 } }
```

The v0 `screenshot` verb remains (it is the built baseline); `observe` subsumes it and is the
recommended loop primitive once the engine exists.

## 2. Stable element identity + diff observations

This is the heart of the operation layer, and we state it plainly: **the stable-identity/diff layer
is the hardest in-house component in the operation layer, and it is the headline token *and*
correctness optimization.** Everything else in this document is careful plumbing over public
platform APIs; this layer is where Shinken does original engineering work.

**Decision (D13).**

- **Stable ids.** Every interactable element gets an id (`e1`, `e2`, …; the wire `Element.ref`)
  that is **stable across observations within a session**: the same on-screen control keeps the
  same id from one observation to the next, even across tree rebuilds. Ids are assigned in
  discovery order and **never reused** within a session; a control that disappears and returns may
  receive a new id, but an id never silently migrates to a different control.
- **Diff observations.** A re-observation of a surface that has a baseline emits a **diff**, not a
  full tree: changed elements (`~`), added elements (`+`), removed ids (`-`). Removed ids are
  **range-summarized** (`- e21…e34 (14 removed)`) — possible precisely because ids correlate with
  discovery order. The serialized diff lives under a **line budget** (default 200 lines, a tuning
  default, not a measured claim); if the diff would exceed the budget — or the baseline is gone —
  the engine **falls back to the full tree**, because a diff larger than its tree is worse than the
  tree.
- **Stale-id answers carry a re-observe hint.** An action targeting an id that no longer resolves
  fails *typed* — `failure_kind: "stale_element_ref"` with the last-known role/title and an explicit
  hint to re-observe — never a silent click at a stale bounding box. The diff layer makes the fix
  cheap: the re-observation the hint asks for is itself a diff.

**Why it is the hardest component.** No platform hands out durable identity: AT-SPI object paths
churn, AXUIElement references are process-local and invalidate on rebuild, UIA runtime ids are
documented as non-persistent across sessions, and Chromium recycles AX node ids. Stability must
therefore be *manufactured* by a matching layer: anchor each id to the backend-native identity
where one exists (CDP `backendDOMNodeId` is the best of the lot — already carried as Element
`provenance`), and fall back to a structural fingerprint (role + title + ancestry + ordinal) with
conservative matching — when the engine is unsure two nodes are the same control, it must mint a
new id rather than mis-bind an old one, because a wrong stable id produces a confidently wrong
click. This identity problem is also why the diff is the *correctness* optimization, not just the
token one: a diff is only sound if identity is.

**Why it is the headline token optimization.** The spike's first-party anchor: a stable-frame tree
diff is **~1–3% of a screenshot's bytes** (2,043 diff vs 10,611 full-tree vs 76,517 screenshot
bytes on a type interaction). A loop that observes diffs instead of full screenshots — escalating
to pixels only when needed — is where the structured thesis (D3) actually pays.

## 3. Settle-before-observe

**Decision (D13).** The engine **debounces on accessibility change notifications until the UI
quiesces** before capturing an observation. Concretely: the engine subscribes to the platform's
change events (AT-SPI event listeners on Linux; `AXObserver` notifications on macOS; UIA events on
Windows; CDP DOM/AX events in the Browser Runtime) and serves the capture only after no change
notification has arrived for a **quiet window** (default ~150 ms), bounded by a **settle deadline**
(default ~2 s) so a perpetually-animating UI cannot stall the loop. The observation reports what
happened: `settle: { quiesced: bool, waited_ms }`.

Rationale: the single most common agent failure mode after an action is observing a half-painted
UI — a menu mid-animation, a dialog not yet populated — and reasoning over it. Fixed sleeps are the
OSWorld anti-pattern this project exists to replace (the eval layer already bans them in favor of
readiness probes, D7); event-driven quiescence is the same principle applied to perception, and the
same actionability discipline Playwright applies before acting on a locator. A `quiesced: false`
observation is still delivered (deadline hit), honestly flagged.

## 4. Act-returns-observation

**Decision (D13).** Every **mutating** action (click, drag, set value, keys, scroll, …) MAY return
a fresh observation, **opt-in** via an `observe` argument carrying the same parameters as the
observe verb. The runtime executes the action, runs settle-before-observe (§3), and returns the
observation *in the action result* — folding re-perception into the action and saving a round trip.

The **recommended loop is one-observe-per-turn**: each model turn ends with exactly one observation
— either folded into the turn's last mutating action or requested explicitly — never both, and
never zero. Combined with diffs (§2), the steady-state loop becomes: *act → settled diff → act*,
which is the minimum-token loop this layer is designed around.

## 5. Per-app / per-window scoping

**Decision (D13).** Actions and observations target an **app and its key window**, not a screen.

- **App selector:** by `name`, `bundle_id` (macOS), `path`, or `pid`. An **ambiguous name is
  rejected** with a typed error listing the candidates — the engine never guesses which "Notes" you
  meant. `enumerate_apps` / `list_windows` (§6) are the read surface for disambiguation.
- **Key window default:** observation and element resolution default to the target app's key
  window (macOS term; the focused/active toplevel elsewhere). Full-screen capture remains available
  by request (`scope: "screen"`).
- **Fidelity knobs, unified:** the existing capture levers — `scope`
  (`screen | active_window | window:<id>`), `max_long_edge`, `format`/`quality` — apply unchanged
  to `observe`. The app selector composes with them: it retargets *which* app's key window
  `active_window`-style capture means. One knob set, one capture contract, both tiers (the tree is
  scoped to the same window the pixels show).

Rationale: per-window scoping is what made the spike's numbers usable — coverage is a per-window
property (a Qt window at 0.87 next to a terminal at zero), so the tier choice (§1) and the diff
baseline (§2) are per-window state. It also cuts both tiers' cost: a key-window capture is smaller
than a screen, and a key-window tree omits every background app.

## 6. The action surface

**Decision (D13).** The operation layer extends the v0 verb set with an **element verb family**,
mapped onto the existing ACI tagged-union (D2) and advertised via the existing capability handshake
(an old runtime simply does not list them). These are additive over the original 11-verb enum;
the core family has since shipped — the schema's `Verb` enum is now **22** (`drag`,
`mouse_down`/`mouse_up`, `observe`, the element verbs under the shorter wire names
`invoke_action`/`set_value`, the typed in-guest `exec` channel (G1), and the G2+G3 desktop verbs
`clipboard_get`/`clipboard_set`/`launch_app`/`activate_window`; `windows` shipped as the
`list_windows` query) — see [aci-spec §3](aci-spec.md) and
[status.md](../engineering/status.md). The desktop verbs are the
first slice of this section's app/window scoping made real on Linux: `launch_app` puts an app on
the desktop, `activate_window` (EWMH `_NET_ACTIVE_WINDOW`, WM-less raise+focus fallback) picks
*which* window `active_window`-scoped observation means, and `list_windows` disambiguates —
the per-app observe *selector* (`observe {app}`) and the `apps` query remain designed-only.

| Operation-layer verb | Relation to v0 ACI | Payload sketch |
|---|---|---|
| `click` (element-id **or** coordinates) | **existing** `click` — `target` already admits `element_ref \| point_px`; the engine makes `element_ref` real guest-side | `{verb:"click", target:{kind:"element_ref", ref:"e14"}}` |
| `drag` | **new** verb | `{verb:"drag", from:Target, to:Target, modifiers?}` |
| `invoke_element_action` | **new** — invoke a *named secondary action* the element advertises | `{verb:"invoke_element_action", target, action:"showMenu"}` |
| `set_element_value` | **new** — set a value directly through the element interface | `{verb:"set_element_value", target, value:"42"}` |
| `set_text_selection` | **new** — select text, or place the caret before/after a match; `prefix`/`suffix` disambiguate repeated matches | `{verb:"set_text_selection", target, mode:"select"\|"caret_before"\|"caret_after", text:"Q3", prefix?, suffix?}` |
| `scroll_element` | **new** — scroll a *specific element* by **pages** (fractions of its visible extent); the existing pixel-delta `scroll` remains | `{verb:"scroll_element", target, pages:-0.5, axis:"v"}` |
| `send_keys` | **= existing `key`** — keysym chords in xdotool notation (`ctrl+shift+t`), already ours | unchanged |
| `enter_text` | **= existing `type_text`** | unchanged |
| `observe` | **new** — the §1 primitive; subsumes `screenshot` | §1 |
| `enumerate_apps` / `list_windows` | **new read surface** — carried on the existing query channel (`q:"apps"`, `q:"windows"` + app selector), not as mutating verbs | returns `{name, pid, bundle_id?, path?, focused}` / windows with the key window flagged |

**Actuation policy — physical events first, element interfaces as fallback.** For pointer and
keyboard verbs the engine *resolves the element to geometry and synthesizes real OS input* (XTEST
today; CGEvent on macOS; SendInput on Windows) — apps behave most faithfully under the same events
a human produces, and physical events exercise hover/focus/animation paths that programmatic
invocation skips. Accessibility-interface invocation (AT-SPI `do_action`, macOS `AXPress`/value
setting, UIA patterns) is the **fallback** when geometry is unusable (occluded, off-screen,
zero-size) — and the *primary* mechanism only for the verbs that are inherently element-interface
operations (`invoke_element_action`, `set_element_value`, `set_text_selection`). This **amends the
earlier router priority** in [aci-spec §3.2](aci-spec.md), which preferred semantic actuation;
the spike's hybrid verdict plus the fidelity argument flip the default.

**Dedicated-capability preference — prefer `exec` over UI automation for file/system work.** The
typed in-guest `exec` verb (**built**; [aci-spec §3.4](aci-spec.md)) is the operation layer's
dedicated-capability escape hatch: when the step *is* a file/system operation — create or read a
file, install a package, run a verifier or setup script — driving a terminal application through
pointer/keyboard verbs is strictly worse (slower, flakier, and the command is invisible to the
permission audit). Workloads should route such steps through `exec` (capability-gated, argv by
default, the command recorded in the gateway's decision event) and reserve the GUI verbs for what
genuinely exercises the UI. The in-tree integrations implement exactly this preference: swerex,
CUA-Gym, and ProRL-Agent-Server use the in-band `exec` channel whenever the runtime advertises
the verb, keeping `docker exec` only as the pre-exec-runtime fallback.

## 7. Legible serialization grammar

**Decision (D13).** The element tree is delivered to the model in a fixed, line-oriented, legible
grammar (the wire also carries the typed `Element[]`; this is the model-facing rendering). One
element per line:

```
<id> <indent dots> <role> ["title"] [desc="…"] [value="…"] [(<states>)] [actions=[…]]
```

- **`<id>`** — the stable id (§2), what action targets cite.
- **indent** — one `.` per depth level (cheap to tokenize, trivially parseable).
- **role** — the normalized cross-OS role (`button`, `text-field`, `menu-item`, …).
- **title / desc / value** — title quoted; description and value only when present; long values
  elided with `…` under a per-field cap.
- **states** — parenthesized, comma-separated (`enabled`, `focused`, `checked`, `expanded`, …);
  omitted when empty.
- **secondary actions** — the element's advertised named actions beyond the default press
  (`actions=[showMenu, scrollToVisible]`); these are the names `invoke_element_action` accepts.
- **focus trailer** — the serialization ends with one line naming the focused element: `focus: e14`.

Full-tree example:

```
e1 window "Untitled — Text Editor" (active)
e2 . menu-bar
e3 . . menu "File" actions=[showMenu]
e4 . . menu "Edit" actions=[showMenu]
e5 . toolbar
e6 . . button "Bold" (enabled)
e7 . . button "Italic" (enabled)
e8 . text-area value="Dear team, the Q3 report…" (focused, editable, multi-line) actions=[setValue, setSelection]
e9 . status-bar
e10 . . label "Ln 1, Col 12"
focus: e8
```

Diff form (§2) prefixes lines with `~` (changed — the line is re-emitted in full), `+` (added,
indented under its parent id), `-` (removed, range-summarized):

```
~ e8 . text-area value="Dear team, the Q3 report is attached." (focused, editable, multi-line)
~ e10 . . label "Ln 1, Col 38"
+ e11 . . button "Save" (enabled)          parent=e5
- e21…e34 (14 removed)
focus: e8
```

The grammar is versioned with the ACI schema; a serializer change is a contract change, because
trained agents key on this text.

## 8. Per-app hint packs

**Decision (D13).** A session may carry **hint packs**: short, curated, versioned text preambles
keyed by app, injected **once per app per session** into the first observation of that app — e.g.
"this app's drawing surface exposes no elements; use pixel targets there" or a table of the app's
load-bearing keyboard shortcuts. Hints are data, not code: optional, capped in size, recorded in
the observation (`hints: { pack: "writer@1", text: "…" }`) so trajectories stay reproducible. They
encode exactly the per-app unevenness the spike measured, without hard-coding app logic into the
engine.

## 9. Per-OS engine architecture

One operation-layer contract, one engine per OS (the D10 handler-factory, deepened).

### 9.1 macOS engine (D14 — designed)

The macOS engine is specified by **D14** as three public-API pillars under the platform's consent
model:

- **Capture: ScreenCaptureKit.** One-shot key-window/screen screenshots via
  `SCScreenshotManager`/`SCShareableContent` (per-window capture including occluded windows);
  `SCStream` is the later screencast path. ([Apple: ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit))
- **Tree: AXUIElement.** The element tree is read through the `AXUIElement` API on the target
  app. For Chromium-family and Electron apps — which build their renderer accessibility tree only
  on demand — the engine sets the app's accessibility-enable attributes (`AXManualAccessibility`,
  with the older `AXEnhancedUserInterface` as the compatibility path) so page content is exposed
  without a screen reader attached. ([Apple: AXUIElement](https://developer.apple.com/documentation/applicationservices/axuielement_h) ·
  [Electron accessibility](https://www.electronjs.org/docs/latest/tutorial/accessibility))
- **Input: CGEvent synthesis.** Pointer/keyboard verbs synthesize `CGEvent`s (posted per-pid where
  targeting allows), per the physical-events-first policy (§6).
  ([Apple: CGEvent](https://developer.apple.com/documentation/coregraphics/cgevent))
- **TCC posture.** Capture requires the user-granted **Screen Recording** permission; tree reads
  and input synthesis require **Accessibility** — granted by a human (or pre-granted via a managed
  profile on pool images; the full readiness analysis is the
  [macOS readiness spike](../engineering/spike-macos-readiness.md)). While grants are pending,
  **observe returns a typed keep-alive observation** (`status: "pending_permissions"`, listing the
  missing grants) rather than failing — the session stays alive across the human's grant flow, and
  the loop resumes the moment the probe passes.

### 9.2 Linux engine (the built baseline + designed deepening)

Today's built slice: **X11 `GetImage`** capture and **XTEST** input synthesis (with the keysym
chord grammar of `send_keys`/`key` in xdotool notation), proven under live CI. The structured tier
uses the **AT-SPI** tree, normalized to the same `Element` schema — currently implemented as an
SDK-side reference path; moving it into the guest engine with the §2 identity/diff layer is the
designed work. Wayland (portal-based capture/input) remains a separate designed-only item.

### 9.3 Windows engine (designed)

**UIA** for the tree and element interfaces (patterns), `SendInput` for physical events — designed
per D10/Phase 3, sequenced after macOS. ([Microsoft: UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32))

## 10. Browser Runtime (designed, phase-next)

For web tasks Shinken **prefers a browser-specialized runtime** over driving the browser as a
generic desktop app: the browser exposes a richer, cheaper automation surface than its window does.
The Browser Runtime presents **three tab surfaces**, mirroring the desktop tier structure:

1. **Locator scripts** — a constrained **Playwright-locator subset**: the model writes small
   scripts against locators (`getByRole`, `getByText`, …) executed in the runtime.
   **Code-over-tools rationale:** a find→assert→act sequence is one script, not three round trips;
   composition lives in code the runtime can bound and record, instead of a wider tool surface.
2. **Semantic node-ids over the DOM** — the same stable-id + diff discipline as §2, anchored on
   CDP node identity over the DOM/AX tree; act by node id.
3. **Pixels** — screenshot + input dispatch via **CDP** (`Page.captureScreenshot`,
   `Input.dispatch*`), the universal fallback, inside the same tab.

Status honesty: the Browser Runtime is **designed/phase-next** — except that the **CDP coverage
basis is already first-party-measured** by the spike (every labeled control on Chromium page
content resolved over CDP, on both a browser and a real Electron app; canvas measured at zero).
The surface design builds on that measurement; the runtime itself is not built. **Do not confuse
it with the built `browser-runtime` *backend*** (§13/D15): that is an SDK-local adapter driving
an *external* CDP browser through the same three-surface shape — this section's in-guest,
Shinken-managed runtime remains designed-only.

## 11. Operating consent and app allow-lists

A short, neutral note (this is a runtime entitlement, D6 — not a headline subsystem): a session may
carry a **per-app allow-list**; operating an app outside it requires a one-time consent grant,
recorded as a capability event. **Unattended sessions default to deny** for non-allow-listed apps
(there is no one to ask). On managed pool images the allow-list is provisioned with the envelope,
so the hot loop never prompts.

## 12. Built vs designed (reconciliation)

| Piece | Status |
|---|---|
| Pixel tier: screenshot/screencast, X11 capture + XTEST input, `key`/`type_text`, fidelity knobs (`scope`/`max_long_edge`/`format`/`quality`) | **built** (Linux/X11, live CI) |
| AT-SPI/CDP → normalized `Element` reference paths; Linux guest stable refs + guest-side `element_ref` resolution; SDK-local AT-SPI/CDP fallback | **built** (Linux guest + SDK fallback) |
| Coverage evidence (Qt/GTK/CDP/Electron/terminal/canvas; tree-diff ~1–3% of screenshot bytes) | **measured** (spike #2/E5) |
| Typed in-guest `exec` channel (argv default + shell opt-in; buffered + streamed; group-kill timeouts; gateway-audited; PTY reserved) | **built** (G1; [aci-spec §3.4](aci-spec.md)) |
| Stable-id + diff engine; settle-before-observe; act-returns-observation; `list_windows`; core element verbs | **built** (Linux/AT-SPI v1); combined dual-tier `observe`, app selector/`apps`, `set_text_selection`/`scroll_element`, and hint packs remain designed (D13) |
| macOS engine | **capture + input v1 built** (CoreGraphics + CGEvent, TCC-honest, local-only proof); ScreenCaptureKit, AXUIElement observation, and co-use tier designed (D14) |
| Windows engine (UIA + SendInput) | **designed-only** (D10) |
| Browser Runtime (three tab surfaces, in-guest) | **designed-only**; CDP coverage basis measured (the built `browser-runtime` *backend* in the next row is the SDK-local adapter, not this runtime) |
| Operation-layer backend contract (third-party drivers under the ACI; honest capability negotiation; `RoutedSession` CU↔BU) | **built** (D15; `shinken.backends` — `cua`/`mcp-computer`/`browser-runtime`/`e2b`/`routed`) |

The authoritative built-vs-designed map remains [status.md](../engineering/status.md); wire shapes
for the new verbs and diff observations are illustrated in [aci-spec §3.3/§4.1](aci-spec.md).

## 13. Operation-layer backends — the narrow waist (D15)

The same verb surface this document specifies is the seam at which **third-party computer-control
systems plug in *underneath* the ACI**. Shinken ships its own backend (`shinkend`), but the
operation layer is a narrow waist: anything presenting the `Sandbox` verb surface can sit beneath
it. The contract is fixed by
**[D15](tech-decisions.md#d15--operation-layer-backends-a-pluggable-execution-substrate-under-the-typed-aci)**:

- A backend is a `SandboxProvider` subclass whose `connect()` returns a **duck-typed Sandbox**
  translating the verbs of §6 onto the third party's API; the inherited `provider.session()`
  lifecycle and every consumer (the Operator loop, eval, the gym) work unchanged.
- **Honest capability negotiation** is the contract: `capabilities.verbs`/`targets`/
  `structured_observation` advertise only what the backend really serves, and missing capabilities
  degrade *loudly* (`UnsupportedProviderOperation`) — never a silent no-op or a fabricated tree. A
  pixels-only backend advertises `point_px` and `structured_observation=False`; one with an a11y
  tree (or a CDP/AX bridge) serves the §2 `element_ref` family. No fork tier ⇒
  `supports_fork=False`.
- **CU↔BU composition** (the host-side split a tool like Codex.app runs — desktop CU beside browser
  BU) is `RoutedSession`: named surfaces behind one Sandbox-shaped object the Operator loop drives
  unchanged, routing per action with `source` provenance on every action and observation.

Built backends: `cua` (trycua), `mcp-computer` (codex-style AX MCP — serves the structured
`element_ref` family, filling the macOS-AX gap until D14 is built), `browser-runtime` (the §10
Browser Runtime as a CDP backend), and `e2b` (E2B cloud desktop, pixels + shell). OSWorld is a
one-way *coarsening* of this contract (pixel/`pyautogui`/full-screenshot-poll), not a peer
backend — compatible (M5 gate, score 1.0) but fork/structured-observe/capability-negotiation do
not round-trip.
