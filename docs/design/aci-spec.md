# 11 — ACI Specification (the north-star interface)

> Status: drafted · 2026-05-30 · Siblings: [00 Vision](vision.md) · [02 Architecture](architecture.md) · [05 ADRs](tech-decisions.md) · [10 Phase-0 plan](../engineering/v0.0.1-plan.md) · wire schema [`../../schema/aci.schema.json`](../../schema/aci.schema.json)

The **Agent-Computer Interface (ACI)** is Shinken's product. This document is the north-star it is
built toward — the elegant surface, the typed action/observation model, the action-execution and
capture strategies, and the small async core contract that lets *any* harness drive Shinken. It
reconciles to decisions **D2** (actions), **D3** (observation), **D5** (replay), **D8** (interfaces),
and — for the deep per-step act/observe contract — **D13/D14** (the operation layer; full design in
[operation-layer.md](operation-layer.md)).

> **Lineage.** Shinken is the elegant Python-exec sandbox idea grown up: the prior `shinken` gave
> you `init() / run(code) / save() / restore() / cleanup()` with a rich `result.output`. Shinken
> keeps that exact ergonomics and generalizes it — `run(code)` gains a sibling GUI surface
> (`observe`/`act`), and `save`/`restore` are promoted from a Python interpreter's namespace to an
> entire **forkable desktop**. The checkpoint idea *was* the right one; it is now the killer
> primitive (D1/D5).

---

## 1. Design principles (what "elegant" means here)

1. **One line to start.** `shinken.connect()` (or `.init()`) returns a ready session.
2. **Few, high-affordance verbs.** A small typed action set; sugar methods for the common ones.
3. **Screenshot baseline, structured upgrade.** The first loop works with screenshots everywhere; structured observations add stable refs when available.
4. **Checkpoints are first-class and trivial.** `save()/restore()/fork()` — the same primitive as instant reset and replay-branching.
5. **Progressive disclosure.** Simple by default (`screenshot()` + typed actions); structure and power on demand (`observe(structured=True)`, `fork`, `unlock`).
6. **Same object, local or remote, any OS.** `connect()` local, `connect(id)` cloud — identical surface.
7. **The agent loop is one call** (`drive`), but `observe`/`act` are always exposed for control.
8. **Powerful in the Sandbox, explicit at the boundary.** The guest is allowed to do real work; boundary capabilities are provisioned, scoped, and replayed (`unlock`).

---

## 2. The north-star API

```python
import shinken

# 1) one-line setup — your init(), now a whole desktop (local or cloud; same object)
env = shinken.connect()                       # or shinken.connect("sbx_id") / shinken.init(os="linux")

# 2) observe — Phase 0 baseline is screenshots; structure is the upgrade path
shot = env.screenshot()                       # -> {'png': bytes, 'w': int, 'h': int}
view = env.observe(structured=True)           # -> a11y tree when available (later)

# 3) act — a tiny verb set, on element refs (preferred) or coordinates
env.click(x=640, y=400); env.type("Q3 report"); env.key("ctrl+s")
env.scroll(x=900, y=400, dy=-300)
env.click(x=640, y=400)                       # raw pixels still available

# 4) run() — code-as-action, preserved verbatim (a gated capability)
r = env.run("a = 10\nb = 20\nprint(a + b)");  print(r.output)   # "30"

# 5) save / restore / fork — checkpoints over the WHOLE desktop
cp = env.save()                               # snapshot the Sandbox
env.click(view["Delete all"]); env.restore(cp)# time-travel back
kids = env.fork(cp, n=8)                       # branch into 8 parallel Sandboxes (Best-of-N)

# 6) drive — one call, provider-agnostic, fully recorded
out = env.drive(shinken.agent("claude-opus-4-8"), task="Make every .txt in ~/docs a .md")

# 7) replay is free — the live event stream IS the recording
env.replay.save("run.skn")

# 8) capability provisioning — boundary powers are explicit and replayed
with env.unlock("net.egress", scope="pypi.org", reason="install pandas"):
    env.run("pip install pandas")

env.close()
```

Streaming is the same object: `for ev in env.events(): ...` yields the typed event stream (the
replay log) live; `env.video()` attaches the on-demand pixel/video channel.

File transfer is also first-class, but it is **not** the same hot RPC channel:

