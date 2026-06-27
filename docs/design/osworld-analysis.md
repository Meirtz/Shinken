# 03 — OSWorld Teardown (Design Input)

> **Status:** design input · **Date:** 2026-05-30 · **Audience:** Shinken contributors
> **Companion docs:** [02-architecture.md](architecture.md) · [04-landscape.md](landscape.md) · [05-tech-decisions.md](tech-decisions.md) · [08 Isolation & capability note](threat-model.md)

OSWorld ([os-world.github.io](https://os-world.github.io/), paper [arXiv:2404.07972](https://arxiv.org/abs/2404.07972), code [github.com/xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld)) is the most influential open benchmark for computer-use agents: ~369 real-desktop tasks across Chrome, LibreOffice, GIMP, VLC, and multi-app workflows, scored by executable checks against a live OS inside a VM. It earned that status honestly — it was the first widely adopted harness to run *real* applications on a *real* desktop and grade *real* end states. State-of-the-art reported scores have climbed from single digits to ~83% on the OSWorld-Verified variant ([xlang.ai/blog/osworld-verified](https://xlang.ai/blog/osworld-verified)) (vendor-published, unverified), now reportedly above the ~72.4% human baseline.

Shinken is the full CUA infrastructure stack — the runtime that benchmarks and harnesses like
OSWorld plug into: **one runtime** that serves production computer-use agents, evaluation, and
trajectory-data capture, layered. This document is the load-bearing design input for that claim. We read OSWorld's
source end-to-end — the in-VM Flask server, the client gym `Env` and controllers, the multi-cloud
providers, the evaluators/getters, and the `mm_agents` layer — to extract exactly **what to keep**
and **what is too primitive**, then map every weakness to the Shinken decision (D-ref, see
[05-tech-decisions.md](tech-decisions.md)) that fixes it. We cross-reference against the closest
modern competitor, **trycua/cua**, which already fixed several of OSWorld's mistakes and so usefully
calibrates "table stakes" vs. "true differentiation."

This is not a takedown. OSWorld's *data-model* instincts — declarative task config, a getter/metric registry, snapshot-based reset, a uniform in-VM control API — are good and we keep them. What we replace is the *transport and runtime*: blocking HTTP polling, full-frame PNGs, code-as-action, an unauthenticated arbitrary-execution server with no identity or scoping, X11/Linux-only assumptions, and slow snapshot-revert.

---

## 1. How OSWorld works, end to end

OSWorld is a Gym-style harness wrapped around a real desktop OS inside a VM. The whole system has five layers; the seams between them are clean, but the I/O model gluing them together is synchronous HTTP polling.

### 1.1 In-VM execution server (Flask + pyautogui/pyxcursor)

Inside each guest VM runs a single ~1,800-line Flask file (`desktop_env/server/main.py`) installed as a systemd unit (`osworld_server.service`). It binds `0.0.0.0:5000` with `app.run(debug=True, host='0.0.0.0')` (`main.py:1797`) — the Flask **development** server, with the Werkzeug interactive debugger enabled, on all interfaces. It exposes ~30 unauthenticated routes:

- **Observe:** `GET /screenshot` renders a full-frame PNG to disk and `send_file`s it back (`main.py:263–333`), compositing the cursor via an X11-only `XFixesGetCursorImage` ctypes binding (`pyxcursor.py:52–66`). `GET /accessibility` walks the AT-SPI / UIA / AX tree and returns the **entire tree serialized as one XML string inside a JSON field** (`main.py:902–964`). `GET /terminal` scrapes the active terminal — Linux-only, returns HTTP 500 elsewhere (`main.py:361–363`).
- **Act/Exec:** `POST /execute` runs `subprocess.run` on a caller-supplied command, with the in-source comment *"Execute the command without any safety checks"* (`main.py:92`). `POST /run_python`, `/run_bash_script`, and `/setup/launch` are likewise arbitrary execution. There is **no dedicated input route** — keyboard/mouse actions are pyautogui Python source strings shipped to `/execute`.
- **Files / setup:** `POST /file` (arbitrary read), `/setup/upload` (arbitrary write), `/setup/download_file` (server-side fetch of any caller-supplied URL), plus window-management and wallpaper routes.
- **Record:** `POST /start_recording` spawns `ffmpeg -f x11grab` to a single global `/tmp/recording.mp4`; `recording_process` is one module global (`main.py:72`), so the server supports exactly one recording and implicitly one client.

It *tries* to be tri-platform (Linux/Windows/macOS branches throughout) but is overwhelmingly X11/Ubuntu-centric; several non-Linux paths are stubs or carry real bugs (e.g. `/screen_size` references an unbound variable on macOS; Windows a11y uses namespace keys that are never defined; `/run_python`'s error path calls an unimported `traceback`).

### 1.2 Client gym `Env` + controllers

The host side is a Gymnasium-style `DesktopEnv` (`desktop_env/desktop_env.py`) exposing `reset(task_config)`, `step(action)`, `render()`, `evaluate()`, `close()`. It wires a `PythonController` and a `SetupController`, both of which are thin HTTP clients pointed at `http://<vm_ip>:5000`.

The control loop (`lib_run_single.run_single_example`) is a strictly synchronous poll:

```
reset(task_config)
  └─ revert snapshot → boot VM → rediscover IP/ports → rebuild controllers → replay setup config
sleep(60)                       # hardcoded "wait for the environment to be ready" (lib_run_single.py:26)
obs = env._get_obs()            # 3 serial blocking GETs: /screenshot, /accessibility, /terminal
loop until max_steps:
  response, actions = agent.predict(instruction, obs)
  for action in actions:
    env.step(action)            # POST /execute  →  time.sleep(pause≈2s)  →  re-GET full obs
    log step_N.png + traj.jsonl line
sleep(20)                       # hardcoded settle (lib_run_single.py:64)
score = env.evaluate()          # getters reach back into the VM, run metrics on the host
```

`reset` re-discovers the VM by parsing a colon-packed string — `get_ip_address` returns `host:server:chromium:vnc:vlc`, sliced with `rsplit(':', 4)` (`desktop_env.py:202–211`); IPv6 corrupts the older variants. `step` hardcodes `reward=0, done=False` (`desktop_env.py:418–419`), so the "Gym" contract is cosmetic — there is no per-step signal, only a terminal `evaluate()`. Three near-duplicate `Env` variants exist, differing only in small fixes.

### 1.3 Multi-cloud providers + snapshot-revert

Every backend hides behind a tiny five-method `Provider` ABC (`providers/base.py:11–44`): `start_emulator`, `get_ip_address`, `save_state`, `revert_to_snapshot`, `stop_emulator`, plus a `VMManager` registry/pool ABC. Concrete providers: VMware, VirtualBox, Docker, AWS, Azure, Aliyun, Volcengine, FastVM (and an **empty 0-byte GCP stub** still wired into the factory).

The key per-task primitive is `revert_to_snapshot`, but its meaning leaks badly across implementations:

| Provider | `revert_to_snapshot` actually does | Reset cost |
|---|---|---|
| VMware | `vmrun revertToSnapshot` (true in-place) | seconds |
| VirtualBox | `VBoxManage` snapshot restore | seconds |
| AWS | **terminate** EC2 + `RunInstances` fresh from an AMI (`aws/provider.py:142–198`), wait `instance_running` | minutes |
| Azure | deallocate, delete disk, create disk from snapshot, swap, recreate | multi-minute |
| Docker | **no snapshot** — `save_state` raises `NotImplementedError`; revert == destroy+recreate from a read-only qcow2 (`docker/provider.py:150–154`) | container restart |
| FastVM | delete microVM + relaunch from snapshot id; sub-second restore | ~0.5–0.7 s (vendor-published, unverified) |

There is **no fast fork/clone, no copy-on-write sharing, no warm pool, and no GPU plumbing** anywhere in the core path. VMware "clones" by re-downloading and unzipping a multi-GB qcow2 and rewriting the MAC; AWS launches full 30 GB gp3 volumes per instance. FastVM (a third-party bolt-on requiring public IPv6 and a one-time image "bake") is the only thing that approximates CoW, and even it does delete-and-relaunch rather than fork. The colon-packed IP string is the only contract for IP/port discovery — a string-typing hack, not a typed handle.

### 1.4 Evaluators / getters

Each task is a self-contained JSON: `{ id, instruction, snapshot, config: [...setup ops], evaluator: { func, conj, result, expected, options, postconfig } }`. The `config` list provisions the environment (download/upload files, launch apps, seed Chrome history via CDP, run shell). The `evaluator` block scores it.

Scoring is **stringly-typed reflection**. `DesktopEnv._set_evaluator_info` (`desktop_env.py:364–409`) resolves the metric `func` via `getattr(metrics, func)` and each result/expected type via `getattr(getters, 'get_' + type)`. At grade time, `evaluate()` runs the getters (which reach back **into the VM** over the same Flask server — e.g. `get_vm_command_line` POSTs to `/execute` and reads stdout), feeds the fetched state to the metric, and combines multiple metrics with an AND (mean) or OR (max) conjunction into a single float. The getters and metrics are hand-coded against live app internals (see §3.6); this is exactly the fragility that produced OSWorld-Verified's 300+ documented grader fixes ([xlang.ai/blog/osworld-verified](https://xlang.ai/blog/osworld-verified)).

### 1.5 `mm_agents`

The agent layer is one Python class per model, all implementing an informal `predict(instruction, obs) -> (response, actions)`. Observations come in four flavors: raw `screenshot`, `a11y_tree` (XML linearized to a TSV table, blindly truncated at ~10k GPT-4 tokens mid-row), `screenshot_a11y_tree`, and `som` (set-of-marks — numbered boxes drawn from filtered a11y nodes). There are **at least five incompatible action representations** — `computer_13` JSON, raw pyautogui code, the Anthropic computer-use tool, the OpenAI `computer_call` tool, and the UI-TARS DSL — each with its own parser and its own ad-hoc translation down to a pyautogui code string `exec`'d in the VM. Every new model adds another bespoke agent file, and the control loop itself is copy-pasted into **~15 near-identical `run_single_example_*` variants** (`lib_run_single.py`) plus dozens of `run_multienv_*` scripts.

A revealing detail: OSWorld's own git submodules (`mm_agents/surferH`, `rdds`, `agp_client`) point at a *different* architecture — a persistent Remote-Desktop-Driver-Server on `:8087` and a hosted agent platform that drives the VM directly, with OSWorld reduced to provisioning + scoring (`surfer_agent.py:38–86`). The authors clearly know the poll-the-screenshot loop is not how a production agent should run.

```
┌────────────────────────── HOST ──────────────────────────┐        ┌──────── GUEST VM ────────┐
│  run_multienv.py  (process-per-VM workers)                │        │                          │
│   └─ DesktopEnv (gym facade)                              │        │  Flask main.py :5000      │
│        ├─ PythonController ──── HTTP GET /screenshot ─────┼──poll──▶│   pyautogui + pyxcursor   │
│        │                  ──── HTTP GET /accessibility ───┼──poll──▶│   AT-SPI / UIA / AX       │
│        │                  ──── HTTP POST /execute ────────┼──exec──▶│   subprocess.run (RCE)    │
│        ├─ SetupController ──── HTTP /setup/* ─────────────┼────────▶│   ffmpeg x11grab → mp4    │
│        └─ Provider (vmware/aws/docker/...) snapshot-revert │        │                          │
│   PromptAgent.predict() → pyautogui code strings           │        └──────────────────────────┘
└───────────────────────────────────────────────────────────┘   side channels: VNC 8006/5910, CDP 9222, VLC 8080
```

---

## 2. What to keep

OSWorld earned its position. Several of its design instincts are correct and Shinken inherits them — generalized, typed, and hardened.

| OSWorld pattern | Where | Why we keep it | Shinken home |
|---|---|---|---|
| **Declarative task config** (`config` setup ops + `evaluator` block as JSON) | `desktop_env.py:354–409` | Portable, data-driven, forkable unit of "environment-prep + scoring." | D7 typed task schema |
| **Getter/metric two-stage scoring** (fetch observable state, then a pure comparison; result-vs-expected duality) | `evaluators/{getters,metrics}` | Cleanly separates introspection from comparison; same metric serves static-answer and golden-file tasks. | D7 verifier DAG |
| **Composable and/or conjunction** of multiple checks | `desktop_env.py:453–519` | Simple way to express compound success criteria. | D7 |
| **Snapshot-based reset to a known-clean state per task** | `_revert_to_snapshot`, providers | The right *invariant* (reproducible initial conditions), even though the *implementation* is slow. | D1 fork-from-snapshot; D7 golden snapshot |
| **`is_environment_used` dirty-flag** (skip needless reverts) | `desktop_env.py:152–160, 293–302` | Sound cost optimization; generalize to per-resource dirty tracking. | D9 Fleet Manager |
| **Pluggable Provider/VMManager seam** (lifecycle vs. allocation) | `providers/base.py` | The single clean abstraction. We *widen* it (own pooling/streaming), but keep the boundary. | D1 substrate-pluggable; D9 |
| **Uniform in-VM control API** (host talks to one well-known endpoint regardless of guest OS) | `server/main.py` | Right concept: a thin guest daemon. We keep the *idea*, replace HTTP-poll with a streaming transport. | D2/D4 Guest Runtime `shinkend` |
| **Per-OS a11y normalization** into one namespaced, query-addressable tree | `main.py:370–403` | Normalizing AT-SPI/UIA/AX into one query language for grounding/eval is genuinely useful. | D3 Rung-0 `Element` schema |
| **Set-of-Marks grounding** (numbered boxes → reference `tag_N` instead of raw pixels) | `agent.py:197–214` | Sidesteps brittle absolute-coordinate prediction. | D3 Rung-1 |
| **Resumability via on-disk markers** + **dead-worker auto-restart** + **graceful VM teardown** | `run.py:234–268`, `run_multienv.py:333–388` | Cheap, crash-tolerant checkpointing and a solid fault-tolerance skeleton. | D9 control plane |
| **Secret injection at boot** (upload + `chmod 600`, not baked into image) | `desktop_env.py:223–246` | Good hygiene; generalize into a real broker. | D6 Vault/KMS + proxy header-injection |
| **`execute_with_verification`** (couple an action with a settle predicate) | `main.py:120–224` | A real readiness primitive worth promoting over fixed sleeps. | D7 readiness probes |
| **The surferH/RDDS submodule direction** (persistent driver + remote agent + streaming; harness only provisions + scores) | `surfer_agent.py:38–86` | This *is* the next-gen shape OSWorld gestures at. Shinken makes it the default, not a side path. | D2/D4/D12 |

The competitive lesson from trycua/cua: it already adopted several of these correctly (typed action dispatch, structured a11y dicts, trajectory recording, a per-OS handler factory). Those are now **table stakes**, not differentiation — Shinken must match them and win elsewhere ([04-landscape.md](landscape.md)).

---

## 3. What is too primitive — and the decision that fixes it

Each weakness below is grounded in `file:line` and mapped to the Shinken decision (D-ref) that closes it.

### 3.1 Unauthenticated Flask `:5000` — no identity, no scoping

**The problem.** The in-VM server is an unauthenticated, plaintext-HTTP, arbitrary-command-execution surface bound to `0.0.0.0`:

- `POST /execute` runs `subprocess.run` on caller-controlled input, commented *"without any safety checks"* (`main.py:92`); `/run_python`, `/run_bash_script`, `/setup/launch` are equally open.
- `/file` is arbitrary file read, `/setup/upload` arbitrary write, `/setup/download_file` a server-side fetch of any caller-supplied URL (`main.py:1135–1158, 1161–1191, 1234–1288`).
- Flask dev server with `debug=True` ⇒ the Werkzeug interactive debugger opens a code console on any traceback (`main.py:1797`).
- Credentials are static and weak (`user`/`password`); the agent prompt literally hands the model the sudo password (`prompts.py:18`); a real third-party API key is committed in source (`surfer_agent.py:24`); `pyautogui.FAILSAFE` is force-disabled (`python.py:31`), removing the panic abort.
- Cloud providers expose this over a **public IP/IPv6 with firewall `mode=open`** (`fastvm/provider.py:53–60`) and EC2 public-IP association — i.e. an unauthenticated execution endpoint reachable over the network during runs.

**Why it's too primitive for a runtime.** "It's only a benchmark VM" stops being true the moment the runtime serves production agents handling real credentials and data. There is no identity, no per-action scoping, no boundary between *agent* actions and *grader* actions.

**Shinken fix — D6 + D2 + D9.** A three-layer capability model — runtime plumbing that scopes each Sandbox to the resources its task needs: a **Cedar** declarative decision layer (formally verifiable, sub-ms), an object-capability caretaker/membrane handle layer (O(1) revoke), and **OS enforcement** (Linux: bubblewrap + seccomp network-gate + Landlock + cgroups + an **out-of-VM egress proxy**, deny-by-default). Eight capability classes (`net.egress`, `fs.scope`, `clipboard`, `gpu`, `install.privileged/sudo`, `persistence`, `credentials`, `peripheral`) with four scope tiers (Auto/Notify/Ask/Block). The host↔guest channel is **virtio-vsock, not HTTP-on-a-public-port** (D4). Secrets are brokered via Vault/KMS with proxy header-injection so the model never sees plaintext (D6). The agent loop runs **outside** the sandbox and routes tool calls through a controlled `tool_runner` boundary and the **Action Gateway** choke point (auth → rate-limit → budget → Cedar policy → dispatch) (D9). See the [08 Isolation & capability note](threat-model.md) and [permissions](../../notes/permissions.md).

### 3.2 Full-frame PNG polling — bandwidth waste, no streaming

**The problem.** Every observation re-renders and ships a full-resolution lossless PNG over HTTP (`main.py:263–333`), and the **entire** a11y tree is re-serialized as a multi-MB XML string every call (`main.py:902–964`). No deltas, no dirty-rect, no region-of-interest, no codec negotiation, no ETag/caching, plus a disk write→read round-trip per frame. The control loop is pull-only: `_get_obs` is three serial blocking GETs (`desktop_env.py:332–340`); there is no push, no event loop, only fixed retries (`python.py:80–81`). `_get_libreoffice_version()` even shells out `libreoffice --version` on **every** `/accessibility` call.

**Why it's fatal.** At 1920×1080 a PNG is hundreds of KB to MBs *per step*. cua improved on this (JPEG/quality, server-side downscale) but still sends full frames base64-in-JSON. Neither approaches the cost of structured observation: structured ≈ **20 kbps** vs. H.264 office video ≈ 3 Mbps, roughly **150×** cheaper (vendor-published, unverified; see [09-economics-and-build-vs-buy.md](economics-and-build-vs-buy.md)).

**Shinken fix — D3 + D4.** **Screenshot-first, structured-upgrade** observation. Rung 0 is the universal screenshot/focused-region baseline; Rung 1 is a normalized cross-OS a11y/DOM **tree diff** → one `Element{ref, role, name, value, states, bbox, source}` schema with stable per-session refs (~6× token savings vs. raw pixels where coverage is strong, ~25k vs. ~150k tokens/task, vendor-published, unverified). Rung 2 = Set-of-Marks/OmniParser on demand; Rung 3 = region/zoom pixels; Rung 4 = full frame/video. The transport is a single WebRTC PeerConnection: a reliable-ordered **data channel** carrying the structured event stream (this stream *is* the replay log) plus an **on-demand media track** (NVENC H.264/AV1, screen-content-tuned). Host↔guest is virtio-vsock in the optimized tier. See [streaming-bandwidth](../../notes/streaming-bandwidth.md).

### 3.3 Code-as-action — no typed schema, no validation

**The problem.** In the dominant paths the agent's action *is* arbitrary Python source: `execute_python_command` wraps model text in a `PYAUTOGUI_PKGS_PREFIX` and POSTs `['python','-c', code]` to `/execute` (`python.py:29–37, 195–222`). The method's own docstring says it runs "any other python command. who knows?". There is no schema validation, no allowlist, trivial injectability, and quoting is so brittle the codebase carries `_fix_pyautogui_less_than_bug` and `isShiftCharacter` monkeypatches. Action execution is even **non-deterministic** — pyautogui injects random easing + random duration (`python.py:315–318`). Five competing action representations each get a bespoke parser and an ad-hoc translation to pyautogui code; parsing is regex/AST string-munging that returns error *strings* instead of raising.

**Why it's fatal.** Stringly-typed code-as-action cannot be cleanly recorded, diffed, deterministically replayed, capability-gated, or version-negotiated. It conflates "drive the UI" with "run arbitrary code."

**Shinken fix — D2.** One canonical typed tagged-union action schema discriminated by `verb` (~16 verbs); `target = oneof{ point_px | point_norm | element_ref }`; explicit `CoordinateSpace` per observation; semver-versioned with capability negotiation at handshake. **Version-pinned bidirectional adapters** are the only model-facing surface (Anthropic `computer_*`, OpenAI `computer_call`, UI-TARS, OSWorld `computer_13`) — so we ingest every existing agent without forcing a new dialect on them. **Code-as-action** (`exec`/`bash`/`edit`) is *not* abolished; it becomes a separate, **off-by-default capability class** behind the `tool_runner` policy boundary (D2 + D6). This also kills the ~15-way control-loop fork: one stable agent contract behind the **Operator** seam. See [ai-native-interface](../../notes/ai-native-interface.md).

### 3.4 X11/Linux-only in practice

**The problem.** Cross-platform is aspirational. The runners hardcode `os_type='Ubuntu'` (`run.py:167`, `run_multienv.py:186`); `/terminal` returns 500 on non-Linux (`main.py:361–363`); the screenshot/cursor path is X11-only (`pyxcursor.py` requires `$DISPLAY`; the README mandates disabling Wayland; `ffmpeg x11grab`); `X_MAX/Y_MAX` are hardcoded `1920/1080` (`actions.py:1–2`); the eval suite assumes Ubuntu app paths. macOS/Windows branches are partial, stubbed, or carry real bugs.

**Why it matters.** A runtime that harnesses plug into must be genuinely multi-OS. cua already ships Linux/macOS/Windows/Android behind one API — its Apple-Silicon virtualization is a moat. Narrowness loses the comparison instantly.

**Shinken fix — D10 + D3 + D1 + D15.** **One** ACI spans the built operation-layer backends today (`cua`, `mcp-computer`, `browser-runtime`, `e2b`) and the Shinken-owned Guest Runtime, with capability negotiation making each surface's actual verbs and observation tiers explicit. Native-engine rollout is a separate maturity axis: Linux/X11 has the deepest first-party implementation and CI evidence; macOS capture+input v1 has local proof; native Windows/Wayland and macOS AX remain follow-ups. The `Element` schema (D3) normalizes AT-SPI/UIA/AX/CDP into one cross-platform shape with explicit `CoordinateSpace` (no hardcoded resolution). Substrate is routed by `(OS × needs-GPU × needs-fast-fork)` (D1): Apple Virtualization.framework on Apple hardware for a managed macOS tier; Cloud Hypervisor/QEMU + virtio-win for a managed Windows tier. **Known gap (carried honestly):** measured a11y coverage is uneven (Qt strong, GTK weak, terminals/canvas absent), and native UIA/AX conformance is not yet proven — see [open-questions](../../notes/open-questions.md).

### 3.5 Slow snapshot-revert — no fast fork, no warm pool

**The problem.** Reset is heavy and its cost is invisible behind the ABC (§1.3). Any prior step flips `is_environment_used=True`, forcing a full snapshot revert + VM reboot + IP rediscovery + controller rebuild + full setup replay **every episode** (`desktop_env.py:293–302`). On AWS that means terminate + `RunInstances` + wait `instance_running` — minutes per task (`aws/provider.py:142–198`). Docker can't snapshot at all. Parallelism is **process-per-VM** with no warm pool, no CoW fork, and no snapshot GC (created AMIs/disks leak). The synchronization is hardcoded sleeps — `sleep(60)` after reset, `sleep(20)` before evaluate, `sleep(2)` per step — not readiness signals (`lib_run_single.py:26, 64`). On a 369-task run this is enormous dead wall-clock and a guess, not a probe.

**Why it's fatal.** High-throughput parallel rollouts (N agents forking one warm state for pass@k, or massive eval fan-out) are impossible when reset is a minute-scale full relaunch.

**Shinken fix — D1 + D9 + D7.** Reset = **fork-from-snapshot**: MAP_PRIVATE CoW + `userfaultfd` + a warm parent pool, targeting <30 ms VMM restore on the Linux fork tier (Firecracker restore 5–30 ms VMM-only; Morph fork P99 ~1.3 ms with ~93% shared pages — vendor-published, unverified; [github.com/firecracker-microvm/firecracker](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md), [morph.so/blog/infinibranch](https://morph.so/blog/infinibranch/)), with a post-fork uniqueness hook (reseed RNG/MAC/hostname/boot-id). **Instant reset and replay-branching are the same primitive** — fork a snapshot node (D1/D5). The Fleet Manager keeps warm pools per image/region/tier with cold-pool replenish (the OSS `kubernetes-sigs/agent-sandbox` CRD shape) (D9). Synchronization uses **readiness probes, not sleeps**, and eval forks N≥5 CoW replicas per task → pass@k / pass^k with CIs (D7). The Provider ABC's colon-packed string becomes a typed `RuntimeInfo` handle. See [sandbox-infra](../../notes/sandbox-infra.md). **First-party proof on commodity Docker (built):** the current safe disk-tier restore has a rerunnable ~0.6 s measurement. The ~0.12 s live graft and ~0.4 s CRIU rows are historical pre-hardening evidence; the graft is disabled, and the atomic stopped-window CRIU implementation requires privileged live revalidation before those latency numbers are republished. See [benchmarks §1/§1b](../engineering/benchmarks.md).

### 3.6 Brittle evaluators — stringly-typed, hand-coded, untested

**The problem.** Evaluators are imperative Python keyed by `getattr` on stringified names (`desktop_env.py:364–409`), with no schema validation — a typo fails only at grade time. Getters hard-code OS/arch-specific app-internal paths (Chrome's `Preferences` in four branches, repeated across getters; comments admit "not tested on Windows and Mac"). Metrics are fragile (`is_utc_0` slices `timedatectl` line `[3]`; `check_gnome_favorite_apps` `eval()`s guest output). `fuzzy_match` returns a *continuous* score that the harness then treats as binary. Grading is a single end-of-episode snapshot diff whose `postconfig` *mutates* the very state it measures (`pkill chrome`, force `ctrl+s`). Oracles are fetched from external URLs at grade time with no checksum. Aggregation `eval()`s `result.txt` and persists `str(dict)`. This is precisely why OSWorld-Verified needed 300+ grader fixes.

**Why it matters.** If the grader is buggy, the leaderboard is fiction and the trajectory data is mislabeled. Graders must be **tested artifacts**, versioned with the task and the environment.

**Shinken fix — D7.** Invert OSWorld: the eval layer is thin orchestration on the production runtime. A **typed verifier DAG** (not `getattr` on strings); **programmatic-primary with a constrained model-verifier fallback** for genuinely fuzzy goals; a **golden snapshot per task**; **task + grader + env versioned together**; checksum-pinned oracles; **readiness probes, not sleeps**; **N≥5 forked replicas** for statistical treatment of flakiness. Graders ship with conformance tests. Built-in suites — OSWorld-Verified, WindowsAgentArena, AndroidWorld, WebArena/VisualWebArena/WebVoyager — run on the same runtime. See [eval-benchmarks](../../notes/eval-benchmarks.md).

### 3.7 No replay, no capability model (recap)

These two cross-cut everything above; OSWorld has neither as a first-class feature.

**No replay.** The replay hooks are explicit stubs: `setup.py` `_act_setup` / `_replay_setup` raise `NotImplementedError` (`setup.py:460–471`); `getters/replay.py:get_replay` only re-emits three action types and is fixme-flagged. Action execution is non-deterministic (`python.py:315–318`). The only artifacts are write-only PNGs + a JSONL of free-text strings + an opaque post-hoc mp4, served by a poll-and-full-page-refresh Flask `monitor/`. There is no structured, timestamped, re-runnable event log; no action IDs; no pre/post state hashes. **Shinken fix — D5:** the event stream *is* the replay log. The `.skn` bundle (ZIP, Playwright-trace model) holds `manifest.json` + append-only `events.jsonl` (logical-clock `seq` + wall anchor, `action_id` pairing action→observation) + an immutable, branchable checkpoint DAG + content-addressed media. Not bit-deterministic — a pragmatic state-snapshot + event-log + observation-log. **Branch = CoW-fork env snapshot + deserialize agent checkpoint → re-run from step N** (the same fork primitive as reset). `.skn` doubles as RL/SFT training data — a supporting byproduct of the runtime-state wedge, never the headline (D12). See [replay](../../notes/replay.md).

**No capability model.** Covered in §3.1; the fix is D6's three-layer Sandbox Capability Manager — runtime plumbing that scopes each Sandbox to the egress, credentials, host filesystem, GPU, persistence, and OS-automation resources its task needs, with grants/denials as first-class replay events. cua's capability story is also thin (binary container-name + API-key; safety-check hook unimplemented) — so finer-grained resource scoping is an open lane.

---

## 4. Summary — weakness → decision map

| # | OSWorld weakness | Representative `file:line` | Shinken decision |
|---|---|---|---|
| 1 | Unauthenticated Flask `:5000` arbitrary-execution server (no auth/TLS, no identity or scoping, debug console, public IP) | `main.py:92, 1797`; `fastvm/provider.py:53–60`; `surfer_agent.py:24` | **D6** (Cedar+ocap+OS, egress proxy) · **D2** (`tool_runner`) · **D4** (vsock) · **D9** (Action Gateway) |
| 2 | Full-frame PNG polling + whole-XML a11y per step; no streaming | `main.py:263–333, 902–964`; `desktop_env.py:332–340` | **D3** (screenshot-first + structured upgrade) · **D4** (WebRTC dual-channel, NVENC) |
| 3 | Code-as-action; 5 untyped action dialects; non-deterministic exec | `python.py:29–37, 195–222, 315–318` | **D2** (typed schema + adapters; code-as-action off-by-default) |
| 4 | X11/Linux-only in practice; hardcoded `Ubuntu`/`1920×1080` | `run.py:167`; `main.py:361–363`; `actions.py:1–2`; `pyxcursor.py:52–66` | **D10** (one ACI, per-OS handlers) · **D3** (`Element` schema) · **D1** (per-OS substrate) |
| 5 | Slow snapshot-revert; no fork/warm-pool; hardcoded sleeps; process-per-VM | `desktop_env.py:293–302`; `aws/provider.py:142–198`; `lib_run_single.py:26, 64` | **D1** (CoW fork-from-snapshot) · **D9** (warm pools) · **D7** (readiness probes, forked replicas) |
| 6 | Brittle stringly-typed evaluators; untested graders; mutating grade-time setup | `desktop_env.py:364–409, 453–519`; `chrome.py:205–236`; `basic_os.py` | **D7** (typed verifier DAG; task+grader+env versioned; conformance-tested) |
| 7a | No replay (stubs; non-deterministic; write-only logs) | `setup.py:460–471`; `getters/replay.py`; `python.py:315–318` | **D5** (`.skn` event-sourced, branchable) |
| 7b | No resource scoping (recap of #1) | `main.py:92`; `prompts.py:18` | **D6** (capability scoping; HITL) |

---

## 5. The thesis, restated

OSWorld proved the *task model* — a real OS, real apps, executable graders, declarative task JSON. It is a great benchmark and a poor runtime: its transport (blocking HTTP poll), actuation (arbitrary code strings), guest server (an unauthenticated arbitrary-execution endpoint with no identity or scoping), observation model (full PNGs), reset (minute-scale relaunch), and grading (stringly-typed reflection) are all the *primitive* path, not the *streaming production* path its own submodules already gesture toward.

Shinken keeps OSWorld's good data-model instincts and rebuilds the runtime around six commitments that fall directly out of this teardown:

1. **Screenshot-first, structured-upgrade** observation (D3) instead of full-frame PNG polling as the whole product.
2. **A single typed, versioned action schema** with adapters (D2) instead of five code-as-action dialects.
3. **Dual-channel WebRTC streaming over vsock** (D4) instead of blocking HTTP on a public port.
4. **Per-Sandbox capability scoping + an Action Gateway** (D6, D9) instead of an unauthenticated, unscoped execution server.
5. **CoW fork-from-snapshot** (D1) — the same primitive for instant reset *and* replay-branching — instead of minute-scale relaunch.
6. **Event-sourced `.skn` replay** (D5) and a **typed, conformance-tested verifier DAG** (D7) instead of write-only logs and stringly-typed graders.

The competitive read from trycua/cua sharpens the bar: cua already fixed #2 (typed actions),
structured a11y, and basic trajectory replay, and matched cross-platform breadth — so those are
table stakes. Shinken's defensible wins are **real-time streaming**, **bandwidth via structured
paths where coverage is strong**, **high concurrency via fork**, and
**training/eval-grade replay artifacts** — exactly the axes where both OSWorld and cua are weakest
(finer-grained per-Sandbox resource scoping rounds out the runtime as plumbing, not a headline win).
See [02-architecture.md](architecture.md) and [04-landscape.md](landscape.md) for how those
wins compose into a single platform serving production agents and evaluation on one runtime.
