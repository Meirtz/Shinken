# trycua/cua — code-level teardown + parity-and-surpass plan (training lens)

> Source: the git-ignored study clone at `references/cua/`, pinned at commit
> **`2925b491c20595ae850e3e4a05d6fea188e8f40a`** (2026-06-08, "Bump lume to v0.3.10",
> verified equal to `origin/main` on 2026-06-11). All `file:line` pointers below are into
> that clone. cua is public Apache/MIT-family OSS (MIT per `pyproject.toml`); naming and
> citing it is fine. Vendor-published numbers are labeled as such. Shinken-side numbers
> cite the measured suites in [`docs/engineering/benchmarks.md`](../docs/engineering/benchmarks.md).
>
> Companion: the short strategic capsule lives in
> [`docs/design/landscape.md` §2.1](../docs/design/landscape.md); this note is the
> raw code-level inventory behind it, plus the surpass plan. Lens: **training scenarios**
> (RL rollouts, fork-native resets, trajectory capture).

## 0. What ships where (monorepo map)

Everything is in the one monorepo — no separate lume/som/agent/docs repos worth cloning
(`pylume` on the org is archived; the docs-site source is `docs/` in-repo):

| Path (in `references/cua/`) | What | Language |
|---|---|---|
| `libs/python/computer-server/` | In-guest action server (FastAPI; WS + HTTP + PTY + MCP) | Python |
| `libs/python/cua-sandbox/` | Next-gen SDK: `Sandbox` + interfaces × `Transport` (11) × `Runtime` (8) | Python |
| `libs/python/computer/` | Legacy SDK (`Computer`, `VMProviderFactory`) — still shipped, overlaps the above | Python |
| `libs/python/agent/` | `ComputerAgent`: 20 registered model loops over litellm + callback pipeline | Python |
| `libs/python/som/` | Set-of-Mark grounding: YOLO icon detection + EasyOCR | Python |
| `libs/python/mcp-server/`, `computer_server/mcp_server.py` | MCP surfaces (30+ tools mounted at `/mcp`) | Python |
| `libs/cua-bench/` | Gym + benchmark + RL harness (workers, traces, TRL/Tinker trainers) | Python |
| `libs/lume/` | macOS VM manager (Apple Virtualization.framework), APFS-clonefile clone | Swift |
| `libs/lumier/` | Docker-packaged lume | — |
| `libs/cua-driver/` | Background MCP daemon driving native apps without stealing focus | Rust/Swift/Python |
| `libs/xfce/`, `libs/kasm/` | Linux container images: XFCE/KasmVNC + noVNC + computer-server | Docker |
| `libs/qemu-docker/` | QEMU-in-Docker images (linux/windows/android) | Docker |
| `libs/typescript/` | TS mirrors of computer/agent/core + playground | TS |

## 1. The action surface (computer-server)

One JSON command loop, **53 registered commands** (`handlers` dict,
`libs/python/computer-server/computer_server/main.py:257-322`) + 12 aliases
(`main.py:242-255`) + Android-only `multitouch_gesture` (`main.py:326-327`):

- **Mouse (8):** `mouse_down`, `mouse_up`, `left_click`, `right_click`, `double_click`,
  `move_cursor`, `drag_to`, `drag` (path-based). `middle_click` exists in the base handler
  (`handlers/base.py:250`) but is **not registered** in the dispatch table.
- **Keyboard (5):** `key_down`, `key_up`, `type_text`, `press_key`, `hotkey`.
- **Scroll (4):** `scroll(x,y)`, `scroll_down`, `scroll_up`, `scroll_direction`.
- **Screen (3):** `screenshot` (PNG/JPEG + quality, base64-in-JSON), `get_cursor_position`,
  `get_screen_size`.
- **Clipboard (2):** `copy_to_clipboard`, `set_clipboard` (pyperclip, `handlers/base.py:359-377`).
- **Shell (1):** `run_command` — unrestricted `create_subprocess_shell` with optional timeout
  (`handlers/base.py:380-431`).
