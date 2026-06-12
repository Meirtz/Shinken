# OSWorld Teardown — File-by-File Evidence Base

> Raw, technical, file-by-file notes on the OSWorld codebase, organized by subsystem. This is the
> evidence base behind [`docs/design/osworld-analysis.md`](../docs/design/osworld-analysis.md): where that
> doc states a conclusion, the supporting `file:line` reference and the corresponding Shinken
> decision (D1–D15, see [`docs/design/tech-decisions.md`](../docs/design/tech-decisions.md)) live here. The
> read covers six subsystems — in-VM server, client env + controllers, providers, evaluators/tasks,
> agents/obs-action, and end-to-end dataflow. All source paths are under `references/OSWorld/`.
> External anchors: the OSWorld repo [github.com/xlang-ai/OSWorld](https://github.com/xlang-ai/OSWorld),
> the paper [arXiv:2404.07972](https://arxiv.org/abs/2404.07972), and the Verified variant
> [xlang.ai/blog/osworld-verified](https://xlang.ai/blog/osworld-verified). Date of assessment:
> 2026-05-30. Every speed/cost figure attributed to a third party is marked **(vendor-published,
> unverified)**.

OSWorld is a Gym-style benchmark harness that drives a real desktop OS inside a VM. One worker
process owns one `PromptAgent` + one `DesktopEnv`; `DesktopEnv` abstracts a pluggable virtualization
`Provider` (vmware / virtualbox / docker / aws / azure / gcp / aliyun / volcengine / fastvm) that
boots a VM image preloaded with a single-file Flask "OSWorld server" (`desktop_env/server/main.py`)
on port 5000. **Every** host↔guest interaction — reset/setup, observe (screenshot / a11y-tree /
terminal), act (pyautogui code snippets), evaluate (run shell, fetch files) — is synchronous HTTP
request/response polling against that one Flask process, plus side-channel ports for VNC (8006/5910),
Chrome CDP (9222) and VLC (8080). The design is functionally complete for benchmark
replay-by-re-execution but is primitive on exactly the four axes Shinken targets: **no real-time
streaming, no bandwidth optimization, no permission management, no deterministic replay.** Below,
each subsystem is dissected: how it works, its API/schema, the weak parts, the genuinely reusable
patterns, and where Shinken's decisions invert it.

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ run_multienv.py  (process-per-VM workers, shared task Queue, dead-worker restart)       │
│   └─ per worker: PromptAgent  +  DesktopEnv ───────────────┐                            │
│                                                            │                            │
│  HOST (lib_run_single.run_single_example, blocking loop):  │  Provider ABC (5 methods)  │
│   reset → sleep(60) → obs → predict → step → log → sleep(20)→ evaluate                   │
│        │            │      │       │                       │  start/get_ip/save/revert/  │
│        │  GET /screenshot  POST /execute  GET /accessibility│  stop  (vmware|aws|docker|  │
│        │  GET /terminal    (python -c ...)                  │        fastvm|...)          │
│        ▼            ▼      ▼       ▼                         ▼                            │
│  ═══════════ plaintext HTTP, no auth, :5000 ════════════════  VM boot / snapshot revert  │
│        ▼                                                                                 │
│  GUEST: desktop_env/server/main.py  (Flask dev server, debug=True, 0.0.0.0:5000)         │
│   ~30 routes: /execute /run_python /run_bash_script (arbitrary exec), /screenshot (PNG), │
│   /accessibility (full XML), /setup/* , /start_recording (ffmpeg x11grab)                │
│  X11/Xorg only · DISPLAY=:0 · user `user` / pw `password` · single global recording      │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. In-VM execution server — `desktop_env/server/`

### How it works

A single ~1,800-line Flask file (`server/main.py`) runs inside the guest as a systemd unit
(`osworld_server.service:9`) — `/usr/bin/python /home/user/server/main.py` as user `user`, with a
hardcoded `DISPLAY=:0`, `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`, and
`XDG_RUNTIME_DIR=/run/user/1000`. The entrypoint is `app.run(debug=True, host='0.0.0.0')`
(`main.py:1796–1797`): the Flask **development** server, with the Werkzeug interactive debugger and
the auto-reloader enabled, bound to all interfaces, no `port=` argument so it defaults to 5000,
single-threaded by default.

Platform dispatch is read once into `platform_name = platform.system()` (`main.py:23`) and the
conditional imports of `pyatspi` (Linux), `pywinauto`/`win32` (Windows), or
`Quartz`/`AppKit`/`oa_atomacos` (Darwin) happen at module load (`main.py:25–58`). Almost every
handler then *re-branches* on `platform.system()` at request time — there is no platform handler
object, just `if/elif` ladders scattered across ~30 routes.

The route surface, grouped by purpose:

| Group | Routes | Behavior |
|---|---|---|
| Observe | `GET /screenshot`, `GET /accessibility`, `GET /terminal`, `GET /platform`, `GET /cursor_position` | full PNG, full XML tree, scraped terminal text |
| Exec (arbitrary) | `POST /execute` ≡ `/setup/execute`, `/run_python`, `/run_bash_script`, `/setup/launch` | arbitrary subprocess / python / bash |
| Exec+wait | `POST /execute_with_verification` (+`/setup/`) | run command then poll for a window title or `returncode==0` |
| Files | `POST /file`, `/list_directory`, `/wallpaper`, `/setup/upload`, `/setup/download_file` | read / write / fetch-URL into guest |
| Window/info | `POST /screen_size`, `/window_size`, `/desktop_path`, `/setup/open_file`, `/setup/activate_window`, `/setup/close_window`, `/setup/change_wallpaper` | mostly `wmctrl`/Xlib, Linux-centric |
| Record | `POST /start_recording`, `/end_recording` | one global ffmpeg x11grab → `/tmp/recording.mp4` |

Notable handler internals:

- **Command exec** (`main.py:76–117`): JSON `{command, shell}`. If `shell` is false and the command
  is a string it is `shlex.split`; `~/` args are `expanduser`-expanded; then `subprocess.run` with
  `timeout=120`, the `shell` flag passed straight through. The in-source comment is literally
  *"Execute the command without any safety checks"* (`main.py:92`). There is **no dedicated
  keyboard/mouse route** — input actions are pyautogui (or arbitrary) Python source strings shipped
  to `/execute` / `/run_python` and `exec`'d in the guest. `pyautogui` is imported and tuned
  (`PAUSE=0`, `DARWIN_CATCH_UP_TIME=0`, `main.py:66–67`) and `/cursor_position` returns
  `pyautogui.position()` (`main.py:1199–1202`), but `xdotool` is unused and `pyxcursor` is only for
  cursor-image capture, not input.
- **Screenshot** (`main.py:263–333`): writes `server/screenshots/screenshot.png` then `send_file`
  with mimetype `image/png`. The Linux path is `pyautogui.screenshot()` (scrot/gnome-screenshot
  under the hood) **plus** manual cursor compositing via `pyxcursor.Xcursor`
  (`XFixesGetCursorImage`) pasted at `pyautogui.position()` (`main.py:319–326`). One full PNG per
  request; no cache, no ETag, no delta, disk write+read round-trip every frame.
- **Accessibility tree** (`main.py:902–964`): Linux walks `pyatspi.Registry.getDesktop(0)`, fans out
  per app with a `ThreadPoolExecutor` calling `_create_atspi_node` (`main.py:423–576`), which
  serializes states/attributes/component bbox/text/value/action into an lxml element tree with
  per-OS namespace maps (`main.py:370–403`). Windows uses `_create_pywinauto_node` (`main.py:580–739`);
  macOS uses `_create_axui_node` (`main.py:744–899`). The whole tree is returned as
  `jsonify({'AT': <entire XML as one unicode string>})`. Limits are `MAX_DEPTH=50`, `MAX_WIDTH=1024`,
  `MAX_CALLS=5000` (`main.py:411–413`; `MAX_CALLS` appears unused). LibreOffice Calc tables get a
  special row/column traversal up to 16384 columns (`main.py:538–565`), and
  `_get_libreoffice_version()` shells out `libreoffice --version` on **every** `/accessibility` call
  (`main.py:909`, `416–420`).
- **Recording** (`main.py:1500–1577`): `ffmpeg -y -f x11grab -draw_mouse 1 -s WxH -i :0.0 -c:v
  libx264 -r 30 /tmp/recording.mp4`, waits 2 s to detect immediate failure; `/end_recording` sends
  SIGINT, `communicate(timeout=15)` else kill, then `send_file`s the mp4. A single module-global
  `recording_process` (acknowledged fixme, `main.py:72`) means one recording at a time — and
  implicitly one client.
- **Files** (`main.py:1042–1288`): `/file` and `/wallpaper` `send_file` an
  `expandvars`/`expanduser` path (arbitrary read); `/setup/upload` `makedirs` + `file.save`
  (arbitrary write); `/setup/download_file` makes the guest `requests.get` any URL with 3 retries
  and content-length verification (a fetch-arbitrary-URL endpoint).

`pyxcursor.py` is an X11-only ctypes binding to `libXfixes`/`libX11`; it raises if `$DISPLAY` is
unset (`pyxcursor.py:52–66`) and reshapes the cursor buffer assuming 8 bytes/pixel then slices 4
(`pyxcursor.py:148–158`) — brittle and dependent on 64-bit `unsigned long` layout.

### API / schema

Plaintext HTTP, mixed body conventions: JSON (`/execute`, `/run_python`), form-encoded (`/file`,
`/window_size`, `/setup/upload`), and multipart (file upload). Mixed return types: plain strings,
`jsonify`, `send_file`, `abort`. Duplicate aliases (`/execute` ≡ `/setup/execute`). No versioning,
no auth header, no content negotiation. `/cursor_position` returns a bare JSON array
`jsonify(x, y)` (`main.py:1202`).

### Primitive / weak parts

- **Arbitrary code execution, no auth, no TLS.** Every route is open on `0.0.0.0:5000`. `/execute`,
  `/run_python`, `/run_bash_script`, `/setup/launch` are no-auth arbitrary code
  execution; `/file`/`/wallpaper` are arbitrary read; `/setup/upload` is arbitrary write;
  `/setup/download_file` is a fetch-arbitrary-URL endpoint. The Flask dev server with `debug=True` turns any traceback into
  an interactive code console.
- **Single-threaded.** A slow a11y dump or a 30 s python exec blocks all other requests; concurrent
  screenshots during recording contend.
- **Maximally bandwidth-wasteful observations.** Full-resolution lossless PNG written to a temp file
  and re-sent on every `GET /screenshot` (hundreds of KB to MB at 1920×1080) with no JPEG/WebP, no
  quality/scale param, no ETag/`If-None-Match`, no delta, no region-of-interest, no streaming. The
  a11y tree is one enormous uncompressed XML string in a JSON field, re-serialized from scratch every
  call, with no diffing, no incremental subtree fetch, no caching.
- **No streaming at all.** Frames are pull-only via polling; recording is an opaque post-hoc mp4 you
  can only retrieve after `/end_recording`.
- **Shallow, buggy cross-platform.** `/screen_size` references an unbound `screen_width` on macOS
  (no Darwin branch, `main.py:967–985`); Windows a11y references namespace keys `cnt`/`cols`/`id` not
  defined in `_accessibility_ns_map['windows']` (`main.py:615/622/629` vs `381–391`), the `KeyError`
  swallowed by a bare except; `/run_python`'s error path calls an unimported `traceback`
  (`main.py:1669`). `/terminal`, `/window_size`, window management are Linux-only.
- **Broad swallowed exceptions** everywhere; `/run_bash_script` forcibly blanks the error field
  ("Always empty as requested", `main.py:1746`), discarding stderr.
- **Hardcoded assumptions:** `DISPLAY=:0`, uid-1000 DBUS path, user `user`/pw `password`,
  1920×1080, `/tmp/recording.mp4`, `/usr/bin/python3`, X11/Xorg only (README requires disabling
  Wayland — `pyxcursor`, ffmpeg `x11grab`, Xlib screen-size all break under Wayland).
- **Inconsistent timeouts vs comments:** `/execute` 120 s, `/run_python` 30 s, `/run_bash_script`
  default 100 s (comment says 30), `/setup/open_file` polls up to `TIMEOUT=1800` s — blocking a
  worker for 30 minutes (`main.py:1335`).

### Reusable patterns

- A **single uniform in-VM control plane over one well-known port** abstracting OS automation behind
  named endpoints is a good baseline shape — the host talks to one daemon regardless of guest OS.
  Shinken keeps the concept and replaces the daemon with the Guest Runtime (`shinkend`) over a
  bidirectional streaming transport (D4, D8).
- **Per-OS a11y serialization into one namespaced tree** (`st`/`attr`/`cp`/`val`/`act`/`role`,
  `main.py:370–403`) addressable by XPath/CSS is genuinely useful: it normalizes AT-SPI / UIA / AX
  into one query language. This is the seed of D3's single `Element{ref,role,name,value,states,bbox,
  source}` schema.
- **Screenshot-with-cursor compositing** (`main.py:319–326`): OS screen grabs often omit the cursor;
  capturing the cursor image + hotspot and pasting at the pointer is worth keeping for faithful
  replay/grounding.
- **`execute_with_verification`** (`main.py:120–224`): couple an action with a success predicate so
  the caller gets a *settled* state rather than racing — the readiness-probe instinct behind D7's
  "readiness probes, not sleeps."
- **`download_file` with retries + content-length verification** and **upload with post-write size
  verification** (`main.py:1176–1179`): verifying transferred bytes is a good habit.
- **`ThreadPoolExecutor` fan-out** over top-level windows to parallelize tree building
  (`main.py:913–959`) is the right latency instinct.
- **Graceful recording shutdown** (SIGINT-then-kill so ffmpeg finalizes the container,
  `main.py:1554–1563`).
- **Application-name command rewrite** (`google-chrome→chromium` on arm, `main.py:254–256`) shows
  exactly where an abstraction layer belongs.

### Maps to

D2 (typed action schema replaces code-as-action), D3 (structured a11y with refs replaces XML
blob), D4 (streaming dual-channel replaces PNG polling), D6 (capability gate replaces open arbitrary exec), D10
(real per-OS handler factory replaces `if/elif` ladders).

---

## 2. Client `DesktopEnv` (gym `Env`) + controllers — `desktop_env/`

### How it works

`DesktopEnv` is a Gymnasium-style `Env` (`reset` / `step` / `render` / `evaluate` / `close`) wrapping
a provider and an HTTP client to the in-VM server.

- **Construction** (`desktop_env.py:89–190`): picks the provider via
  `create_vm_manager_and_provider`, sets default ports (server 5000 / chromium 9222 / vnc 8006 /
  vlc 8080), decides `client_password` by provider, and sets `is_environment_used` heuristically —
  vmware/virtualbox start "dirty" (`True`); cloud/docker start "clean" (`False`) to skip a needless
  first revert (`desktop_env.py:155–160`). Then it immediately calls `_start_emulator()`.
- **Boot** (`desktop_env.py:193–214`): `provider.start_emulator(path, headless, os_type)` powers the
  VM; `provider.get_ip_address(path)` returns either `<host>` or a packed
  `<host>:server:chromium:vnc:vlc` string parsed with `rsplit(':', 4)` (`desktop_env.py:202–213`).
  Then it constructs `PythonController(vm_ip, server_port)` and `SetupController(...)` — both just
  store `http://<ip>:<port>` base URLs (`python.py:74–78`, `setup.py:46–47`).
- **Reset** (`desktop_env.py:268–330`): a `MAX_RETRIES=5` loop: if `is_environment_used`, call
  `_revert_to_snapshot()` then `_start_emulator()` again (full VM reboot + new IP discovery + new
  controllers), then optional proxy setup, `_set_task_info` (parses id/instruction/config/evaluator),
  `reset_cache_dir`, and `setup_controller.setup(config)`. On failure: `sleep(5)` and retry. Finally
  returns `_get_obs()`. `_revert_to_snapshot` (`desktop_env.py:248–258`) rewires the manager registry
  (delete/add/occupy) and updates `self.path_to_vm` when the provider returns a **new** handle (the
  cloud "replace, don't mutate" path).
- **Setup** (`setup.py:57–106`): first blocks on `GET /terminal` up to `MAX_RETRIES=20 × 5 s` to
  confirm liveness, then iterates the config list, builds method name `_{type}_setup`, asserts it
  exists, and `getattr`-dispatches with `**parameters`. Any exception aborts the whole setup.
  Primitives hit `/setup/*`: `_download_setup`, `_upload_file_setup` (exp-backoff retries),
  `_change_wallpaper`, `_open_setup` (blocking up to ~1810 s), `_launch_setup`, `_execute_setup`
  (an `until` polling loop on returncode/stdout/stderr, `sleep(0.3)`, give up after 5 failures),
  `_command`/`_sleep`, `_activate_window`/`_close_window`. Chrome tab/login/history use Playwright
  `connect_over_cdp` to `http://<ip>:9222` **directly from the host**, bypassing the in-VM server
  entirely (`setup.py:579–811`). `_execute_setup` templates `{CLIENT_PASSWORD}`/`{SCREEN_WIDTH}`/
  `{SCREEN_HEIGHT}` into commands.
- **Step** (`desktop_env.py:411–451`): increments the counter, appends to `action_history`, marks
  env used. Sentinels `WAIT`/`FAIL`/`DONE` are handled locally. For `computer_13` it dispatches to
  `controller.execute_action()` (maps a structured `{action_type, parameters}` to a pyautogui call
  string). For `pyautogui`/`claude_computer_use` the action is a raw Python string sent verbatim via
  `execute_python_command`. It always `sleep(pause)` (default 2 s) afterward, then returns a fresh
  `_get_obs()`.
- **`execute_python_command`** (`python.py:195–222`): wraps the command in `PYAUTOGUI_PKGS_PREFIX`
  (`import pyautogui; import time; pyautogui.FAILSAFE=False; <monkeypatched isShiftCharacter>;
  {command}`), builds `['python','-c', code]`, JSON-POSTs `{command, shell:False}` to `/execute`
  (120 s timeout, 3 retries × 5 s). `run_python_script` POSTs raw code to `/run_python`.
- **`_get_obs`** (`desktop_env.py:332–340`): three independent blocking GETs — `/screenshot`
  (validated via PNG/JPEG magic bytes, `python.py:101–127`), `/accessibility` → `json['AT']`,
  `/terminal` → `json['output']` — gated by `require_a11y_tree` / `require_terminal`.

### API / schema

`DesktopEnv.reset(task_config) → obs`; `step(action, pause=2) → (obs, reward=0, done, info)`;
`evaluate() → float`. `obs = {screenshot: bytes, accessibility_tree: str|None, terminal: str|None,
instruction: str}`. Action = `str` pyautogui code | `dict {action_type, parameters}` | sentinel
`WAIT`/`FAIL`/`DONE`. The `computer_13` schema (`actions.py:ACTION_SPACE`) has a `KEYBOARD_KEYS`
allowlist and hardcoded `X_MAX`/`Y_MAX = 1920/1080` (`actions.py:1–2`).

### Primitive / weak parts

- **Synchronous request/response everywhere — no streaming.** Each step is `POST /execute` (blocks,
  up to 120 s) + fixed `sleep(pause=2)` + `GET /screenshot` + `GET /accessibility` + `GET /terminal`,
  all serial round-trips. Per-step wall time is dominated by the hardcoded 2 s sleep plus 3–4
  sequential HTTP RTTs; nothing is pipelined or pushed.
- **Stringly-typed code injection.** pyautogui actions are concatenated into `python -c "...{command}"`
  (`python.py:29–37, 201`); in the pyautogui/claude action spaces the *action IS arbitrary Python
  source* run in the guest — no schema validation, no sandbox, brittle to quoting (hence the
  `_fix_pyautogui_less_than_bug` and `isShiftCharacter` monkeypatches).
- **Polling, not events.** Liveness is a busy `GET /terminal` loop (`setup.py:73–83`, up to 100 s);
  `_execute_setup` polls with `sleep(0.3)` and a magic "give up after 5 failures"
  (`setup.py:393–401`). No webhooks/long-poll/SSE.
- **Fragile IP/port discovery.** `rsplit(':',4)` only works for exactly 0 or 4 colon-appended ports;
  the older `desktop_env_pointer.py` / `desktop_env_os_symphony.py` use plain `split(':')`
  (`pointer.py:230`) which corrupts IPv6 hosts. Ports are also hardcoded defaults; the chromium port
  is called out as "fixme: hard-coded" (`setup.py:581, 632`).
- **Inconsistent, magic-numbered retries/timeouts.** `retry_times=3`/`interval=5`; setup
  `MAX_RETRIES=20`; `_execute_setup` `nb_failings>=5`; download `max_retries=3`; open_file 1810 s;
  execute 120 s; run_python 90 s. A `ReadTimeout` on `/execute` is silently swallowed and treated as
  terminal (`python.py:214–215`).
- **Reset is extremely heavy.** Any prior step/setup flips `is_environment_used=True`, forcing a full
  snapshot revert + VM reboot + IP rediscovery + controller rebuild + full setup replay every episode.
  On AWS this terminates and relaunches an EC2 instance from an AMI (minutes). Docker cannot snapshot
  at all (revert just kills the container).
- **Three divergent copies** of the same ~400–500-line `Env` (`desktop_env.py` / `_pointer` /
  `_os_symphony`) with subtly different fixes — high duplication, easy to drift.
- **No env-layer trajectory record.** `start_recording`/`end_recording` capture only a server-side
  video; the action stream is an in-memory `action_history` list with no serialization, no
  timestamps, no determinism. `_replay_setup`/`_act_setup` are `NotImplementedError` stubs
  (`setup.py:460–471`).
- **`reward` is hardcoded 0**, `done` only for FAIL/DONE sentinels (`desktop_env.py:418–419`) — the
  Gym contract is cosmetic; scoring is a separate `evaluate()`.
- **Weak secrets handling.** `client_password` defaults to literal `password` /
  `osworld-public-evaluation` (`desktop_env.py:129–135`), interpolated into shell via
  `echo '<pw>' | sudo -S ...` in proxy setup (`setup.py:544–554`); proxy creds written cleartext to
  `/tmp/tinyproxy.conf`.

### Reusable patterns

- **Gymnasium-conformant `Env` surface** (`reset`/`step`/`render`/`close` + dict observation) makes
  the desktop a drop-in for RL/agent loops — keep as the outer contract even when the transport is
  replaced.
- **Declarative dispatch-by-convention setup**: config is a list of `{type, parameters}` dicts mapped
  to `_<type>_setup` (`setup.py:90–98`) — a clean, JSON-serializable provisioning DSL, the basis for
  a versioned, validated task schema (D7).
- **Provider abstraction** cleanly separates "how to get a reachable VM" from env logic, enabling
  local and cloud backends behind one interface.
- **`is_environment_used` dirty-tracking** (`desktop_env.py:152–160, 293–302`): skip reverts when the
  VM is provably clean — generalize into proper CoW reset / per-resource dirty tracking (D1).
- **Dual action representations**: structured `computer_13` (the AI-native-friendly part) plus a
  raw-code escape hatch. Standardize and expand the structured schema (D2).
- **`until`-condition polling on execute** and **`execute_with_verification`** are reasonable
  building blocks for deterministic setup gating — promote to a real wait/assert primitive (D7).
- **Magic-byte screenshot validation** rather than trusting `Content-Type` (`python.py:83–99`).
- **Host-side Chrome control via CDP** (`setup.py:579–811`) shows the value of app-native protocols
  for precise state setup over pixel automation.

### Maps to

D2, D3, D4, D7 (deterministic setup + verifier DAG), D9 (control-plane/data-plane split replaces the
one-VM-one-synchronous-client coupling).

---

## 3. VM/sandbox providers — `desktop_env/providers/`

### How it works

Every backend hides behind a 5-method `Provider` ABC plus a `VMManager` ABC. `path_to_vm` is an
opaque, provider-specific handle (a local `.vmx`/`.qcow2` path, an EC2 instance id, a
`RESOURCE_GROUP/VM_NAME` string, or a microVM UUID).

```
Provider ABC (base.py:11–44)                VMManager ABC (base.py:47–97)
  start_emulator(path, headless, os_type)     initialize_registry / add_vm / delete_vm
  get_ip_address(path) -> "host[:s:c:v:l]"    occupy_vm / list_free_vms / check_and_clean
  save_state(path, name) -> snap_id|None      get_vm_path(os_type, region, screen_size, ...)
  revert_to_snapshot(path, name) -> path|None
  stop_emulator(path)
```

A factory `create_vm_manager_and_provider(provider_name, region, use_proxy)` lazily imports and
returns the `(VMManager, Provider)` pair (`__init__.py:4–47`). The boot sequence is
`manager.get_vm_path → start_emulator → get_ip_address → parse colon-string → build controllers`.

The decisive per-task primitive is `revert_to_snapshot`, and the implementations diverge wildly:

| Provider | `revert_to_snapshot` | Cost | Snapshot model |
|---|---|---|---|
| VMware | `vmrun revertToSnapshot` + 3 s sleep (`vmware/provider.py:93–98`) | seconds | true in-place |
| VirtualBox | savestate then snapshot restore (`virtualbox/provider.py:109–116`) | seconds | true in-place |
| AWS | **terminate** old EC2 + **RunInstances** fresh from AMI (`aws/provider.py:102–255`) | minutes | full AMI per `save_state` |
| Azure | deallocate + delete disk + create disk from snapshot + swap + recreate (`azure/provider.py:110–182`) | minutes | per-disk snapshot dance |
| Aliyun / Volcengine | same describe/start/stop cloud pattern | minutes | instance/disk |
| Docker | **just `stop_emulator`** — `save_state` raises `NotImplementedError` (`docker/provider.py:150–154`) | container restart | **none** |
| FastVM | delete + relaunch microVM from snapshot id (`fastvm/provider.py:187–208`) | **sub-second** | org-scoped microVM snapshot |
| GCP | **0-byte empty stub** wired into the factory | — | crashes on import |

Detail worth keeping in mind: VMware "generates a new VM" by **re-downloading and unzipping a
multi-GB qcow2 from HuggingFace** and rewriting the MAC (`vmware/manager.py:119–193`) — there is no
linked clone. Docker runs a **nested QEMU** with KVM passthrough when `/dev/kvm` exists, else `KVM=N`
(much slower), with dynamic port allocation under a `FileLock` (`docker/provider.py:39–127`).
AWS hardcodes a 30 GB gp3 4000-IOPS volume and a t3.large/t3.xlarge CPU shape (`aws/provider.py:180–191`,
`aws/manager.py:13`).

FastVM is the only one that approximates a fast path: its `FASTVM_GUIDELINE.md:87–99` reports
launch ~0.5 s / revert ~0.7 s vs AWS 60–90 s boot **(vendor-published, unverified)**, after a
one-time image "bake" (~1.5–4.5 min) producing an org-scoped snapshot id. Even so it does
delete+relaunch, not fork; it ships Debian/XFCE (not the canonical Ubuntu/GNOME), risking task drift;
and it requires public IPv6 reachability to the no-auth guest server.

### API / schema

```
start_emulator(path_to_vm: str, headless: bool, os_type: str=None) -> None
get_ip_address(path_to_vm: str) -> str   # "<host>" OR "<host>:<server>:<chromium>:<vnc>:<vlc>"
save_state(path_to_vm: str, snapshot_name: str) -> Optional[str]   # Docker raises NotImplementedError
revert_to_snapshot(path_to_vm: str, snapshot_name: str) -> Optional[str]   # may return a NEW handle
stop_emulator(path_to_vm: str, region=None) -> None
VMManager.get_vm_path(os_type, region, screen_size=(1920,1080), **kwargs) -> str
```

Canonical in-VM ports (`fastvm/config.py`): server 5000, chromium 9222, vnc 8006, vlc 8080,
novnc 5910.

### Primitive / weak parts

- **Reset semantics are leaky and inconsistent behind one ABC.** `revert_to_snapshot` means
  "in-place restore" (VMware/VBox, seconds), "terminate + relaunch" (AWS, minutes), "disk swap"
  (Azure), "delete + relaunch microVM" (FastVM), or "just stop the container" (Docker, no snapshot).
  The caller cannot reason about cost from the interface.
- **AWS revert is brutally expensive:** `create_image` (a full AMI) on `save_state`, terminate +
  `RunInstances` on revert, no incremental/delta snapshot, no warm pool. Cold start is gated on the
  EC2 `instance_running` waiter (15 s × up to 10) **plus** guest boot **plus** the :5000 readiness
  probe.
- **No fast fork / CoW sharing anywhere.** Only FastVM's snapshot restore approximates CoW, and it
  delete+relaunches rather than forks. There is no linked clone, no warm parent pool, no
  fork-per-task.
- **No GPU support in any provider** — CPU-only Nitro/c4m8 shapes; no `--gpus`, no VFIO/vGPU plumbing
  in the ABC or any implementation.
- **Racy Docker port allocation** scans `psutil.net_connections()` + docker port tables and linearly
  probes up to 65354 under a coarse 10 s `FileLock` — TOCTOU between selection and bind.
- **Polling readiness everywhere** (`GET /screenshot` loops with fixed sleeps,
  `docker/provider.py:65–85`, `fastvm/provider.py:82–105`), default 300 s timeouts, no
  exponential backoff in most paths.
- **`get_ip_address` overloads its return** to carry ports as a colon-delimited string parsed by a
  fragile `rsplit(':',4)` — a string-typing hack, not a typed contract (FastVM had to bracket IPv6
  and add an 80-line docstring to keep it from breaking, `fastvm/provider.py:1–21`).
- **GCP is a dead 0-byte stub** yet is dispatchable from the factory and listed in the clean-start set.
- **Cloud reaches the guest over a public IP** with firewall `mode=open` (FastVM) or a public-IP EC2
  — the in-VM server has no auth and accepts arbitrary `/run_python`/`/execute`: an open
  arbitrary-exec endpoint exposed to the internet during runs.
- **No snapshot GC.** AWS AMIs, Azure disk snapshots, FastVM snapshots are created but never deleted —
  a cost/quota leak.
- **Manager pooling is essentially absent for cloud** (AWS/FastVM managers are no-ops except
  `get_vm_path`): every episode boots a fresh VM serially.

### Reusable patterns

- **The minimal 5-method `Provider` ABC + separate `VMManager` (pool) ABC** is a clean
  lifecycle-vs-allocation split worth keeping — but type the return values (a `VMHandle` dataclass
  with host/ports/transport instead of a colon string).
- **`revert_to_snapshot` returning a NEW handle** + consumer registry rewire (`desktop_env.py:248–258`)
  cleanly supports immutable-infra "replace, don't mutate" — useful for fork-per-task designs (D1).
- **FastVM's allocate-once-then-restore-from-snapshot + one-time bake** is the right direction: pay
  boot/install cost once, then sub-second restores. The bake script + measured-timings table is a
  good ops artifact.
- **Signal-handler cleanup of partially-provisioned VMs** on SIGINT/SIGTERM
  (`aws/manager.py:67–95`, `fastvm/manager.py:46–101`) prevents orphaned cloud resources.
- **Centralized lru-cached SDK-client singleton** with a pointed `ImportError` (`fastvm/client.py`) is
  cleaner than scattered `import boto3`.
- **Canonical provider-independent in-VM port constants** (`config.py`) — a good abstraction boundary
  between control plane and guest service.
- **KVM-availability autodetection with graceful fallback** (`docker/provider.py:100–107`) — extend
  to GPU detection.
- **Cloud-side TTL via EventBridge Scheduler** (`aws/provider.py:201–233`) so leaked instances
  self-terminate — a good defensive default.

### Maps to

D1 (tiered substrate, fork-from-snapshot, warm pools, post-fork uniqueness reseed), D9 (Fleet Manager
warm pools + fork-on-demand replacing serial allocate-per-episode), D11 (GPU tier the ABC cannot
express today).

---

## 4. Evaluation + task system — `desktop_env/evaluators/`, task JSON, `lib_run_single`, `show_result`

### How it works

Each benchmark task is a self-contained JSON config at
`evaluation_examples/examples/{domain}/{id}.json`: `id`, `snapshot`, `instruction`, `source`,
`config` (setup steps), `related_apps`, `evaluator`, `proxy`, `possibility_of_env_change`.

The **evaluator block** is a declarative spec: `func` (one metric name or a list), `conj`
(`and`/`or`, default `and`), `result` (getter config(s) that fetch agent post-state), optional
`expected` (oracle getter config(s)), `options` (per-metric kwargs), and `postconfig` (setup steps
run immediately before grading, e.g. pkill+relaunch chrome, `ctrl+s` to force-save, sleep).

`DesktopEnv._set_evaluator_info` (`desktop_env.py:364–409`) resolves the string names into callables
via `getattr(metrics, func)` and `getattr(getters, 'get_'+type)`. Parallel lists (func / result /
expected / options) must be equal length — even unused slots must be padded with `None` (asserted at
`desktop_env.py:407–409`).

`DesktopEnv.evaluate()` (`desktop_env.py:453–519`):

1. re-run `postconfig` setup;
2. special-case infeasibility — `func=='infeasible'` returns 1 iff the last action was `FAIL`;
   conversely a trailing `FAIL` on a normal task short-circuits to 0;
3. call `result_getter(self, config)` to fetch state from the VM, optionally `expected_getter(...)`
   for the gold value;
4. `metric(result_state[, expected_state], **options) → float`;
5. combine: AND returns mean (short-circuit 0 on any zero); OR returns max (short-circuit 1 on any one).

**Getters** fetch post-condition state live from the running VM: `get_vm_file` pulls a file off the
guest over HTTP (`getters/file.py:74`); `get_cloud_file` downloads the gold artifact from a URL
(`file.py:31`); `get_vm_command_line` / `get_vm_terminal_output` run shell; `get_rule` echoes the
inline `rules` dict from the JSON as "expected" (`misc.py:87`); Chrome getters hard-code per-OS
`Preferences`/`Cookies`/`History` paths and parse SQLite or `Preferences` JSON, or drive a live
Playwright/CDP session on port 9222 (`chrome.py`, ~134 KB).

**Metrics** score the fetched state: `exact_match` (`general.py:41`), `fuzzy_match` (rapidfuzz
ratio/100, a *continuous* score, `general.py:95`), `check_include_exclude`, `compare_table`,
`diff_text_file` (difflib `SequenceMatcher` ratio), `run_sqlite3`. Both `metrics/__init__.py` and
`getters/__init__.py` are flat namespaces re-exporting everything by name.

**Episode driver** `lib_run_single.run_single_example` (`lib_run_single.py:14`) resets env+agent,
`sleep(60)`, gets initial obs, loops `predict → for each action env.step → write step PNG + traj.jsonl
line`, then after `max_steps` (and another `sleep(20)`) calls `env.evaluate()` once, writes the float
to `result.txt`, and calls `log_task_completion`. `lib_results_logger.append_task_result`
(`lib_results_logger.py:26`) `fcntl.flock`s `summary/results.json` and **rewrites the whole array** on
each append. `show_result.py` walks `results/{action_space}/{observation_type}/{model}/{domain}/{id}/
result.txt`, `float()`s (or `eval()`s) each, computes per-domain and hardcoded Office/Daily/Professional
category rates, and dumps `all_result.json` via `str(dict)`.

### API / schema

```jsonc
{
  "id": "<uuid>", "snapshot": "<vm-snapshot-name>", "instruction": "<NL goal>",
  "config": [ { "type": "download"|"launch"|"open"|"execute"|..., "parameters": { ... } } ],
  "related_apps": [...],
  "evaluator": {
    "func": "exact_match" | ["m1","m2"],
    "conj": "and" | "or",
    "result":   { "type": "vm_file"|"rule"|"vm_command_line"|... , ... } | [ ... ],
    "expected": { "type": "rule"|"cloud_file"|... , ... } | [ ... ],   // optional, padded with null
    "options":  { ... } | [ ... ],                                      // optional
    "postconfig": [ { "type": ..., "parameters": ... } ]                // optional
  }
}
```
Manifest `evaluation_examples/test_all.json` maps `domain → [ids]` (chrome 46, multi_apps 101, …,
~369 tasks).

### Primitive / weak parts

- **Stringly-typed, unvalidated evaluator dispatch.** `getattr(metrics, func)` /
  `getattr(getters, 'get_'+type)` with no schema validation; a typo or a mis-padded parallel list
  fails only at runtime (assert at `desktop_env.py:407–409`, or `AttributeError`).
- **Getters reach into live app internals with hardcoded, OS/arch-specific paths.**
  `get_default_search_engine` has 4 branches for Windows/Darwin/Linux-arm/Linux-x86 of the Chrome
  `Preferences` path, repeated verbatim across `get_cookie_data`, `get_profile_name`,
  `get_enable_do_not_track`, etc. (`chrome.py:205–236`). Comments literally warn "not tested on
  Windows and Mac." Any Chrome version/profile change silently breaks grading.
- **Stringly-typed, fragile metrics.** `basic_os.is_utc_0` grabs `timedatectl` line index `[3]`;
  `check_gnome_favorite_apps` calls `eval()` on a guest-returned string (a code-exec footgun);
  `show_result.py` uses `eval()`/`float(bool(str))` fallbacks, so a non-numeric `result.txt` silently
  scores 1.0.
- **Continuous scores treated as binary.** `fuzzy_match` returns `ratio/100`; the harness sums
  results as if 0/1, and AND conjunctions average partial scores into non-binary "success."
- **No real replay.** `get_replay` (`replay.py:4–20`) only re-emits hotkey/typewrite/press via
  pyautogui, is flagged fixme/incomplete, doesn't restore focus or verify state, and isn't wired into
  `evaluate()`. `traj.jsonl` + PNGs are write-only logs, not deterministic, re-executable artifacts.
- **Single end-of-episode snapshot diff.** No partial-credit milestones, no per-step assertions, and
  `postconfig` *mutates* the VM right before scoring (pkill chrome, force `ctrl+s`), so the grader can
  change the state it measures — and flips `is_environment_used`, costing a revert.
- **Fragile timing dependence.** `sleep(60)` after reset, `sleep(20)` before evaluate, postconfig
  sleeps, Playwright/CDP getters racing the UI → flaky scores.
- **Oracle artifacts fetched from external URLs at grade time** (HuggingFace `cloud_file`), with no
  checksum pinning — link rot or drift breaks grading non-deterministically.
- **Massive duplication.** `lib_run_single.py` contains ~15 near-identical `run_single_example_*`
  variants per agent vendor, each re-implementing the loop+logging with subtle divergences.
- **Ad-hoc aggregation.** `results.json` is rewritten in full on every append (O(n²));
  `show_result.py` hardcodes domain ordering and category groupings and persists `all_result.json` via
  `str(dict)` (not valid JSON — must be `eval`'d back).
- **`infeasible` scoring is a last-action heuristic** (`== 'FAIL'`): it cannot distinguish "correctly
  judged infeasible" from "gave up / crashed."

### Reusable patterns

- **Declarative self-describing task config** (instruction + setup + evaluator in one JSON) is a
  clean, portable unit — keep it.
- **Getter / metric two-stage abstraction**: one side is environment introspection, the other a pure
  comparison function — exactly the shape of D7's typed verifier DAG.
- **Result-vs-expected duality** where `expected` is itself a getter (static rule via `get_rule` or a
  computed/downloaded oracle) lets one metric serve both static-answer and golden-file tasks.
- **Composable multi-metric scoring** with explicit and/or conjunction (mean/max) for compound
  criteria.
- **`postconfig` hook** for normalizing/flushing state before inspection — useful **if** made
  non-destructive/read-only.
- **Per-task `traj.jsonl` + mp4** is a good observability baseline to build a real replay format on
  top of.
- **Manifest-driven task selection** (`test_all.json`, plus `test_small`/`test_infeasible`) cleanly
  decouples the corpus from the runner.

### Maps to

D7 (typed verifier DAG, programmatic-primary + constrained model-verifier fallback, golden snapshot
per task, N≥5 CoW replicas → pass@k/pass^k, readiness probes, task+grader+env versioned together —
heeding OSWorld-Verified's 300+ grader bugs, [xlang.ai/blog/osworld-verified](https://xlang.ai/blog/osworld-verified)),
D5 (the `.skn` replay format that `traj.jsonl` only gestures at), D6 (a policy boundary instead of
`eval()`-on-guest-output and `getattr` dispatch).

---

## 5. Agent layer + observation/action representation — `mm_agents/`, `lib_run_single`, `monitor/`

### How it works

The agent layer is a collection of per-model Python classes, all implementing the same *informal*
contract: `predict(instruction, obs) → (response_text, actions)` plus `reset(...)`.

**Observations** come in four types: raw `screenshot` (PNG bytes), `a11y_tree` (AT-SPI/UIA XML
linearized to a TSV table), `screenshot_a11y_tree` (both), and `som` (set-of-marks). The a11y
linearization (`agent.py:71–117`) parses the XML, runs a heuristic `filter_nodes()`, and emits a
tab-separated table (`tag name text class description position size`) **blindly truncated at 10,000
gpt-4 tokens** (`agent.py:217–223`). `judge_node()` (`heuristic_retrieve.py:38–102`) keeps a node only
if its tag matches an allowlist, it is showing+visible, has a name/text, and has non-negative coords +
positive size. Set-of-Marks (`heuristic_retrieve.py:105–214`) draws numbered red boxes over filtered
nodes and emits an index→center-coordinate map so the model can write `pyautogui.click(tag_2)`
(`agent.py:197–214`).

**Action spaces** — at least five incompatible representations:

| Space | Representation | Translation |
|---|---|---|
| `pyautogui` | raw Python code string | run verbatim |
| `computer_13` | JSON `{action_type, parameters}` (`prompts.py:44–287`) | mapped to pyautogui |
| Claude computer-use | tool schema `computer_20251124`, display 1280×720 | `parse_actions_from_tool_call` → pyautogui (`anthropic/main.py:154–364`) |
| OpenAI CUA | `computer_use_preview` tool, `computer_call` | `_convert_cua_action_to_pyautogui_action` (`openai_cua_agent.py:398–641`) |
| UI-TARS | custom DSL, 0–1000 normalized box coords | `parse_action_qwen2vl` → rescale → pyautogui (`uitars_agent.py:106–188`) |

**Every** non-native space is ultimately normalized to a pyautogui code string and `exec`'d in the VM
over plain HTTP via `controller.execute_python_command` (`python.py:195–222`). Sentinels
`WAIT`/`DONE`/`FAIL` (and `CALL_USER` for some) form a tiny universal control vocabulary
(`agent.py:128–129`, `desktop_env.py:423–431`).

The **PromptAgent loop** (`agent.py:289–543`) builds an OpenAI-style messages array (system +
interleaved obs/thoughts for the last `max_trajectory_length=3` steps + current obs), calls
`call_llm()`, parses actions, and appends to `self.thoughts`/`self.actions`. `call_llm()` is a
~540-line `if/elif` ladder on model-name prefix (gpt/azure/claude/mistral/THUDM/gemini/llama3/qwen,
`agent.py:571–1111`) reshaping the same dict into each vendor's payload.

The **Claude agent** (`anthropic/main.py`) maintains Beta messages, resizes the real screenshot to
1280×720 and scales returned coordinates back up by `resize_factor` (`main.py:79–82, 169–178`),
keeps only the N most recent screenshots (`_maybe_filter_to_n_most_recent_images`,
`utils.py:411–457`), injects ephemeral prompt-cache breakpoints (`utils.py:377–409`), halves image
count on 413 errors, supports batched actions in one tool call (`utils.py:206–313`), and embeds the
sudo password in the system prompt (`utils.py:163–185`). The **OpenAI CUA agent** uses
`client.responses.create`, owns its own env reference and `step()`, and detects infeasibility by
keyword scan. The **UI-TARS agent** prompts a single VLM with the DSL, AST-parses each `Action:
fn(args)` line, and types via `pyperclip`+`ctrl+v`.

The **driver** (`lib_run_single.py:14–74`) is the synchronous observe→think→act loop with a hardcoded
`sleep(60)` startup and `sleep(20)` settle. The **monitor** (`monitor/main.py`) is a Flask dashboard
that, *after* a run, reads `traj.jsonl` and serves per-step PNGs and `recording.mp4` over a poll +
full-page-refresh UI — post-hoc observability, not live streaming.

### API / schema

```
predict(instruction: str, obs: Dict) -> (response: str, actions: List[str])   # ~30 vendor variants drift
obs keys: {'screenshot': bytes, 'accessibility_tree': str|None}
obs types: 'screenshot' | 'a11y_tree' | 'screenshot_a11y_tree' | 'som'
action spaces: 'pyautogui' | 'computer_13' | 'claude_computer_use' | (OpenAI CUA / UI-TARS internal)
sentinels: 'WAIT' | 'DONE' | 'FAIL' | 'CALL_USER'
traj.jsonl line: {step_num, action_timestamp, action, response, reward, done, info, screenshot_file}
monitor HTTP (poll): GET /api/tasks(/brief), /api/task/<type>/<id>, .../screenshot/<file>, .../recording
```

### Primitive / weak parts

- **Code-as-action is arbitrary execution by design.** Model output is wrapped as `python -c '{command}'` and run with
  no sandbox, allowlist, or static check; the prompt hands the model the sudo password
  (`utils.py:163–185`); `pyautogui.FAILSAFE` is force-disabled (`python.py:31`), removing the panic
  abort. `execute_python_command`'s own docstring admits "…or any other python command. who knows?"
- **No standard action schema.** Five incompatible representations, each with its own parser and
  ad-hoc translation to pyautogui. Every new model adds another bespoke agent file
  (`o3_agent`, `qwen3vl_agent`, `jedi_*`, `gta1`, `aguvis`, …) re-implementing predict/parse/
  linearize — `linearize_accessibility_tree` is duplicated verbatim in `agent.py:71` and
  `uitars_agent.py:339`.
- **No streaming of intermediate steps.** Fully synchronous and blocking; full PNG re-sent every turn;
  hardcoded 60 s startup + 20 s settle sleeps.
- **Brittle coordinate handling.** PromptAgent asks the LLM to invent absolute pixel coords from an
  image with no grounding model (`prompts.py:8`); the Claude agent hardcodes a 1280×720 base and
  int-truncates scaled coords; UI-TARS relies on aspect-ratio-preserving resize so 0–1000 coords stay
  valid — any deviation silently mislocates clicks.
- **Regex/AST string-munging parsing.** `parse_code_from_string` keys off triple-backtick fences and
  bare WAIT/DONE/FAIL lines; `parse_actions_from_string` returns an error *string* instead of raising;
  UI-TARS hand-rolls quote escaping and `eval()`s box strings.
- **Lossy, heuristic a11y tree** — tag allowlist drops anything without a name/text or with
  non-positive size; the table is truncated mid-row at 10k tokens.
- **Error handling masks failures.** `call_llm` returns `''` on non-200; context-overflow recovery
  drops all history except first+last message; several agents fall back to `['DONE']`/`['FAIL']` on
  parse/client errors, **conflating infra failure with task completion**.
- **Write-only trajectory.** `traj.jsonl` records *intended* action + free-text response + a
  post-execution PNG; there is no record of executed pyautogui vs resulting state diff, no action IDs,
  and `reward` is hardcoded 0 with evaluation only at episode end.
- **Unmaintainable provider dispatch** — a 540-line `if/elif` ladder, secrets read straight from
  `os.environ`, debug artifacts written to cwd (`open('response.json','w')`, `agent.py:948`).
- **Monitor is post-hoc**, not real-time — poll + full-page refresh over files on disk.

### Reusable patterns

- **Uniform agent contract** `predict(instruction, obs) → (response, actions)` + `reset()` — simple
  enough that ~30 heterogeneous models plug in. A clean, *typed* version is the basis for D2's Operator
  contract.
- **Pluggable `observation_type × action_space` matrix** selecting the system prompt
  (`agent.py:256–287`) — formalize as an explicit capability descriptor (D2 capability negotiation).
- **One execution primitive for every vendor space.** The translation tables
  (`parse_actions_from_tool_call`, `_convert_cua_action_to_pyautogui_action`) are a ready-made action
  ontology mapping — exactly the version-pinned bidirectional adapters in D2.
- **Set-of-Marks pipeline** (filter a11y nodes → numbered boxes → index→center map → `tag_N`
  references) is a reusable grounding trick that sidesteps raw-pixel coordinate prediction — D3 Rung-1.
- **Image-history budgeting + auto-halving on 413** (`utils.py:411–457`) and **prompt-cache breakpoint
  injection** are concrete bandwidth/cost levers (D3, D4).
- **Action batching** (multiple actions in one tool call, `utils.py:206–313`) amortizes round-trip
  latency — a useful pattern for reducing observe-act round trips.
- **Sentinel control vocabulary** `WAIT`/`DONE`/`FAIL`/`CALL_USER` layered on any action space.
- **Execution-vs-observability separation** (per-step JSONL consumed by a separate dashboard) — a
  clean seam to upgrade into a streaming event log (D4/D5).

### Maps to

D2 (one canonical typed tagged-union + version-pinned adapters), D3 (screenshot-first observation with structured upgrades and
stable refs), D4 (streaming replaces synchronous full-frame polling), D6 (capability gate replaces
sudo-in-prompt + FAILSAFE-off), D8 (one canonical Operator/SDK contract replaces per-vendor forks).

---

## 6. End-to-end dataflow + the alternative architecture hiding in the submodules

### How it works

The runner `run_multienv.py` spawns N worker processes, each owning its own agent+env, pulling
`(domain, example_id)` tuples off a `multiprocessing.Manager().Queue` (`run_multienv.py:333–348`) —
parallelism is **process-per-VM**, no shared env state, with dead-worker auto-restart
(`run_multienv.py:351–373`). The single-process `run.py` is deprecated. Both hardcode
`os_type='Ubuntu'` (`run.py:167`, `run_multienv.py:186`).

The per-episode flow:

```
LAUNCH    run_multienv → N workers, each: PromptAgent + DesktopEnv
BOOT      create_vm_manager_and_provider → start_emulator → get_ip_address ("host:s:c:v:l")
WIRE      PythonController + SetupController over http://vm_ip:5000
RESET     [revert_to_snapshot] → SetupController.setup(config) via getattr(_<type>_setup)
READY     time.sleep(60)   ← a guess, not a readiness signal
OBSERVE   GET /screenshot (full PNG) + GET /accessibility (full XML) + GET /terminal   (blocking)
ACT       agent.predict → for each action: POST /execute ['python','-c', prefix+cmd] → sleep(pause) → re-screenshot
LOG       step_<n>_<ts>.png + traj.jsonl line  (reward=0, done=False)
RECORD    POST /start_recording (ffmpeg x11grab); /end_recording → recording.mp4
EVALUATE  time.sleep(20) → postconfig → getters reach back into VM via POST /execute → metrics → result.txt
TEARDOWN  env.close → provider.stop_emulator
```

The whole I/O model is **blocking request/response polling**: N synchronous GETs per observation,
a synchronous POST per action, `retry_times=3`/`retry_interval=5` (`python.py:80–81`), and hardcoded
sleeps as synchronization. On a 369-task run with many steps each, the fixed
`sleep(60)`+`sleep(20)`+per-action `sleep(pause)` is enormous dead wall-clock.

**The alternative architecture in the submodules.** `.gitmodules` declares `rdds`
(Remote Desktop Driver Server) and `agp_client` submodules; `mm_agents/surferH/surfer_agent.py:38–86`
shows the modern path: install RDDS on :8087, then `SurferAgent.predict` hands a
`DirectEnvironmentConnection(url=rdds_url)` to a hosted "Agent Platform" (AGP) that drives the VM
**itself** and returns a whole trajectory — OSWorld only provisions + scores. This implies the real
production loop is *persistent driver + remote agent + streaming*, with OSWorld reduced to
provisioning and grading. (Note the footgun: a hardcoded AGP API key is committed in
`surfer_agent.py:24`.) This is precisely the direction Shinken takes — but as an open, typed,
streaming contract rather than a private remote.

### API / schema (the full host↔guest surface)

```
In-VM Flask (host→guest, plaintext HTTP :5000)
  Observe:   GET /screenshot -> image/png ; GET /accessibility -> {AT: xml} ; GET /terminal -> {output}
  Exec:      POST /execute {command:[...],shell} -> {output,error,returncode} ; /run_python ; /run_bash_script
  Files:     POST /file -> bytes ; /list_directory ; /wallpaper ; /setup/upload ; /setup/download_file
  Info:      POST /screen_size,/window_size,/desktop_path ; GET /platform,/cursor_position
  Record:    POST /start_recording ; /end_recording -> mp4
  Setup ns:  POST /setup/{execute,launch,upload,open_file,change_wallpaper,activate_window,
             close_window,download_file,execute_with_verification}
Side-channels: VNC 8006 + noVNC 5910 (human view), Chrome CDP 9222, VLC 8080, RDDS 8087 (surferH only)
```

### Primitive / weak parts (system-level)

- **Blocking polling is the entire I/O model** — no push, no event loop; the host idle-blocks on
  requests.
- **Hardcoded sleeps as synchronization** — guesses, not signals; real readiness checks exist only in
  providers and aren't reused in the loop.
- **Full-frame PNG + full XML every step** — no deltas, no diffing, no ROI, no codec negotiation, no
  caching.
- **Arbitrary code execution, no auth, plaintext HTTP, ports bound 0.0.0.0/0**; static credentials; an API
  key committed in source.
- **No replay / determinism** — `_act_setup`/`_replay_setup` are `NotImplementedError`; `get_replay`
  is a fixme stub; pyautogui injects random easing + random duration (`python.py:315–318`) so even
  action *execution* is non-deterministic; only PNGs + a JSONL of strings persist.
- **`reward`/`done` are fake** — the Gym contract is cosmetic, only a terminal `evaluate()` scores.
- **Config-/eval-by-`getattr` stringification** — reflective, unvalidated injection surface.
- **Massive copy-paste of the control loop** — ~15 `run_single_example_*` + ~34 `run_multienv_*`
  files, because there is no stable agent interface.
- **Single-platform in practice** — `os_type='Ubuntu'` hardcoded; `/terminal` returns 500 on
  non-Linux; the eval suite assumes the Ubuntu image.
- **Leaky provider abstraction** — the port-packing colon string, the `is_environment_used`
  heuristics, and AWS-AMI special-casing in the runner all leak through the ABC.
- **State/concurrency hazards** — `recording_process` is a single module global; no session concept,
  no request isolation.
- **Scalability = process-per-VM + AMI-relaunch** — minute-scale cold-boot per reset on AWS; no warm
  pool, no fork/snapshot-restore fast path in the core (FastVM exists but isn't the default).

### Reusable patterns (system-level)

- **Pluggable Provider/VMManager ABC** — keep the seam, widen it to own lifecycle/pooling/streaming
  (D9).
- **A thin in-VM daemon the host talks to over a uniform API** — keep the *concept*, replace HTTP-poll
  with a bidirectional streaming transport (D4).
- **Declarative task config (setup) + evaluator (getters+metrics+conj)** — clean separation of prep
  from scoring (D7).
- **Snapshot/AMI reset to a known-clean state per task** — the right invariant even if the
  implementation is slow (D1).
- **Resumability via on-disk markers** (`get_unfinished` scans for `result.txt` to skip completed
  tasks, `run.py:234–268`) — cheap, crash-tolerant checkpointing.
- **Resilient parallel execution** — shared task queue + dead-worker restart + graceful
  SIGINT/SIGTERM teardown.
- **Secret injection at boot** (`vm_secret_mounts → upload_file + chmod 600`,
  `desktop_env.py:223–246`) instead of baking into images — generalize into a real secrets/permissions
  layer (D6, Vault/KMS broker so the model never sees plaintext).
- **The surferH/AGP/RDDS pattern** — persistent on-VM driver + remote agent + streaming, harness only
  provisions + scores — is the architectural direction Shinken adopts, made open and typed.

### Maps to

All twelve decisions converge here. The end-to-end map is the single clearest statement of *why*
Shinken inverts OSWorld: D4 replaces blocking polling with a single-PeerConnection WebRTC dual
transport (reliable data channel = the event/replay log; on-demand NVENC media track); D3 replaces
full PNG + full XML as the whole product with screenshot-first, layered structured escalation (~6× token savings where coverage is strong, structured ≈ 150×
cheaper than H.264 office video — both **vendor-published, unverified** pending a first-party
measurement spike, per [`docs/design/tech-decisions.md`](../docs/design/tech-decisions.md)); D5 replaces the
write-only `traj.jsonl`+mp4 with the event-sourced, branchable `.skn` bundle; D6 replaces open arbitrary exec
with the 3-layer capability-unlock permission stack; D1+D9 replace process-per-VM + AMI-relaunch with
warm pools + fork-from-snapshot; D10 replaces the `os_type='Ubuntu'` reality with one Guest Runtime
contract across Linux/Windows/macOS.

---

## 7. Summary scorecard — what to keep, what to replace

| Subsystem | Keep (reusable instinct) | Replace (too primitive) | Decision |
|---|---|---|---|
| In-VM server | uniform one-port control API; per-OS a11y → one namespaced tree; cursor compositing; execute-with-verification | arbitrary exec/no-auth; Flask dev `debug=True`; full-PNG polling; XML blob; X11-only; single-threaded | D2/D3/D4/D6/D10 |
| Client env | gym surface; declarative `{type,parameters}` setup; provider abstraction; dirty-tracking; magic-byte validation | synchronous polling + fixed sleeps; code-as-action; heavy reset; 3× duplicated env; reward cosmetic | D2/D3/D4/D7/D9 |
| Providers | 5-method lifecycle vs allocation split; new-handle-on-revert; bake-once-restore (FastVM); TTL self-terminate | leaky colon-string contract; cost-opaque revert; no fork/CoW; no GPU; no warm pool; public no-auth surface | D1/D9/D11 |
| Evaluators | declarative task JSON; getter/metric two-stage; result-vs-expected duality; and/or conjunction; manifest | `getattr` stringification; `eval()` on guest output; hardcoded OS paths; continuous-as-binary; no replay; flaky sleeps | D5/D6/D7 |
| Agents/obs-action | uniform predict contract; obs×action matrix; vendor→one-primitive translation tables; SoM; image budgeting; sentinels | five incompatible schemas; code-as-action; brittle coords; failure-masking; per-vendor forks | D2/D3/D4/D6/D8 |
| End-to-end | provider seam; in-VM daemon concept; declarative config; snapshot reset; resumable markers; resilient workers; surferH driver direction | blocking poll I/O; hardcoded sleeps; full-frame every step; fake Gym reward; single-platform; process-per-VM scale | D1–D12 |

The one-line verdict for [`docs/design/osworld-analysis.md`](../docs/design/osworld-analysis.md): OSWorld's
**data-model instincts are sound and we keep them** (declarative tasks, getter/metric registry,
snapshot reset, a uniform in-VM control API, the gym surface); what Shinken replaces is the
**transport and runtime** — blocking HTTP polling, full-frame PNGs, code-as-action, a no-auth
arbitrary-exec server, X11/Linux-only assumptions, and slow snapshot-revert — on the four headline axes:
streaming (D4), bandwidth (D3+D4), permissions (D6), and deterministic replay (D5).