```python
env.put_file("local/input.csv", "/workspace/input.csv")
env.put_dir("fixtures/task_001", "/workspace/task")
env.get_file("/workspace/result.json", "artifacts/result.json")
artifact = env.artifacts.upload("trace.log")     # content-addressed, checksummed
env.replay.attach(artifact)                      # referenced from .skn, not copied through JSON
```

Small transfers should feel synchronous; large transfers are binary, chunked, resumable, and
backpressured. The ACI event stream records transfer metadata and content hashes, not megabytes of
base64.

---

## 3. Action model (D2)

**One canonical typed action**, a tagged union discriminated by `verb` (wire form in
[`../../schema/aci.schema.json`](../../schema/aci.schema.json)). The spatial `target` is a discriminated
union so the *same* verb serves pixel models, normalized models, and element-ref models:

```
target = oneof{ point_px{x,y} | point_norm{x,y∈0..1} | element_ref{ref, source} }
```

Every observation carries a `CoordinateSpace {origin, w, h, dpr}`; coordinate normalization lives in
the protocol, once. v0 verbs (**17**, matching the `Verb` enum in
[`../../schema/aci.schema.json`](../../schema/aci.schema.json) and the runtime's advertised
capabilities):

| Family | Verbs |
|---|---|
| Pointer | `click`, `double_click`, `right_click`, `move`, `scroll` |
| Gesture | `drag`, `mouse_down`, `mouse_up` |
| Keyboard | `type_text`, `key` |
| Pixel observation | `screenshot`, `start_screencast`, `stop_screencast` |
| Structured observation (D13/M1b) | `observe` |
| Element (AX path, D13/M1b) | `invoke_action`, `set_value` |
| Timing | `wait` |

`drag` is the composed
gesture — pointer down at `target`, interpolated moves, up at `to`, with optional `duration_ms`
(clamped guest-side) and `button` (`left`/`middle`/`right`); `mouse_down`/`mouse_up` are its
decomposed halves for free-form gestures (down → moves → up), `target` optional (absent = act at
the current pointer position). `observe` captures the structured (a11y) tree with stable element
ids, diff rendering, and settle (§4.1); `invoke_action`/`set_value` drive an element's
accessibility interface directly (the AX-path fallback to physical events — §3.2 router).
**Code-as-action** (`run`/bash/edit) is a separate,
**off-by-default** capability class behind the policy boundary (D6) — expressive, but gated and
auditable.

**Act-returns-observation (`observe` argument).** Every *mutating* verb (the schema's
`MutatingVerb` set — pointer/keyboard/gesture plus the element verbs, not
`screenshot`/screencast/`observe`/`wait`) accepts an optional `observe` object carrying
the screenshot-shaped capture levers (`scope`, `format`, `quality`, `max_long_edge`). The runtime
then follows the action's `ack` with a fresh `observation` whose `cause` is the SAME `call_id` —
one round trip instead of act + screenshot, the same correlation rule as the one-shot screenshot
reply (and a binary frame on a binary-negotiated session). Capability-gated: a runtime advertises
`capabilities.observe_after_act` in the welcome; clients must not send `observe` otherwise (old
runtimes are unaffected — they never see the field). If the action succeeds but the capture fails,
the ack stays `ok:true` and the follow-up is a typed error `result` on the same `call_id`.

**Queries** (`Query.q`, a closed enum): `platform`, `screen_size`, `ready` (S9 boot readiness),
and `list_windows` — EWMH window enumeration (`_NET_CLIENT_LIST`, `_NET_WM_NAME`, `_NET_WM_PID`,
`_NET_ACTIVE_WINDOW`; a WM-less display falls back to the mapped root children), answering
`[{id, title, pid, x, y, w, h, focused}]` with `id` usable as the `window:<id>` capture scope —
the Linux "enumerate apps" read primitive ahead of the structured/a11y engine (D3).

### 3.1 Action execution taxonomy

The ACI action object is the **model-facing contract**, not the execution strategy. Shinken keeps the
surface small and typed, then routes each accepted action through an explicit capability boundary and
a backend router:

```text
agent/model grammar -> adapter -> typed ACI action -> capability boundary -> executor router -> backend
```