- **Files (11):** `file_exists`, `directory_exists`, `list_dir`, `read_text`, `write_text`,
  `read_bytes` (offset/length), `write_bytes`, `get_file_size`, `delete_file`, `create_dir`,
  `delete_dir` — all base64-over-WS.
- **Window/app management (13):** `open`, `launch`, `get_current_window_id`,
  `get_application_windows`, `get_window_name`, `get_window_size`, `get_window_position`,
  `set_window_size`, `set_window_position`, `maximize_window`, `minimize_window`,
  `activate_window`, `close_window` (`handlers/base.py:113-186`).
- **Desktop (2):** `get_desktop_environment`, `set_wallpaper`.
- **Accessibility (2):** `get_accessibility_tree`, `find_element`.
- **Other:** `version`, `diorama_cmd` (macOS "App-Use" virtual desktops).

**Endpoints** (`main.py`): `WS /ws` (command loop, 10 MB message cap, `main.py:85`),
`POST /cmd` (HTTP fallback, SSE-framed single result, `main.py:613`), **PTY sessions**
(`POST /pty`, stdin/resize/kill, output over SSE or WS, `main.py:746-965`),
`POST /responses` (runs their own `ComputerAgent` *inside the sandbox* as a proxy,
`main.py:968`), `POST /playwright_exec` (in-guest Playwright browser commands,
`main.py:1301`, `browser.py`), `/mcp` (FastMCP mount, 30+ tools, `mcp_server.py`),
`GET /status`, `GET /commands` (introspection).

**Contract weaknesses (verified in code):**
- No typed schema: commands are strings; params are filtered by
  `inspect.signature` and **unknown params are silently dropped** (`main.py:574`,
  `main.py:694`). Typos get a "did you mean" suggester (`main.py:418`), not a schema error.
- Errors are `{"success": false, "error": str(e)}` — no taxonomy, no infra-vs-agent
  distinction (`main.py:586`).
- Auth: local mode = **no auth** ("local development mode", `main.py:347-352`); cloud mode
  validates one container-name+API-key pair against their SaaS per WS connection / per HTTP
  request (`main.py:330-389`). No per-action authorization.
- One command at a time; every action is a full WS/HTTP round trip (no batch verb).

## 2. OS handler reality (per-OS depth is very uneven)

| OS | Input backend | a11y | Notes |
|---|---|---|---|
| Linux | pynput + PIL `ImageGrab` (`handlers/linux.py:26-29,508`) | **Hardcoded stub**: returns a dummy `{"role":"Window","title":"Linux Window"}` tree (`linux.py:41-62`); `find_element` → "not supported on Linux" (`linux.py:64-80`) | The OS their containers actually run |
| macOS | Quartz/AppKit (`handlers/macos.py`, 1450 lines) | Real AX API tree (`macos.py:926`) + `diorama` per-app virtual desktops | The flagship platform |
| Windows | `handlers/windows.py` (772 lines) | Real, behind `WINDOWS_API_AVAILABLE` (`windows.py:73-88`) | |
| Android | adb + gRPC emulator control (`handlers/android.py`, 1109 lines) | — | multitouch gestures |
| VNC backend | `handlers/vnc.py` — drives any VNC target | stub | `CUA_BACKEND=vnc` (`handlers/factory.py:62-82`) |

The "any OS" pitch is real for *input/screenshot*; the **structured-observation story is
macOS/Windows-only**, and absent exactly where their cloud containers run (Linux).

## 3. Observation model: poll, don't push

- The only agent-facing observation is `screenshot` — full-frame PNG/JPEG, base64-in-JSON,
  captured with PIL `ImageGrab` per call (`handlers/linux.py:496-522`). JPEG support (with
  the q>95 clamp) is recent (`handlers/base.py:193-210`).
- **No server-push screencast, no delta/dirty-tile encoding, no damage-event capture, no
  binary frames** anywhere in `computer_server/` or the SDK transports. Human live viewing
  is offloaded to VNC/noVNC baked into the container images (`libs/xfce/Dockerfile`:
  XFCE + VNC 5901 + noVNC 6901 + API 8000) or KasmVNC (`libs/kasm/`).
