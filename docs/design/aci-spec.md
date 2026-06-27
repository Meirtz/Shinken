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

Every coordinate-aware one-shot screenshot observation (including observe-after-act) carries this
frame descriptor (the field is optional on the wire only for compatibility with older/legacy
backends):

```jsonc
"display": {
  "origin": "top-left",
  "w": 1920, "h": 1080, "dpr": 1.0,                 // global point_px action space
  "source_rect": {"x": 320, "y": 180, "w": 800, "h": 600}, // pre-scale global crop
  "delivered": {"w": 400, "h": 300}                // pixels actually shown to the model
}
```

`point_px` and `point_norm` on the ACI wire remain **global-screen actions**. When a target instead
comes from a delivered frame, the SDK maps a delivered pixel `p` to
`source_origin + floor(p * source_size / delivered_size)`; normalized coordinates map across
`[0, source_size - 1]`, so `1.0` lands on the final source pixel. This covers both a scoped window
and `max_long_edge` downscaling without inferring scale from image dimensions. Callers must provide
the exact observation used by the model (`frame=`); absent metadata is a typed client error, never a
guessed click. X11 reports the root-window offset for `active_window`/`window:<id>` and `dpr=1`.
Backends whose capture and actuation units have not been proven equivalent (notably scoped Retina
capture) omit `display` rather than fabricate a mapping.