**String-form XML tool calls are a first-class model grammar.** Many CU models emit their tool
calls as *text*, not structured `tool_use` JSON. The SDK's dialect layer
(`shinken.dialect.parse_actions(text, format="auto"|"xml"|"dialect")` /
`parse_xml_actions(text)`) parses the wild-type grammars into the same canonical typed actions:
(a) JSON-in-XML `<tool_call>{"name": "computer_use", "arguments": {…}}</tool_call>`
(Qwen/Hermes), (b) `<invoke name="…"><parameter name="…">…</parameter></invoke>` blocks,
(c) `<function=…>` / `<function name="…">` parameter-element calls (Seed/UI-TARS-2,
qwen3.5-4b), and (d) attribute/element XML (`<click x="100" y="200"/>`,
`<action name="click"><param name="x">100</param></action>`). Parsing is tolerant — markdown
fences, namespace prefixes, unclosed tags, unquoted attributes, trailing-comma/truncated JSON —
but **never silently drops**: an unknown verb or an action-shaped tag the parser cannot map
raises a typed `DialectError` carrying the offending snippet, so the Operator loop returns a
teaching error instead of executing a partial plan; multiple calls in one message yield an
ordered action list. The vendor adapters expose this as `.from_text(text)`; plain-text DSLs
keep their existing parsers (Kimi-VL/Aguvis pyautogui in `shinken.adapters.kimi`, OSWorld
pixel-pyautogui code blocks in `shinken.osworld.parse_model_actions`).

This gives Shinken four distinct action classes:

- **GUI actions** — pointer, keyboard, scroll, screenshot, and later element-ref actions. These are
  typed ACI verbs executed by a GUI backend. A backend may use XTEST, PyAutoGUI, CDP, AT-SPI, UIA,
  AX, SendInput, or CGEvent internally, but it does **not** accept arbitrary model-supplied code.
- **Browser-semantic actions** — a specialization of GUI actions where the router can use CDP or a
  DOM/accessibility handle before falling back to synthetic input. They still enter as typed ACI
  verbs and produce replayable action/observation events.
- **CLI / code actions** — shell, Python, editor, install, and other command execution. These are not
  GUI backends. They are powerful side-effecting capabilities behind the D6 `tool_runner` policy
  boundary, with scoped filesystem, egress, credential, timeout, and replay-redaction rules. The
  Phase-0 boundary, request/result shape, and `.skn` event mapping are specified in
  [`code-execution.md`](code-execution.md) (#60).
- **Artifact and file operations** — fixture upload, result download, directory sync, and replay
  resource attachment. These use the artifact/file-transfer channel, not the low-latency GUI action
  path.

**v0.0.1 scope (settled 2026-06, D2):** CLI/code actions and file transfer are **not** ACI wire verbs
in v0.0.1 — they ride the substrate's own channel (the OSWorld controller's `/execute`, `docker
cp`/`exec`, the inject transport; the SDK's `put_file`/`get_file` are substrate-side, not wire
messages). Typed `exec`/`put_file`/`get_file`/`launch` wire verbs are deferred post-v0.0.1, added
behind the code-as-action capability class when a Workload must do setup/scoring purely through the
ACI. `element_ref` resolution is **guest-side on Linux** as of the M1b observation engine: the
runtime resolves a live ref from the session's last structured observation to its bbox centre and
fires a physical XTEST event there (the SDK-side `point_px` resolution remains the fallback for
pre-engine runtimes); `invoke_action` is the AX-path alternative for geometry-less elements. A
stale/unknown ref answers a machine-readable ack error starting `stale_element_ref:` — the contract
is *re-observe, then retry with a fresh ref*. The rest of the #96 backend ladder (CDP, UIA/AX) stays
designed-only (D3).

The Action Gateway (or the Phase-0 gateway shim) owns capability decisions before an action reaches
`shinkend`. `shinkend` executes only validated actions inside the Sandbox and reports typed
acknowledgements/observations back into the event stream. This preserves a simple invariant:
**ordinary GUI backends implement typed verbs; boundary-crossing power is requested, authorized, and
recorded separately.**

### 3.2 Backend-pluggable executor

`shinkend` exposes one typed action surface over a **per-OS, backend-pluggable executor** with a
router that prefers semantic actuation, falling back to synthetic input. (Validated by the P0
deep-dive; see [notes/p0-deepdive.md](../../notes/p0-deepdive.md).)