- No screen *recording* command either; trajectory screenshots are whatever the agent loop
  saved per step (see §6).

## 4. Snapshot / fork / resume — the split-brain (receipts)

- **SDK-level `Sandbox.snapshot()` is cloud-only.** Local transports raise:
  `raise NotImplementedError("Snapshots are only supported for cloud sandboxes")`
  (`libs/python/cua-sandbox/cua_sandbox/sandbox.py:297-298`). This matches what we measured
  when probing the local path.
- **Cloud snapshot** = `POST /v1/vms/{name}/snapshot` against their closed SaaS backend;
  the code comments warn it "can take minutes on dir storage" (`transport/cloud.py:219-224`);
  `stateful=True` claims memory capture for VMs (`sandbox.py:290`, docs
  `docs/content/docs/cua/guide/sandbox/snapshots.mdx`). "Instant on btrfs" / CoW claims are
  **vendor-published, unverified** (`sandbox.py:288`, snapshots.mdx). Fork = boot a new VM
  from the snapshot (`transport/cloud.py:348-358`) — a full VM boot, not a warm resume.
- **Local runtime layer** has `ensure_base`/`fork`/`checkpoint` primitives
  (`runtime/base.py:81-103`), implemented **only by lume**: APFS `clonefile(2)` of
  `disk.img` (`libs/lume/src/FileSystem/Home.swift:274-283`) — instant CoW copy, but the
  source VM **must be stopped** (`libs/lume/src/LumeController.swift:300`;
  `runtime/lume.py:181-186` "stop it before using as a clone source") and the clone cold-boots.
  Their `checkpoint()` is stop → clone → restart (`runtime/lume.py:204-246`). This is the
  clone we probed at ~32 ms (file-level clone only; usable-VM time is a full macOS boot).
- Other local tiers degrade further: QEMU bare-metal suspend/resume = QMP
  `savevm`/`loadvm` + process relaunch (`runtime/qemu.py:596-683`); Docker "suspend" =
  `docker pause` (`sandbox.py:804-808`); DockerRuntime/QEMUDockerRuntime implement **no**
  fork/checkpoint at all.
- **Fork is not wired into the training/benchmark harness**: `cua-bench`'s
  `Environment.reset()` closes the session and **provisions a brand-new sandbox per task**
  (`libs/cua-bench/cua_bench/environment.py:156-186`) — no golden-checkpoint → fork-N path
  exists anywhere in the stack.

## 5. SDK shape (cua-sandbox, the new one)

- `Sandbox` facade with namespaced interfaces: `.mouse .keyboard .screen .clipboard .shell
  .files .window .terminal .mobile .tunnel .apps` (`cua_sandbox/sandbox.py:259-270`,
  `cua_sandbox/interfaces/`). Three lifecycles: `create` / `connect` / `ephemeral`
  (`sandbox.py:397-586`). Chainable immutable `Image` builder (`image.py`).
- **11 transports** (`cua_sandbox/transport/`): websocket, http, cloud, vnc, ssh, vnc+ssh,
  qmp, adb, gRPC-emulator, local(host), **osworld** (a ~75-line shim driving an OSWorld
  guest server, `transport/osworld.py`) — plus `localhost()` for unsandboxed host control.
- **8 runtimes** (`cua_sandbox/runtime/`): docker, qemu (docker-wrapped / bare-metal /
  WSL2), lume, hyperv, tart, android-emulator; auto-selected from the image
  (`sandbox.py:131-185`).
- `.tunnel.forward(port)` port-forwarding and `.apps.install()` per-OS app install
  (`interfaces/tunnel.py`, `interfaces/apps.py`).
- A legacy SDK (`libs/python/computer/`) with a different provider enum
  (lume/lumier/cloud/docker/winsandbox) still coexists — two overlapping SDK families.
- Telemetry (PostHog) is on by default in SDK, server, and bench
  (`cua_core.telemetry`; e.g. `sandbox.py:188-210`, `main.py:534-543`).

## 6. Agent framework + grounding + trajectory capture