Coordinate normalization therefore lives in the protocol/SDK boundary, once. v0 verbs (**22**,
matching the `Verb` enum in
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
| In-guest exec (G1, §3.4) | `exec` |
| Desktop (G2+G3) | `clipboard_get`, `clipboard_set`, `launch_app`, `activate_window` |
| Timing | `wait` |

`drag` is the composed
gesture — pointer down at `target`, interpolated moves, up at `to`, with optional `duration_ms`
(clamped guest-side) and `button` (`left`/`middle`/`right`); `mouse_down`/`mouse_up` are its
decomposed halves for free-form gestures (down → moves → up), `target` optional (absent = act at
the current pointer position). `observe` captures the structured (a11y) tree with stable element
ids, diff rendering, and settle (§4.1); `invoke_action`/`set_value` drive an element's
accessibility interface directly (the AX-path fallback to physical events — §3.2 router);
`exec` is the typed in-guest exec channel (§3.4 — argv default, shell opt-in, buffered or
streamed output, gateway-audited). The broader **code-as-action** class (`run`/bash/edit
profiles) remains a separate, **off-by-default** capability class behind the policy boundary
(D6) — expressive, but gated and auditable.

**Desktop verbs (G2+G3, task parity).** A large share of OSWorld-class tasks touch the clipboard
or start an application; without typed verbs, workloads shell out around the ACI and the
typed-contract story leaks. Four verbs close that gap:

- `clipboard_get` → the runtime answers a typed `result` whose value is `{text}` (the read's data
  channel — an `ack` carries no payload). `clipboard_set {text}` acks. **v1 is text-only and
  size-capped guest-side** (1 MiB; an INCR/oversized transfer is a typed error, never a silent
  truncation); binary clipboard formats are future targets. Linux v1 speaks the ICCCM selection
  protocol itself (`shinkend` owns the `CLIPBOARD` selection on a dedicated worker thread and
  serves `TARGETS`/`UTF8_STRING`/`STRING`; reads via `ConvertSelection` with a bounded wait and one
  `STRING` retry) — **no `xclip` subprocess dance**. The clipboard is a data channel, so the SDK
  gateway gates BOTH directions on the envelope's `clipboard` capability (default-off).
- `launch_app {app, args?}` spawns `app` (a PATH name or absolute path; `args` verbatim — never
  through a shell) detached with the session environment (Linux v1: `$DISPLAY` + the session D-Bus
  address `shinkend` itself runs under, so the app lands on the sandbox desktop and its a11y tree
  reaches the session bus). The ack carries no window: find it via `list_windows` (title /
  `_NET_WM_PID`). Gateway capability: `app_launch` (default-granted, deniable per session).
- `activate_window {window_id | app}` raises + focuses a window — by `list_windows` id, or by `app`
  (first case-insensitive title substring match). Linux v1 sends the EWMH `_NET_ACTIVE_WINDOW`
  client message (source = pager/user); on a WM-less display (bare Xvfb) it falls back to
  raise + set-input-focus, and `active_window` (the capture scope and the `focused` flag) gains the
  matching input-focus fallback so activation is observable without a WM.

macOS v1 answers all four with a **typed unsupported error** (the native paths need
NSPasteboard/NSWorkspace via AppKit bindings the v1 engine does not carry — see
[macos-engine.md](../engineering/macos-engine.md)); the verbs stay advertised as vocabulary, and
backend support is a runtime condition answered honestly, like AT-SPI availability.

**Act-returns-observation (`observe` argument).** Every *mutating* verb (the schema's
`MutatingVerb` set — pointer/keyboard/gesture, the element verbs, and the desktop writes
`clipboard_set`/`launch_app`/`activate_window`; not
`screenshot`/screencast/`observe`/`wait`/`clipboard_get`) accepts an optional `observe` object carrying
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

**v0.0.1 scope (settled 2026-06, D2; amended 2026-06-11):** the typed in-guest **`exec` wire verb
is now BUILT** (§3.4 — the first slice of the CLI/code-action class, behind the gateway's `exec`
capability with the argv/shell recorded in the audit event); every benchmark family's setup/verify
(OSWorld, CUA-Gym verifiers, swerex sessions) can now flow in-band instead of leaking through the
substrate channel, and the integrations prefer it when advertised (the `docker exec`/inject
fallback is kept for pre-exec runtimes). The GUI-shaped `launch_app` (spawn an app on the desktop,
no output channel) also shipped as a typed verb in §3. File transfer stays substrate-side (the
SDK's `put_file`/`get_file` are not wire messages); typed `put_file`/`get_file` wire verbs
remain deferred. `element_ref` resolution is **guest-side on Linux** as of the M1b observation engine: the
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
11-verb enum with an **element verb family**. The core of it is now **built and in the 22-verb
enum** (§3): `drag`, the `observe` wire verb, the element verbs — shipped under the shorter
wire names `invoke_action`/`set_value` (the canon's `invoke_element_action`/`set_element_value`) —
and the G2+G3 desktop verbs (`clipboard_get`/`clipboard_set`, `launch_app`, `activate_window`),
with `windows` shipped as the `list_windows` query. The **remainder is designed-only** — additive,
capability-negotiated (`welcome.capabilities.verbs`), absent from `schema/aci.schema.json` until
built: `set_text_selection`, `scroll_element`, the `apps` query, and the per-app observe
selector. Illustrative, schema-ready shapes (designed forms; the built verbs use the §3/§4.1 wire
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

### 3.4 Typed in-guest exec channel (built)

The **`exec` verb** runs a command inside the Sandbox as a child of `shinkend`, over the SAME
WebSocket as every other action — one protocol, no second surface — so setup/verify works on any
substrate a runtime runs on (a remote `shinkend` over WS where `docker exec` does not exist).
Discipline: **`argv` is the default form** (the program is executed directly — no shell
interpretation, no silent injection surface); **`shell` is the explicit opt-in** (`/bin/sh -c`),
mutually exclusive with `argv`. `cwd`/`env` shape the child; `timeout_ms` (runtime default 60 s,
hard-capped) kills the child's **whole process group** on expiry and is reported honestly
(`timed_out: true`, null exit code — never disguised as an exit status); `stdin` is written and
closed. Concurrency is runtime-bounded (a small semaphore; saturation is a nack). Two reply forms:

```jsonc
// buffered (default): one typed `result` — ok:true even on a nonzero exit code
// (the COMMAND failed, not the action); value = $defs.ExecResult
{ "verb": "exec", "argv": ["python3", "/tmp/reward.py"], "cwd": "/home/user",
  "env": {"TASK_ID": "t1"}, "timeout_ms": 30000 }
// → { "type": "result", "ok": true, "value": { "exit_code": 0, "signal": null,
//     "timed_out": false, "stdout": "REWARD: 1.0\n", "stderr": "",
//     "stdout_truncated": false, "stderr_truncated": false, "duration_ms": 412.7 } }

// streamed: ack, then incremental `exec_output` events on the existing demux
// (same plane as screencast frames; raw-byte binary frames on a binary-negotiated
// session, base64 `data_b64` text events otherwise), terminated by ONE `exec_exit`
{ "verb": "exec", "shell": "make test 2>&1", "stream": true }
// → ack, then: { "type": "exec_output", "cause": "<call_id>", "seq": 0,
//                "channel": "stdout", "data_b64": "…" } …
// → { "type": "exec_exit", "cause": "<call_id>", "exit_code": 2, "timed_out": false,
//     "duration_ms": 9120.4, "truncated": false }
```

Output is budgeted honestly: the buffered form caps each channel (256 KiB) with per-channel
truncation flags; the streamed form caps chunks (64 KiB) and the total (8 MiB), flags `truncated`
on the exit event, and always drains the pipes so the child never deadlocks. `seq` is monotonic
across BOTH channels, so total output order is reconstructible. The gateway maps the verb to the
**`exec` capability** and records the argv/shell in the decision event's `detail`, so the
permission audit shows *what* was run. **`pty` is a reserved field** (only `false` validates):
PTY allocation is the designed follow-up — a stream kind over these same events, not a second
protocol. SDK surface: `env.exec(["ls","-la"])` → the typed result dict;
`env.exec_stream(...)` → a sync/async iterator of byte chunks ending in the exit item.

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

**Pointer metadata, not pointer pixels (built).** Captures are **cursor-free** on every engine
(neither X11 `XGetImage` nor CoreGraphics composites the hardware cursor) — deliberately:
cursor pixels would break frame-hash dedup (a moving cursor makes forked replicas'
byte-identical frames diverge) and idle suppression. The field splits here — e2b-desktop
(`scrot --pointer`) and Anthropic's quickstart (`gnome-screenshot -p`) burn the pointer into
the pixels so the model can see it; OSWorld/cua/codex-style AX servers don't (the codex-style
co-use reference captures with `showsCursor = false` and shows the *human* a software overlay
cursor instead). Shinken serves the model's need structurally: one-shot `screenshot` (and
`not_modified`) observations carry **`pointer: [x, y]`** — the live pointer position in
capture pixels as observation METADATA, omitted on screencast stream frames and on backends
that cannot report it. The frame stays clean; the model still knows where the pointer is.

**Content-negotiated screenshots (frame dedup, built).** A `frame_dedup` runtime stamps every
one-shot screenshot with a `frame_hash` computed over the RAW pixels (post-scope/downscale,
pre-encode — codec-independent, so a hash minted under PNG matches a JPEG request over the same
framebuffer). A client echoes the last seen hash back as the screenshot action's `if_none_match`;
on a match the runtime skips the encode and answers a compact `not_modified` observation instead
of the payload. Capability-negotiated both ways (`capabilities.frame_dedup`), and the fleet form —
one shared client-side `FrameCache` across N forked replicas — is what turns fork's
pixel-identity-by-construction into an N× observation cut (see
[benchmarks §10](../engineering/benchmarks.md)).

- **The hash is an opaque token on the wire.** Clients MUST echo it verbatim and never parse it.
  Current format: XXH3-128 as 32 lowercase hex chars, dims folded in as the seed (it was fnv1a-64 /
  16 chars before v0.0.1 hardening); matching is string equality, so a hash minted under any other
  format/version simply never matches and the answer degrades to a full frame — the safe direction.
  Old-client/new-runtime and new-client/old-runtime mixes therefore need no negotiation beyond
  `frame_dedup` itself.
- **Collision failure mode, and why 128 bits.** A hash collision is the design's one
  silent-wrong-answer path: the runtime would answer `not_modified` for pixels that differ from the
  client's cached frame, and the agent would act on a stale screen with no error surfaced anywhere.
  At 64 bits the accidental birthday bound across a fleet cache's accumulated candidates was ~2³²
  frames — too close for a long-lived training fleet — and FNV-1a's algebraic structure made
  colliding inputs easy to construct even without trying to be unlucky. At 128 bits the accidental
  bound is ~2⁶⁴ frames (a fleet that observes 10⁹ distinct screens accumulates ~10⁻²⁰ total
  collision probability); XXH3 is not a cryptographic hash, which is accepted deliberately: the
  screen content here is produced by the sandbox's own desktop for the session's own client, so
  robustness against accidental/pathological content is the requirement. **Verify-on-hit** (pin the
  raw pixels behind every served hash and memcmp before each `not_modified`) is the paranoid
  fallback if that posture ever changes; it was rejected for v0 because the hit is cross-session by
  design (the fleet cache lives client-side — one runtime cannot hold the frames other replicas
  minted), and pinning ~3 MB of raw pixels per session to guard a ~2⁻⁶⁴-per-candidate event
  re-introduces the memory and latency the dedup exists to remove.

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
  "display": { "origin": "top-left", "w": 1920, "h": 1080, "dpr": 1,
               "source_rect": {"x": 320, "y": 180, "w": 1280, "h": 800},
               "delivered": {"w": 1280, "h": 800} },
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

The Linux v1 subset of §4.1 is built in the guest runtime: AT-SPI trees, stable element refs,
tree-text diffs, settle, guest-side ref actions, `invoke_action`, and `set_value`. The
combined pixel+tree envelope illustrated above, macOS permission-pending observation,
Windows UIA/macOS AX, and in-guest CDP remain designed-only; SDK-side AT-SPI/CDP paths remain
compatibility fallbacks. See [status.md](../engineering/status.md).

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
  replace it. What a snapshot *captures* is advertised per provider as `snapshot_kind`
  (`none | disk | memory | process | provider_managed`): the Docker reference tier is `disk`
  (`docker commit`, files only), and the built CRIU tier is `process` — the live process tree
  (memory, threads, FDs, X11 clients) paired with a disk commit, restored into a fresh privileged
  container (`requires_privileged=True` is advertised alongside; a latency/state-fidelity tier,
  not an isolation posture). Established TCP is closed at dump (`--tcp-close`); agent sessions
  reconnect via the screencast `resume_stream` semantics (§ above).
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