| OS | Default (semantic) | Fallback (synthetic) | Focused/background |
|----|--------------------|----------------------|--------------------|
| **Linux** (P0, X11) | browser → **CDP**; `element_ref` → **AT-SPI2 `do_action`** | **XTEST** via libXtst/libxdo (not shelling `xdotool` per call) | XTEST on a controlled Xvfb/Xorg-dummy session (we own the display) |
| Linux (later) | AT-SPI2 | **libei/libeis** via the RemoteDesktop portal; `uinput` | portal session |
| **Windows** | **UIAutomation `Invoke`/`Value`** | **SendInput** | UIA + background dispatch (later) |
| **macOS** | **AXUIElement `AXPress`/`AXSetValue`** | **CGEventPostToPid** (avoid `CGWarpMouseCursorPosition`) | per-pid post; SkyLight (later) |

**Router priority at actuation (amended 2026-06-11 by D13 — physical events first):** browser
surfaces route to the Browser Runtime (CDP); for everything else, an `element_ref` is **resolved to
geometry and actuated with synthetic OS input** (`XTEST`/`SendInput`/`CGEvent`) at the element —
apps behave most faithfully under the same events a human produces — and accessibility-interface
invocation (AT-SPI `do_action` / UIA patterns / AX `AXPress`) is the **fallback** when geometry is
unusable (occluded, off-screen, zero-size), plus the primary mechanism for the inherently
element-interface verbs (`invoke_element_action`, `set_element_value`, `set_text_selection`, §3.3).
Pixel-coordinate targets go straight to synthetic input. We do **not** make raw XTEST/pyautogui the
sole strategy (coordinate-only, focus-stealing, X11-only). A `pyautogui`-compatible shim is kept so
OSWorld's pyautogui action space runs unchanged.

### 3.3 Operation-layer verb family (D13 — built core + designed remainder)