- `ComputerAgent` (`libs/python/agent/cua_agent/agent.py:257`): model-string-routed
  registry of **20 loop modules** (`cua_agent/loops/`): anthropic (computer_20241022 →
  computer_20251124 tool versions, `loops/anthropic.py:58-91`), openai computer-use,
  UI-TARS 1/2 (text DSL `click(start_box='<|box_start|>(x,y)<|box_end|>')`,
  `loops/uitars.py:65-71`), qwen-3-VL/3.5, GLM-4.5V, InternVL, OpenCUA, Gemini, holo,
  moondream3, omniparser, gta1, fara, gelato, uiins, yutori, generic VLM, plus a
  `human_tool` HITL loop. All over litellm; adapters for HF-local, MLX, Azure ML
  (`cua_agent/adapters/`).
- **Composed grounding**: `composed_grounded.py` splits "thinking model + grounding model";
  the thinking model emits *element descriptions* ("red submit button") and a grounding
  model resolves them to coordinates (`loops/composed_grounded.py:28-92` tool schema).
  The `som` package (YOLO icon detection + EasyOCR, `libs/python/som/som/detect.py`) is the
  in-house grounder — ~0.4 s/frame on Apple-Silicon MPS, ~1.3 s CPU (vendor-published,
  `libs/python/som/README.md`).
- **Callback pipeline** (`cua_agent/callbacks/`): budget manager, image-retention,
  PII anonymization, logging, OTel, telemetry, **TrajectorySaverCallback**
  (`callbacks/trajectory_saver.py`) — saves per-turn artifacts + screenshots (PNG files
  keyed by `call_id`, crosshair-annotated on clicks, `trajectory_saver.py:430,468`).
- **cua-bench tracing**: HuggingFace-`datasets`-backed trajectory recorder — events +
  PIL images per row, `save_to_disk`/`push_to_hub` (`libs/cua-bench/cua_bench/tracing.py:17-58,228`).
  This is their training-data capture format.

## 7. Training story (cua-bench)

- Gym-style `Environment`: `make()/reset()/step()/evaluate()` (`cua_bench/core.py:20`,
  `environment.py:156,250,368`); tasks-as-code with decorated `tasks_config / setup_task /
  solve_task (oracle) / evaluate_task` functions (`environment.py:99-133`).
- **Parallel rollouts**: HTTP worker servers + `WorkerPool` spawning N workers on free ports
  (`cua_bench/workers/worker_manager.py:113,266`), `CBEnvWorkerClient`, and a
  `MultiTurnDataloader` with episode replay buffer + reward discounting, "compatible with
  RL training frameworks like TRL's GRPOTrainer" (`workers/dataloader.py:1-40`).
- **An actual RL trainer**: `cua_bench/trainer/off_policy/tinker/` — GRPO config, rollout
  collection via `cb run` subprocesses, checkpointing against the Tinker training API
  (`trainer/off_policy/tinker/{grpo,rl_loop,rollout,checkpoints}.py`).
- Session providers for the bench: docker, cloud, local_environment
  (`cua_bench/sessions/providers/`).
- **Reset cost**: as §4 — every `reset()` tears down and re-creates the sandbox
  (`environment.py:158-186`); rollout parallelism is worker-process-level, with no state
  reuse between episodes. No determinism story (wall-clock sleeps, live desktop).

## 8. cua-driver (separate surface, worth watching)

Rust/Swift MCP daemon that drives **native apps on the user's own desktop in the
background** without stealing focus — AX/UIA element handles + window-scoped screenshots,
per-session agent cursors (`libs/cua-driver/README.md`; Claude-Code-compat MCP mode).
Different product than the sandbox runtime, but its snapshot-keyed element-handle model is
the closest public analog to our D3 element_ref design.

## 9. Feature matrix — cua vs Shinken

Shinken status legend: **built** (CI-proven Linux/X11 slice), **partial**, **designed**
(docs only, not implemented — see [`docs/engineering/status.md`](../docs/engineering/status.md)).

