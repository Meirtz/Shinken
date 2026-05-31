# 11 — ACI Specification (the north-star interface)

> Status: drafted · 2026-05-30 · Siblings: [00 Vision](vision.md) · [02 Architecture](architecture.md) · [05 ADRs](tech-decisions.md) · [10 Phase-0 plan](../engineering/v0.0.1-plan.md) · wire schema [`../../schema/aci.schema.json`](../../schema/aci.schema.json)

The **Agent-Computer Interface (ACI)** is Shinken's product. This document is the north-star it is
built toward — the elegant surface, the typed action/observation model, the action-execution and
capture strategies, and the small async core contract that lets *any* harness drive Shinken. It
reconciles to decisions **D2** (actions), **D3** (observation), **D5** (replay), **D8** (interfaces).

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
the protocol, once. v0 verbs: `click, double_click, right_click, move, scroll, type_text, key,
screenshot, wait`. **Code-as-action** (`run`/bash/edit) is a separate, **off-by-default** capability
class behind the policy boundary (D6) — expressive, but gated and auditable.

### 3.1 Action execution taxonomy

The ACI action object is the **model-facing contract**, not the execution strategy. Shinken keeps the
surface small and typed, then routes each accepted action through an explicit capability boundary and
a backend router:

```text
agent/model grammar -> adapter -> typed ACI action -> capability boundary -> executor router -> backend
```

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

**Router priority at actuation:** `browser → CDP` · `element_ref with a valid a11y action → AT-SPI/UIA/AX` · `else pixel coordinate → XTEST/SendInput/CGEvent`. We do **not** make raw XTEST/pyautogui the sole strategy (coordinate-only, focus-stealing, X11-only). A `pyautogui`-compatible shim is kept so OSWorld's pyautogui action space runs unchanged.

---

## 4. Observation model (D3) — screenshot baseline, structure upgrade

Phase 0 is **screenshot-first** because that is the universal GUI-agent baseline. The first usable
loop is `screenshot -> model/adapter -> typed action -> screenshot`, with every step recorded to
`.skn`. The accessibility tree is a **parallel structured track** (our bandwidth/robustness
differentiator), not a prerequisite for the first GUI agent.

**One capture contract, three operations, one capture source per OS:**

```
screenshot(scope=screen|active_window|window:<id>, max_long_edge?) -> image
start_screencast(scope=screen|active_window|window:<id>, fps=0.1..30, max_long_edge?) -> stream
stop_screencast(stream)
```

- **macOS:** ScreenCaptureKit (`SCScreenshotManager` for stills incl. occluded per-window; `SCStream` for video).
- **Windows:** Windows.Graphics.Capture (WGC) — the only API doing per-window **and** occluded/background, for both stills and video.
- **Linux:** Wayland → `xdg-desktop-portal` ScreenCast + PipeWire (persist `restore_token`); X11 (our P0 fork tier) → XComposite per-window + XShm.
- **Encoder hand-off:** keep frames GPU-resident → GStreamer `nvcodec`/NVENC (neko-style, realtime-tuned). NVENC streaming runs on Ada L4/L40S, never A100/H100 (D11).

**Two paths off the same source:** (1) **screenshot-per-step**, downscaled to the model's true vision
resolution, for the agent loop; (2) **continuous video** for the human Control Panel. A structured
**observation event** (`a11y` full→diff with stable element refs) is recorded alongside screenshots
when available and can become the low-bandwidth default for tree-rich apps.

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

---

## 5. Runtime state + replay model (D5) — two artifacts, two contracts

The P0 deep-dive splits evidence from runnable state:

- **(A) `.skn` layered bundle** (cross-OS, the debug/audit/train artifact): append-only event log
  (the source of truth, = the live stream) + checkpoint/snapshot references + an **on-demand VIDEO
  sidecar**. Storage levers from day one: content-addressed `resources/<sha256>` dedup, full-snapshot
  + typed-delta observations (`a11y_delta`/`png_diff`). Wire form in [`../../schema/skn.schema.json`](../../schema/skn.schema.json).
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
capabilities}` advertising `{schema_version, verbs, targets, observation_types, max_long_edge}`. The
client uses only advertised capabilities. The ACI is semver-versioned; adapters are version-pinned.

---

## 8. P0 vs later

| Area | P0 | Later |
|------|----|-------|
| Actions | typed schema + executor; Linux XTEST + CDP/AT-SPI routing + pyautogui-compat | Wayland (libei), Windows UIA/SendInput, macOS AX/CGEvent, background injection |
| Observation | screenshot baseline + screen video + focused-app capture + a11y/CDP reference tracks | structured-default fast path for tree-rich apps, SoM/OmniParser service, multi-OS a11y, hardware NVENC streaming pipeline |
| Replay | `.skn` event log + state snapshots; qcow2-eval (revert only) | video sidecar, mid-execution branching (fork tier), agent-core determinism |
| Harness | async Env+Operator core; Gym shim; OSWorld shim; 1 vendor adapter | MCP server, full adapter set, RL gym at scale |
| File transfer | `put_file/get_file` design + benchmarks; no base64 on hot paths | resumable directory sync, object-store handoff, artifact GC/dedup |
| API | `connect/observe/act/run/save/restore/close` + handshake | `fork/drive/unlock(capability)/events/video`, cloud `connect(id)` |

This spec is the contract M1–M4 implement against; changes here update the relevant ADR in
[05-tech-decisions.md](tech-decisions.md). Evidence + sources: [notes/p0-deepdive.md](../../notes/p0-deepdive.md).