The operation layer ([operation-layer.md](operation-layer.md), D13) extends the original v0
11-verb enum with an **element verb family**. The core of it is now **built and in the 17-verb
enum** (§3): `drag`, the `observe` wire verb, and the element verbs — shipped under the shorter
wire names `invoke_action`/`set_value` (the canon's `invoke_element_action`/`set_element_value`),
with `windows` shipped as the `list_windows` query. The **remainder is designed-only** — additive,
capability-negotiated (`welcome.capabilities.verbs`), absent from `schema/aci.schema.json` until
built: `set_text_selection`, `scroll_element`, the `apps` query, and per-app/key-window observe
scoping. Illustrative, schema-ready shapes (designed forms; the built verbs use the §3/§4.1 wire
shapes):

```jsonc
// click by element id — the EXISTING click verb; target already admits element_ref (the engine
// resolves it guest-side to geometry, then synthesizes real input — §3.2 router, D13)
{ "verb": "click", "target": { "kind": "element_ref", "ref": "e14" } }

// drag — new verb; two targets, same Target union
{ "verb": "drag", "from": { "kind": "element_ref", "ref": "e14" },
  "to": { "kind": "point_px", "x": 640, "y": 410 }, "modifiers": ["shift"] }

// invoke a NAMED secondary action the element advertises (actions=[…] in the serialization)
{ "verb": "invoke_element_action", "target": { "kind": "element_ref", "ref": "e3" },
  "action": "showMenu" }

// set a value directly through the element interface
{ "verb": "set_element_value", "target": { "kind": "element_ref", "ref": "e8" }, "value": "42" }

// text selection / caret placement; prefix/suffix disambiguate repeated matches of `text`
{ "verb": "set_text_selection", "target": { "kind": "element_ref", "ref": "e8" },
  "mode": "caret_after",            // "select" | "caret_before" | "caret_after"
  "text": "Q3", "prefix": "the ", "suffix": " report" }

// scroll a specific element by PAGES (fractions of its visible extent); pixel-delta scroll remains
{ "verb": "scroll_element", "target": { "kind": "element_ref", "ref": "e20" },
  "pages": -0.5, "axis": "v" }

// the unified observe primitive (§4.1) — subsumes screenshot
{ "verb": "observe", "app": { "name": "Text Editor" }, "include": ["screenshot", "tree"],
  "max_long_edge": 1280, "format": "jpeg", "quality": 80, "tree_mode": "auto" }
```

Unchanged aliases: `send_keys` **is** the existing `key` (xdotool keysym chords); `enter_text`
**is** the existing `type_text`. The read surface rides the existing query channel, not a verb:

```jsonc
{ "type": "query", "q": "apps" }
// → { "apps": [ { "name": "Text Editor", "pid": 4242, "path": "/usr/bin/gnome-text-editor",
//                 "focused": true } ] }
{ "type": "query", "q": "windows", "app": { "name": "Text Editor" } }
// → { "windows": [ { "id": "w:18", "title": "Untitled", "key": true, "bbox": [0,0,1280,800] } ] }
```

**`element_ref` semantics under D13:** refs are **stable across observations within a session**
(never reused, never migrated to a different control); an action citing a ref the engine can no
longer resolve fails typed, with a re-observe hint:

```jsonc
{ "type": "result", "id": "a-19", "status": "failed",
  "failure_kind": "stale_element_ref",
  "detail": { "ref": "e14", "last_seen": { "role": "button", "title": "Save" },
              "hint": "re-observe; the element tree has changed since seq 312" } }
```

An **ambiguous app selector** is likewise a typed rejection (never a guess):

```jsonc
{ "type": "result", "id": "a-20", "status": "failed", "failure_kind": "ambiguous_app",
  "detail": { "name": "Notes", "candidates": [ { "pid": 511, "path": "/usr/bin/gnome-notes" },
                                               { "pid": 988, "path": "/opt/notes-electron/notes" } ] } }
```

**Observe-after-act (D13):** every mutating verb accepts an optional `observe` argument (same
parameters as the `observe` verb). The runtime actuates, waits for UI quiescence
(settle-before-observe), and folds the fresh observation into the action result — the recommended
loop is one observation per turn:

```jsonc
{ "verb": "click", "target": { "kind": "element_ref", "ref": "e14" },
  "observe": { "include": ["tree"], "tree_mode": "auto" } }
// → result { "status": "ok", "observation": { …ViewObservation (§4.1), tree.mode = "diff"… } }
```

---

## 4. Observation model (D3) — screenshot baseline, structure upgrade

Phase 0 is **screenshot-first** because that is the universal GUI-agent baseline. The first usable
loop is `screenshot -> model/adapter -> typed action -> screenshot`, with every step recorded to
`.skn`. The accessibility tree is a **parallel structured track** (our bandwidth/robustness
differentiator), not a prerequisite for the first GUI agent.

**One capture contract, three operations, one capture source per OS:**

```
screenshot(scope=screen|active_window|window:<id>, max_long_edge?) -> image
start_screencast(scope=screen|active_window|window:<id>, fps=0.1..30, max_long_edge?, resume_stream?) -> stream
stop_screencast(stream)
```

`resume_stream` is the reconnect contract (#56): if the runtime still holds that logical stream's
state, pushed frames keep the SAME `stream` id and `seq` continues where it left off — the seq gap
counts frames the runtime emitted but the client never received; capture pauses while no connection
holds the stream, so use the `ConnectionError` window for temporal accounting. Otherwise a fresh
stream starts (new id, seq 0), so a consumer always learns whether continuity was lost.

- **macOS:** ScreenCaptureKit (`SCScreenshotManager` for stills incl. occluded per-window; `SCStream` for video).
- **Windows:** Windows.Graphics.Capture (WGC) — the only API doing per-window **and** occluded/background, for both stills and video.
- **Linux:** Wayland → `xdg-desktop-portal` ScreenCast + PipeWire (persist `restore_token`); X11 (our P0 fork tier) → XComposite per-window + XShm.
- **Encoder hand-off:** keep frames GPU-resident → GStreamer `nvcodec`/NVENC (neko-style, realtime-tuned). NVENC streaming runs on Ada L4/L40S, never A100/H100 (D11).

**Two paths off the same source:** (1) **screenshot-per-step**, downscaled to the model's true vision
resolution, for the agent loop; (2) **continuous video** for the human Control Panel. A structured
**observation event** (`a11y` full→diff with stable element refs) is recorded alongside screenshots
when available and can become the low-bandwidth default for tree-rich apps.

### 4.1 Structured observation contract (M1b, built for Linux/AT-SPI)

The guest engine is wired as one verb plus an observation shape (schema:
[`schema/aci.schema.json`](../../schema/aci.schema.json), advertised as
`capabilities.structured_observation`):

```
observe(structured=true, diff?, settle_ms?) ->
  observation{ tree: full|diff, tree_text, elements[], revision, diff_of?,
               focus?, node_count, capture_ms }
```

- **Settle then walk.** `settle_ms` debounces a11y change notifications (focus/window/text/
  children-changed) for a bounded quiesce window (clamped; total wait hard-capped) before the
  AT-SPI walk of the **focused app** (all apps when none is active). The walk is capped in
  nodes/depth/time and runs on a dedicated worker thread with per-call deadlines — a hung AT-SPI
  peer yields a typed error, never a wedged runtime; an over-cap tree returns partial,
  labeled `truncated`.
- **Stable ids.** Elements are `e<N>`: minted monotonically per session, keyed to a composite
  identity heuristic (AT-SPI bus name + object path, plus role and parent path), kept while the
  element lives, **evicted and never reused** when it disappears. Honest limit: a toolkit that
  recycles an object path for an identical-role node under the same parent is indistinguishable
  from the original. Content changes (name/value/states/bbox) do not change identity — they render
  as `~` lines.
- **Two renderings, one message.** `tree_text` is the model-facing serialization — a header
  (`app:`/`window:`/revision), one indented line per element
  (`e<id> <role> [(states)] ["title"] [Value:…] [Actions:…]`), and a `focus:` trailer; `elements`
  is the raw structured array (ACI `Element`s) for tooling, always the FULL live list. With
  `diff`, `tree_text` becomes `~` changed / `+` added lines plus a summarized `- removed:`
  id-range line against revision `diff_of` (an explicit *no change* notice when nothing moved; the
  full tree when the diff exceeds the line budget, `SHINKEND_DIFF_BUDGET`).
- **Element actions.** Pointer verbs accept `target.kind=element_ref`: the runtime resolves the
  live ref to its bbox centre and fires a **physical** XTEST event (physical-event preference).
  `invoke_action` (AT-SPI Action by name, `text` = action name, default = first action) and
  `set_value` (`text` = new value; numeric Value or EditableText) are the AX-path verbs. A
  stale/unknown ref nacks with an error starting **`stale_element_ref:`** → re-observe and retry.

Layered observation in Phase 0: rung 0 screenshot baseline → rung 1 a11y/DOM structure when available
→ rung 2 Set-of-Marks / OmniParser for screenshot-only UIs → rung 3 focused region/zoom/video.

Phase-0 screencast resource policy is deliberately conservative:

- Runtime outbound queue: bounded to 32 frames/replies per connection.
- Live-frame policy: drop newest screencast frame when the client is behind; RPC replies are awaited.
- Runtime connection caps: 64 concurrent connections and 8 concurrent screencasts by default.
- Runtime timeouts: 10s unauthenticated handshake timeout, 15m authenticated idle timeout, and 4h
  max connection lifetime.
- Python SDK frame queue: bounded to 32 frames with drop-oldest semantics; starting a new stream
  clears stale frames, and stopping a stream pushes an explicit end sentinel.

### 4.2 Unified observe + diff observations (D13 — the designed superset)

The operation layer binds both perception tiers into **one observe primitive** ([operation-layer.md](operation-layer.md)
§1–§3): the observation carries the screenshot, the structured element tree, and a **focus
pointer**; the model picks the tier per step; pixels are the universal fallback. Illustrative,
schema-ready shape (`ViewObservation`):

```jsonc
{ "type": "observation", "kind": "view", "seq": 312,
  "app": { "name": "Text Editor", "pid": 4242,
           "window": { "id": "w:18", "title": "Untitled", "key": true } },
  "space": { "w": 1280, "h": 800 },                       // CoordinateSpace, as today
  "image": { "format": "jpeg", "quality": 80, "w": 1280, "h": 800, "ref": "<bytes>" },
  "tree": { "mode": "full",
            "elements": [ /* Element[] — existing $defs.Element, refs stable in-session */ ],
            "focus": "e8",
            "serialized": "e1 window \"Untitled — Text Editor\" (active)\n…\nfocus: e8" },
  "settle": { "quiesced": true, "waited_ms": 140 },        // settle-before-observe report
  "hints": { "pack": "writer@1", "text": "…" }             // optional, once per app per session
}
```

A re-observation against an in-session baseline returns a **diff** instead of a full tree (the
existing `$defs` `delta` sketch, deepened — removed refs may be range-summarized because ids are
assigned in discovery order; if the serialized diff exceeds the line budget, the engine sends the
full tree instead):

```jsonc
"tree": { "mode": "diff", "base_seq": 312,
  "changed": [ { "ref": "e8",  "role": "text-area", "value": "Dear team, …", "states": ["focused","editable"] } ],
  "added":   [ { "ref": "e11", "role": "button", "name": "Save", "parent": "e5" } ],
  "removed": [ "e21…e34" ],
  "focus": "e8",
  "serialized": "~ e8 . text-area value=\"Dear team, …\" (focused, editable)\n+ e11 . . button \"Save\" (enabled)\n- e21…e34 (14 removed)\nfocus: e8",
  "line_budget": 200, "truncated": false }
```

On a permissions-gated engine (macOS TCC, D14), observe while grants are pending returns a typed
**keep-alive**, not a failure:

```jsonc
{ "type": "observation", "kind": "view", "status": "pending_permissions",
  "missing": ["screen_recording"], "retry_after_ms": 1000 }
```

All of §4.1 is **designed-only** (the guest observation engine is unbuilt — see
[status.md](../engineering/status.md)); the built v0 surface is the screenshot/screencast contract
above plus SDK-side AT-SPI/CDP reference paths.

---

## 5. Runtime state + replay model (D5) — two artifacts, two contracts

The P0 deep-dive splits evidence from runnable state:

- **(A) `.skn` layered bundle** (cross-OS, the debug/audit/train artifact): append-only event log
  (the source of truth, = the live stream) + checkpoint/snapshot references + an **on-demand VIDEO
  sidecar**. Storage levers from day one: content-addressed `resources/<sha256>` dedup, full-snapshot
  + typed-delta observations (`a11y_delta`/`png_diff`). The `.skn` recording surface was removed and
  deferred (#216/#217); the `.skn` wire form will be defined when that surface returns (see D5).
- **(B) Runtime state**: provider snapshots, Shinken checkpoints, restore/resume operations, and
  fork-from-checkpoint. This is what makes a desktop live again. `.skn` points at it; `.skn` does not
  replace it.
- **(C) qcow2 deterministic-eval VM** (the OSWorld-style eval path): a read-only golden qcow2 base
  per task-suite + per-task **backing-file overlays** (redirect-on-write) + `savevm/loadvm`. This
  needs **only snapshot-revert** — agent-trajectory replay is **deferred** here (P0).

**Later:** video sidecar as IDR-aligned fragmented-MP4 with a keyframe→seq→byte-offset index (scrub
snaps to event seq); true mid-execution **branching** on the Linux fork tier (Firecracker MAP_PRIVATE
CoW memory-fork + a versioned, non-pickle agent-half checkpoint, immutable checkpoint DAG). The
"scientific" determinism layer is reserved for the **agent core only** (recorded LLM/tool inputs
replayed via stubs; CRIU for the process).

Terminology:

- **Snapshot**: substrate-captured state.
- **Checkpoint**: Shinken restore point linking snapshot refs, event offset, and optional agent state.
- **Fork**: create a new live branch from a checkpoint.
- **Resume/restore**: continue a paused/snapshotted Sandbox.
- **Replay**: inspect or export the event ledger.

---

## 6. The harness-compat core (D8) — small async core + thin adapters

Plain Gym (sync `reset`/`step`) is a necessary *veneer* but insufficient as the *core* — it can't
express streaming observations, async/long-horizon runs, or permission interrupts. So the core is a
**small async contract**, and every harness is a **thin adapter** over it.

**Env contract (environment side, async):** `create(spec)` / `connect(id)` / `ephemeral(spec)`
(cua's three lifecycle modes); `reset(task_config?) -> Observation`; `step(action) -> (obs, reward?,
done?, info)`; `observe()`; `save()/restore()/fork()`; `events()` stream; `close()`.

**Operator contract (actuation side, async, UI-TARS-shaped):** `screenshot()/observe() ->
Observation`; `execute(actions: Action[]) -> ExecuteResult{status, executed_target_logical_px,
error?}`; `supported() -> {verbs, targets, observation_types}`.

**Control + capability channel:** a `capability_required{capability, scope, token}` event pauses the
run only when it needs a new boundary power (external egress, credential broker, host mount, GPU,
persistence, OS entitlement). The controller or policy resolves it, and the decision is recorded as
a capability/permission event in `.skn`. Ordinary in-sandbox actions do not prompt.

**Artifact / file-transfer channel:** `put_file`, `get_file`, `put_dir`, `get_dir`, `artifact.upload`,
and `artifact.download` move bytes outside the action/observation hot path. Requirements:
binary frames or a side channel (not JSON/base64), chunk checksums, resumability, cancellation,
backpressure, per-session quotas, and p50/p95/p99 latency/throughput metrics. The capability layer
authorizes boundary scopes (`fs.scope`, host mounts, persistence, credentials); `.skn` records
content hashes and artifact references.

**Four thin adapters, generated from one IDL:**
1. **Gym/Gymnasium shim** — `reset()->(obs,info)`, `step()->(obs,reward,terminated,truncated,info)` for RL users.
2. **MCP server** — model-agnostic tools/resources (never the hot video loop).
3. **OSWorld `DesktopEnv` shim** — existing OSWorld tasks/agents run **unchanged** (subsumes OSWorld's runtime/message-passing).
4. **Vendor agent-loop adapters** — `AnthropicComputerAdapter` (`computer_20241022/0124/1124` + bash + text_editor), `OpenAIComputerAdapter` (`computer_call`/`computer_call_output`), UI-TARS — so any off-the-shelf CUA model drives Shinken unmodified.

---

## 7. Handshake & versioning

On connect the client sends `hello{v, client, accept}`; the runtime replies `welcome{server,
capabilities}` advertising `{schema_version, verbs, targets, observation_types, max_long_edge,
image_formats, binary_frames, observe_after_act}`. The client uses only advertised capabilities.
The ACI is semver-versioned; adapters are version-pinned.

### 7.1 Binary media frames (negotiated)

JSON-with-base64 is the wrong carrier for the media hot path: base64 inflates every frame ~33%
and forces the client to `json.loads` megabyte-class strings (measured to dominate the
client-plane CPU ceiling — see `docs/engineering/benchmarks.md` §6). A session that opts in via
`hello.accept.binary_frames` — against a runtime advertising `capabilities.binary_frames` —
therefore receives every **image-bearing observation** (one-shot screenshots, screencast frames,
dirty-tile delta frames) as one WebSocket **Binary** message:

```
u32 LE header_len | JSON header | payload area (raw codec bytes, concatenated)
```

The header mirrors the `observation` JSON with each image/tile base64 `ref` replaced by
`off`/`len` byte offsets into the payload area (`schema/aci.schema.json
$defs.BinaryFrameHeader`). Everything else on the wire — handshake, acks, results, queries,
structured observations — stays JSON text. The negotiation is strictly opt-in both ways: an old
client never receives bytes it can't parse, and an old runtime simply ignores the offer (the
SDK falls back to the text path transparently).

---

## 8. P0 vs later

| Area | P0 | Later |
|------|----|-------|
| Actions | typed schema + executor; Linux XTEST + CDP/AT-SPI routing + pyautogui-compat | Wayland (libei), Windows UIA/SendInput, macOS AX/CGEvent, background injection |
| Observation | screenshot baseline + screen video + focused-app capture + a11y/CDP reference tracks | the D13 operation layer (unified observe, stable-id diffs, settle, observe-after-act, app/window scoping), SoM/OmniParser service, multi-OS engines (macOS per D14, Windows UIA), hardware NVENC streaming pipeline |
| Replay | `.skn` event log + state snapshots; qcow2-eval (revert only) | video sidecar, mid-execution branching (fork tier), agent-core determinism |
| Harness | async Env+Operator core; Gym shim; OSWorld shim; 1 vendor adapter | MCP server, full adapter set, RL gym at scale |
| File transfer | `put_file/get_file` design + benchmarks; no base64 on hot paths | resumable directory sync, object-store handoff, artifact GC/dedup |
| API | `connect/observe/act/run/save/restore/close` + handshake | `fork/drive/unlock(capability)/events/video`, cloud `connect(id)` |

This spec is the contract M1–M4 implement against; changes here update the relevant ADR in
[05-tech-decisions.md](tech-decisions.md). Evidence + sources: [notes/p0-deepdive.md](../../notes/p0-deepdive.md).