| Capability | cua has | Shinken has | Shinken status |
|---|---|---|---|
| Action verbs | 53 string commands + 12 aliases (§1) — mouse/keyboard/scroll/screen/clipboard/shell/files/windows/desktop/a11y | 9 typed verbs (`move click right_click double_click scroll type_text key screenshot wait`, `shinkend/src/executor.rs:1364-1438`) + `start/stop_screencast` + queries + `act_batch`; schema-validated, unknown fields rejected | built (narrow by design); no clipboard/shell/window-mgmt verbs — see plan |
| Action transport | 1 RTT per command; WS JSON or HTTP-SSE; params silently filtered (`main.py:574`) | Single WS, typed envelope, `act_batch`, typed per-action failure taxonomy | built |
| Observation | Poll full-frame screenshot (PNG/JPEG base64); no push, no deltas (§3) | Screenshot (PNG/JPEG, `max_long_edge`, `scope=screen/active_window/window:<id>`) + **server-push screencast** with idle suppression, dirty-tile delta (~11×), binary WS frames (wire −25%), XDamage capture | built |
| Structured / a11y observation | Real AX tree on macOS/Windows; **stub on Linux** (§2); SOM grounder (YOLO+OCR) as separate package | `element_ref` in wire contract (resolution stubbed); SDK `observe_structured` + AT-SPI/CDP probes; coverage spike (#2) **measured (E5)** — verdict hybrid per-window, canvas measured zero | partial — D3 stays Provisional; guest observation engine unbuilt |
| Snapshot/fork/resume | Cloud-only `snapshot()` (NotImplementedError locally, `sandbox.py:297`); lume APFS clone of *stopped* VMs; QMP savevm; docker pause (§4) | **Local Docker disk-tier checkpoint/fork/resume**: checkpoint 0.53 s live, classic fork→usable 0.60 s, **warm-pool graft 0.118 s p50**, every replica state-verified ([benchmarks §1](../docs/engineering/benchmarks.md)) | built (disk tier); CRIU memory tier spiked POSITIVE (~300 ms fork e2e, `spikes/criu-memory-tier/`) but unbuilt; sub-ms CoW designed |
| Fork wired into harness | **No** — bench reset re-creates sandbox (`environment.py:158`) | `eval.run_eval_forked`: golden-checkpoint → fork-N → score | built |
| Streaming to humans | noVNC/KasmVNC sidecar in images | Same screencast plane serves operator view | built (no WebRTC/NVENC yet — designed D4) |
| Multi-OS substrates | macOS (lume/VZ), Linux (Docker/QEMU), Windows (QEMU/Hyper-V/winsandbox), Android (emulator); VNC/SSH to anything | Linux/X11 containers | built Linux-only; mac/Win/Wayland designed (D10) — cua clearly ahead |
| In-guest shell/exec | `run_command` + full PTY sessions (SSE/WS) (§1) | None in ACI (provider-level `docker exec` + injector only; `put_file`/`get_file` via provider) | gap — see plan |
| Clipboard | get/set verbs | None | gap — see plan |
| App/window management | 13 verbs + diorama app-isolation | Window *capture* scope only; no control verbs | gap — see plan |
| File transfer | 11 in-guest file verbs over WS base64 | `put_file`/`get_file` with sha256 receipts + capability gating (`sdk/python/src/shinken/client.py:1038-1057`) | built (host-side); in-guest file verbs not needed if exec lands |
| Agent-framework coupling | Tight: 20 model loops, litellm, callbacks; server can even run the agent in-sandbox (`/responses`) | Deliberately decoupled: narrow-waist `shinken.runtime` + thin provider adapters (Anthropic/OpenAI/Kimi-VL fixtures) | built (positioning difference, not a gap) |
| Trajectory capture | TrajectorySaver (per-turn JSON+PNG) + HF-Datasets traces w/ `push_to_hub` (§6) | `shinken.runtime.Trajectory` (typed steps, `exit_reason` precedence); `.skn` deferred (#216) | partial — no dataset-format exporter; see plan |
| Training harness | Real gym + worker pools + TRL-compatible dataloader + Tinker GRPO trainer (§7) | Workload registry + OSWorld workload + `run_eval_forked`; interop targets (verl/uni-agent, CUA-Gym) identified | partial — no in-tree trainer (by design); reset-from-fork is the wedge |
| Reset cost (training) | Fresh sandbox per reset; cloud snapshot-boot or local cold boot | Cold boot→usable 0.198 s; fork→usable 0.118–0.60 s; N=32 warm-pool fan-out 4.12 s ([benchmarks §1](../docs/engineering/benchmarks.md)) | built, measured |
| Step loop cost (training) | Poll-screenshot architecture (same class as OSWorld's server; unmeasured by them) | **5.37 ms p50 act+observe** (~36× vs OSWorld HTTP on identical guest, [benchmarks §7](../docs/engineering/benchmarks.md)) | built, measured |
| Parallel client plane | Worker subprocess per env | `SharedLoop`: 64 real sandboxes / 1,024 live sessions on one thread ([benchmarks §5–6](../docs/engineering/benchmarks.md)) | built, measured |
| Determinism | None (live desktop, wall-clock) | None yet; event-sourced replay + `.skn` designed (D5) | designed |
| Deployment | Local (Docker/QEMU/lume) + managed cloud SaaS | Local Docker; control plane designed | cua ahead on managed cloud |
| Interfaces | WS + HTTP + SSE + MCP (30+ tools) + REST lifecycle + TS SDK | Typed WS ACI + Python SDK + TS control-surface SDK; no MCP, no REST | gap (MCP) — see plan |
| Auth/permissions | One key per VM via SaaS; local = open; no per-action authz (§1) | Dev-token handshake; local capability-gateway shim (allow/ask/deny + envelope) | built (shim); production enforcement designed (D6) |
| Wire efficiency | base64-in-JSON everywhere; 10 MB WS cap | Binary frames (negotiated), delta tiles, idle suppression | built |
| Telemetry default | PostHog on by default across SDK/server/bench | None | — |

## 10. Surpass plan (training-first)

Effort classes: **S** = days, **M** = 1–2 weeks, **L** = multi-week+.

### 10.1 Close (cua capability → Shinken-native better design)

| # | cua capability | Shinken-native answer | Effort |
|---|---|---|---|
| G1 | `run_command` + PTY (`main.py:746-965`) | **Typed `exec` channel on the event plane**: `exec` verb with argv-array (not shell-string) default, streamed stdout/stderr as server-push events on the existing demux (same plane as screencast frames), typed exit status, capability-gated per the gateway shim; PTY = a stream kind later. Beats theirs: no second protocol (their PTY is a separate REST+SSE surface), no silent shell injection by default, gateway-auditable. | M |
| G2 | Clipboard get/set (`handlers/base.py:359`) | `clipboard_get`/`clipboard_set` typed verbs (X11 selections via the existing x11rb connection — no pyperclip subprocess dance), size-capped, in the capability envelope (clipboard is a data-exfil surface; cua gates nothing). | S |
| G3 | App-launch + window management (13 verbs, `handlers/base.py:113-186`) | Two typed verbs first: `launch` (argv + wait-for-window) and `window_list`/`activate_window` via X11 EWMH — we already enumerate windows for `scope=window:<id>` capture; reuse that path. Resize/minimize later only if a workload needs it. | S–M |
| G4 | In-guest file verbs (11) | Don't mirror them: `put_file`/`get_file` already cover host↔guest with sha256 receipts; in-guest manipulation falls out of G1 (`exec`). Document the equivalence. | S (docs) |
| G5 | Trajectory export (HF-Datasets traces, `tracing.py`; TrajectorySaver) | Exporter from `shinken.runtime.Trajectory` → parquet/HF-Datasets rows (events + screenshot refs), schema-versioned, with `exit_reason`/failure-taxonomy columns cua lacks. Makes Shinken rollouts directly consumable by TRL/verl-style trainers. | M |
| G6 | Gym `make/reset/step/evaluate` + worker pool (§7) | **Fork-native gym adapter**: `reset()` = warm-pool fork from the task's golden checkpoint (0.118 s) instead of their sandbox re-create; expose as a Workload-backed env class; interop with CUA-Gym TaskSource / uni-agent shapes rather than inventing a task format. This converts our measured fork advantage into the API trainers already speak. | M |
| G7 | MCP server (30+ tools) | Thin MCP server over the Python SDK (verbs + screenshot + checkpoint/fork as tools) — distribution surface, zero runtime change. | S–M |
| G8 | SOM grounding (som pkg) + composed-grounded loop | Stay the course on D3 (a11y/structured tier + `element_ref`) under the measured E5 verdict: **hybrid per-window** — structured where the tree is real (Qt/Chromium controls), pixel fallback where it is measured-zero (canvas; games unmeasured). SOM-style annotation is the natural SDK-side annotator for exactly those zero-tree windows. Their own Linux a11y stub (`linux.py:41`) is evidence the tree must be earned, not assumed. | M (annotator), engine L |
| G9 | Multi-OS (macOS VZ + clonefile, Windows, Android) | D10 roadmap; adopt their handler-factory shape (`handlers/factory.py`) and lume's stopped-VM-clone semantics as the macOS substrate design input. Not a v0.0.1 race. | L |
| G10 | Managed cloud + suspend/resume lifecycle | Control plane is designed (D12); near-term, keep publishing local-first numbers they can't match locally (their local snapshot() raises). | L |

### 10.2 Defend (Shinken exclusives — why each matters for training)

- **Typed contract + failure taxonomy** (`sandbox_died`, `exit_reason` precedence, schema-rejected unknown fields): trainers can drop infra-failed rollouts without poisoning reward signals — cua returns stringly `{"success": false}` and silently drops mistyped params (`main.py:574`).
- **5.37 ms/step act+observe loop** (~36× vs OSWorld-class polling; [benchmarks §7](../docs/engineering/benchmarks.md)): step cost is the RL throughput denominator; cua's per-step path is the same poll-PNG architecture we beat.
- **Harness-integrated fork** (`run_eval_forked`, golden→fork-N→score; warm-pool 0.118 s): resets are the other denominator; cua's reset re-provisions the sandbox (§4, §7) and their fork is cloud-only.
- **Server-push screencast + idle suppression + dirty-tile delta + binary frames + XDamage**: observation bandwidth at fleet scale (64 sandboxes/1,024 sessions measured) — cua ships none of it.
- **`SharedLoop` client plane**: one thread drives 1,024 sessions — their worker model is a subprocess per env.
- **Local-first measurements**: every number above is rerunnable from `benchmarks/` on one machine; their fork/snapshot numbers require their SaaS and are vendor-published.

### 10.3 Priority — the 3 gaps to close first for training users

1. **G1 — typed `exec` channel** (M). Every benchmark family (OSWorld setup/verify, CUA-Gym
   verifiers, cua-bench `setup_task`) needs in-guest execution; today it leaks through
   provider `docker exec`/injector, which won't survive non-Docker substrates. It is also
   the prerequisite that makes G4 free.
2. **G6 — fork-native gym adapter** (M). The measured fork advantage is invisible to
   trainers until `reset()` *is* a fork. This is the one feature that converts our wedge
   into the API the trainer camp (TRL/verl/uni-agent) already consumes — and cua's own
   harness proves nobody else has wired it.
3. **G2+G3 — clipboard + launch/activate verbs** (S–M combined). Cheap task-parity items:
   a large share of OSWorld-class tasks touch clipboard or app launch; without them,
   workloads shell out around the ACI and the typed-contract story leaks.

## 11. Open questions / watch items

- Their cloud "stateful" (memory) snapshot flag (`sandbox.py:290`) — if it becomes real and
  fast, it answers part of our CRIU tier; claims remain unverified.
- `cua-driver`'s element-handle MCP surface is converging on our D3 design from the
  desktop-assistant side; its small-tree pixel-fallback heuristic independently corroborates
  the E5 hybrid verdict.
- The Tinker GRPO trainer (§7) makes them the first sandbox vendor shipping an in-tree RL
  loop; our counter is interop + reset cost, not a competing trainer.
