# Shinken

[![CI](https://github.com/Meirtz/Shinken/actions/workflows/ci.yml/badge.svg)](https://github.com/Meirtz/Shinken/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

> The open infrastructure stack for computer-use agents: real desktops behind one typed
> interface, low-bandwidth observation, **checkpoint / fork / resume of live runtime state**,
> and eval as thin orchestration over the same runtime agents run on.

Shinken boots real desktops, drives them through a versioned **Agent-Computer Interface
(ACI)**, and treats running state as a first-class object: name a checkpoint of a live
session, fork it into N verified replicas, resume it later. Evals run on that primitive
(golden state → fork-N → score) instead of re-creating environments per attempt — the
inversion of a benchmark harness. It is not a benchmark, a cloud browser, a VNC desktop, or a
model adapter; it is the runtime those plug into.

What is real today is a **measured Linux/X11 vertical slice under live CI** — every claim
below links to first-party data you can rerun ([`benchmarks/`](benchmarks)) or audit
([`docs/benchmarks/`](docs/benchmarks/README.md)). What is design-only is marked, here and in
the [status map](docs/engineering/status.md).

## Quickstart

```bash
# 1) run the Guest Runtime (loopback, no token)
cargo run --manifest-path shinkend/Cargo.toml

# 2) install the Python SDK
cd sdk/python && pip install -e ".[dev]"
```

```python
import shinken

with shinken.connect() as env:                       # connect + ACI handshake
    print(env.platform, env.screen_size())           # 'linux'  {'w':…, 'h':…}
    shot = env.screenshot(format="jpeg", quality=80) # the bandwidth lever (PNG is default)
    env.click(x=640, y=420)
    env.type_text("real desktops, one typed interface")
```

**Checkpoint / fork / resume** — runtime state is provider-managed, so open the session
through a provider (local Docker here):

```python
from shinken import DockerLocalProvider, SandboxSpec

provider = DockerLocalProvider()
env = provider.connect(provider.create(SandboxSpec()))

ckpt = env.checkpoint(name="golden")            # sub-second, sandbox stays live
replica = provider.connect(env.fork())          # live replica branched from here
restored = provider.connect(env.resume(ckpt))   # checkpoint back to a connectable sandbox
```

```python
from shinken.eval import run_eval_forked
summary = run_eval_forked(task, provider, n=5)  # golden → fork-N → score, ONE checkpoint
```

**Drive with a model adapter** — model dialect in, validated ACI actions out:

```python
from shinken.adapters import AnthropicComputerUseAdapter

adapter = AnthropicComputerUseAdapter()
with shinken.connect() as env:
    action = adapter.to_aci_action(model_tool_call)   # one validated, normalized ACI action
    verb = action.pop("verb")
    env.act(verb, action.pop("target", None), **action)
    result = adapter.to_tool_result(env.screenshot()) # back in the model's grammar
```

**Many sandboxes, one process** — the async core is the native fan-out path; `SharedLoop` is
the sync convenience (one event-loop thread for all N):

```python
with shinken.SharedLoop() as loop:
    envs = [shinken.connect(addr, token=tok, loop=loop) for addr, tok in endpoints]
    shots = [e.screenshot(format="jpeg") for e in envs]
```

## Architecture

Solid = built and in CI today. Dashed = designed, not yet built.

```mermaid
flowchart LR
  subgraph proc["one client process"]
    Agent["Agent / Operator<br/>Anthropic · OpenAI · Kimi · OSWorld dialects"]
    SDK["Shinken SDK<br/>canonical ACI: typed action ⇄ observation"]
    Agent -->|model tool call| SDK
    SDK -->|validated result| Agent
  end
  subgraph box["Sandbox (local Docker today)"]
    SK["shinkend<br/>Guest Runtime (Rust)"] --> Desktop["real desktop<br/>Linux/X11"]
  end
  SDK <-->|"WebSocket · act + observe<br/>PNG · JPEG · lossless tile-delta stream"| SK
  Provider["Provider<br/>boot · checkpoint · fork · resume<br/>(runtime state lives here)"] -.manages.-> box
  Provider --> Eval["eval on the runtime<br/>run_eval_forked: golden → fork-N → score"]

  SDK -.-> A11Y["structured observation<br/>AT-SPI · CDP · UIA · AX (D3, hybrid)"]:::d
  CP["Control Plane<br/>scheduling · capability scoping"]:::d -.-> Provider
  Human["human reviewer"]:::d -.-> Panel["Control Panel<br/>watch / take over"]:::d
  Panel -.-> CP
  classDef d stroke-dasharray:5 5,stroke:#999,color:#666;
```

Most CUA stacks run `screenshot → model → pixel click → sleep → repeat` and throw the run
away. Shinken's loop is typed at every edge and lands in checkpointable state:

```text
observation (pixels now, hybrid structured designed) → typed action → verified result → checkpointable state
```

## Measured results

First-party numbers; **~100k tracked datapoints** across thirteen rerunnable local suites plus
audited one-off WAN runs. Full tables, provenance, and evidence-class labels:
[`docs/benchmarks/`](docs/benchmarks/README.md); methodology:
[`docs/engineering/benchmarks.md`](docs/engineering/benchmarks.md).

**Head-to-head vs OSWorld's guest server — same guest, same display, same frames.** Both
servers in one sandbox, OSWorld's unmodified Flask/pyautogui server driven exactly as its own
client drives it (pinned commit, frame parity verified at **0.0 mean pixel delta**): a full
act-then-observe agent step costs **191.8 ms p50 over OSWorld's HTTP interface vs 5.37 ms over
the ACI (~36×)** — ~5.2 vs ~186 steps/s of pure runtime overhead. Bytes/step at default codecs
honestly favors OSWorld (its PIL-encoded PNG is denser than `shinkend`'s speed-tuned encoder);
the ACI takes the wire game back with the measured JPEG/downscale/delta levers below.

**Concurrency — real sandboxes first, then the client-plane ceiling.** One process drives
**64 real Docker desktops** (~1,260 observations/s aggregate, 2 OS threads). To measure the
client plane past one host's guest RAM, the same SDK then holds **1,024 concurrent live ACI
sessions on one event-loop thread** — real handshake, real WebSockets, protocol-faithful
loopback peers serving synthetic frames sized to measured codec operating points — sustaining
**2,356 frames/s ≈ 884 Mbps** of decoded frames for 20 s at ~1 CPU core.

<p align="center"><img src="docs/assets/bench/client_scale.png" width="820"></p>

**Runtime state — the differentiator, measured on the disk tier.** Checkpointing a live
sandbox takes **~0.57 s** and is non-disruptive. After the push-based readiness work (S9: guest-side
`ready` query; `provider.create()` 7.7 s → **~0.2 s** p50), a classic fork→usable is
**~0.6 s**, and the opt-in **warm-pool graft** (pre-booted containers + the checkpoint's
filesystem delta) reaches **~0.12 s** — every replica verified to inherit the golden state,
files-only semantics (the same tier as `docker commit`). And because forked replicas render
identical pixels *by construction*, **content-negotiated observation** (`if_none_match`
against a raw-pixel `frame_hash`, one shared `FrameCache` across the fleet) collapses the
fleet's observe traffic to ~one stream: measured **15.2×** wire cut over N ∈ {4, 8, 16}
forked fleets (~725× at steady state, honest divergence curve — benchmarks §10). The **CRIU
memory tier is de-risked by a positive spike**: the full desktop tree dumps in ~60 ms and restores into a
fresh container in ~40 ms — carrying *live process/memory state*, which no files-only
mechanism can (`spikes/criu-memory-tier/`; privileged rig, latency evidence only). The CoW
fast tier remains designed.

<p align="center"><img src="docs/assets/bench/boot_waterfall.png" width="820"></p>

**Observation bandwidth — a content-dependent lever, not magic.** On a content-rich 1080p
frame (remote, WAN): JPEG q80 turns 1804 KiB into 87 KiB (**20.7×**), and downscale stacks to
**~131×** at @512. On sparse flat UI, **PNG wins outright** — which is why PNG is the lossless
default and JPEG/downscale are explicit knobs. During interaction the **lossless dirty-tile
delta** stream is the robust win: **11.3×** vs full-PNG while typing, zero quality loss, and
idle costs ~zero. Projected egress at 1024 sandboxes × 1 Hz: ~405 Mbps (JPEG q80@1280) vs
~15 Gbps (full-res PNG) — *projection from measured frame sizes, labeled as such*.

Two pipeline mechanisms make the cost **change-proportional end to end**: negotiated
**binary WS frames** kill the base64+JSON tax (wire −25%; saturated client-plane ceiling
4,032 → **7,713 frames/s, ~1.9×**), and **XDamage event-driven capture** makes an idle
streaming sandbox cost **~0 guest CPU** (4.4–8.6% → 0.1–0.6% of a core; typing@30fps 6.4×
cheaper) — an idle tick captures nothing at all. The remote 20.7× JPEG headline is also now
**reproducible from this repo alone**: a procedurally generated photographic frame confirms
**19.3×** locally (no binary assets, seeded).

<p align="center"><img src="docs/assets/bench/bandwidth_bars.png" width="680"></p>

**Structured observation — measured, verdict: hybrid.** Accessibility-tree coverage (spike
E5): strong for Qt (0.87 addressable) and for browser *controls* via CDP (1.00 of labeled
controls; 0.23 of all nodes), weak for GTK, absent for terminals — so the structured-*default*
stays provisional (D3) and the shipped design is per-window structured + pixel fallback.

**Functional.** Single-task OSWorld end-to-end gate passed (1 task of the 369-task suite:
Kimi K2.6 over `shinkend`, official OSWorld evaluator **score 1.0**, 6 steps, 110 s — a full
conformance sweep has not been run). 98 Rust + 518 Python tests in a 9-job CI; line coverage
measured (**78% Rust / 87% Python**) with per-verb test traceability; every README snippet is
itself executed by the test suite.

## How it compares

Shinken's wedge is the unclaimed intersection, not winning any single axis. Survey date
2026-06; competitor figures are vendor-published, sources in
[`docs/design/landscape.md`](docs/design/landscape.md).

| | cross-OS desktop | runtime fork | structured + pixel obs | eval on same runtime | streaming |
|---|---|---|---|---|---|
| **Shinken** | designed (Linux built) | **disk tier built + measured, local-first** | hybrid (coverage measured) | **yes — `run_eval_forked` built** | PNG/JPEG/delta built; WebRTC designed |
| trycua/cua | yes | cloud-only — local `snapshot()` raises (measured); local verbs = `docker pause` / stopped-VM clone | a11y trees | recreates env per reset | VNC + polled PNG (measured: 174 ms/step vs our 2.9 ms) |
| E2B desktop | Linux | cloud pause/resume, 1:1 (API-key required — no keyless/local mode, measured) | none | n/a | raw VNC |
| Morph | Linux | **ms-class CoW (vendor-published P99 ~1.3 ms)** | none | n/a | n/a |
| OSWorld | Linux (in practice) | slow revert, no fork | full-XML per step | *is* the benchmark | full-frame PNG poll |
| browser SaaS | no (Chromium only) | no | DOM | no | WebRTC/HLS |

The cua and e2b cells marked *measured* are first-party, rerunnable numbers — both stacks as
shipped, same host, same window, pinned versions
([S12](docs/engineering/benchmarks.md), [`docs/benchmarks/`](docs/benchmarks/README.md) §7).

## Integrations

Adapters that plug Shinken under stacks that already exist (duck-typed protocol shapes, no
hard dependency on the target framework; each ships fixture tests + a runnable example):

- **OSWorld** — a `DesktopEnv`-shaped shim (`shinken.osworld`) + OSWorld-as-a-Workload
  (`osworld-eval`): OSWorld-format pyautogui/computer_13 actions actuate over the typed ACI,
  OSWorld's own evaluator scores (alpha gate passed at score 1.0).
- **uni-agent / verl** — `shinken.integrations.swerex` implements the SWE-ReX deployment/runtime
  protocol [uni-agent](https://github.com/verl-project/uni-agent) drives its sandboxes through, so
  verl-style rollout collection runs on Shinken sandboxes (with fork-from-golden-checkpoint
  `start()`); see [`examples/uniagent_shinken.py`](examples/uniagent_shinken.py) and
  [agent-runtime.md](docs/design/agent-runtime.md).
- **CUA-Gym** ([xlang-ai/CUA-Gym](https://github.com/xlang-ai/CUA-Gym)) —
  `shinken.integrations.cua_gym`: exported task bundles as a `TaskSource` + their VM-env
  method surface, with **fork-native reset** — bundle setup runs once into a golden
  checkpoint and every `reset()` forks a fresh replica from it (seconds on the Docker disk
  tier) instead of provisioning a fresh cloud VM per environment. 32k oracle-validated RLVR
  tasks, zero authoring. Example: `examples/cua_gym_shinken.py`.
- **Agentix** ([Agentix-Project/Agentix](https://github.com/Agentix-Project/Agentix)) —
  `shinken.integrations.agentix`: a `SandboxProvider`-shaped provider (async
  `create/delete/get` + scoped `session()`) exposing `DockerLocalProvider` + the typed ACI to
  their orchestration, with `golden=<checkpoint>` turning every `create()` into a fork from a
  golden state. Example: `examples/agentix_shinken.py`.
- **ProRL-Agent-Server** ([NVIDIA-NeMo/ProRL-Agent-Server](https://github.com/NVIDIA-NeMo/ProRL-Agent-Server))
  — `shinken.integrations.prorl_agent_server`: a rollout-as-a-service runtime plugin
  (`BaseRuntime` contract — `start/stop/cancel`, `exec`, file up/download) giving each rollout
  session one provider-managed Shinken sandbox, with the INIT stage mapped onto
  **resume-from-golden** instead of a cold boot. Example: `scripts/prorl_runtime_example.py`.

## Status — honest built-vs-designed map

The authoritative map is [`docs/engineering/status.md`](docs/engineering/status.md); the
numbers behind every "measured" are in [`docs/benchmarks/`](docs/benchmarks/README.md).

| area | state | what exists |
|---|---|---|
| ACI v0 (typed actions + observation) | ✅ built | handshake/auth, pointer+keyboard via X11/XTEST (incl. `drag` + `mouse_down`/`mouse_up`), screenshot, act-returns-observation (`observe`), real-time screencast (idle-suppress, downscale, reconnect), focused-window capture, `list_windows` enumeration; **17 verbs**, contract-tested |
| Structured observation (Linux v1) | ✅ built | guest `observe` engine in `shinkend` (AT-SPI): stable element ids, `tree_text` diff rendering, settle; guest-resolved `element_ref` targets + `invoke_action`/`set_value`; live Docker smoke |
| Observation codec | ✅ built + measured | PNG lossless default; JPEG lever **~1–21× content-dependent** (PNG can win; ~131× stacked with downscale on content-rich frames); **lossless dirty-tile delta ~11× on text** |
| Runtime state | ✅ built + measured | Docker disk-tier **checkpoint / fork / resume** behind a provider interface; `run_eval_forked`; checkpoint ~0.57 s; fork→usable ~0.7 s classic / **~0.1 s warm-pool graft** (state-verified); boot→usable ~0.2 s after S9 push-based readiness |
| Concurrency | ✅ built + measured | async core + `SharedLoop`: **64 real sandboxes** on 2 threads; client plane held to **1,024 live ACI sessions on one loop thread** (~884 Mbps sustained ingest, protocol-faithful synthetic peers); `ping_jitter` fleet decorrelation |
| Eval | ✅ built | tiny verifier harness, OSWorld-as-a-Workload, typed exit-reason, subprocess scorer isolation; **single-task OSWorld gate passed (score 1.0; no conformance sweep yet)** |
| SDK + adapters | ✅ built | Python SDK (sync + async), TypeScript SDK, Anthropic/OpenAI/Kimi-VL adapters → canonical ACI |
| Structured a11y/DOM default (D3) | ⏳ provisional | coverage measured (E5): hybrid per-window structured + pixel fallback, *not* structured-by-default |
| Capability scoping (D6) | ○ mostly designed | a sandbox is granted the resources its task needs; local gateway shim records the envelope; control-plane enforcement designed |
| CRIU memory + sub-second CoW fork | ○ designed | only the Docker disk tier is built |
| macOS engine (D14) | 🟡 v1 slice | native CoreGraphics capture + CGEvent input in `shinkend` (`--backend macos`), TCC-honest readiness; local-only proof — no mac CI; AX tree designed |
| Control plane, WebRTC/GPU, Windows/Wayland, `.skn` replay | ○ designed | reference path collapses these to one local `shinkend` |

## Why "Shinken"?

Most computer-use sandboxes are *mogitō* — training swords: fine for demos and benchmarks,
not built for real side effects, forkable state, or scale. **Shinken (真剣)** means a real
sword — and idiomatically, *doing something in earnest*: a runtime with typed actions,
checkpointable state, and eval on the same substrate production agents run on.

## Repository layout

```text
shinken/
├─ schema/         ACI JSON Schema (the wire contract)
├─ shinkend/       Rust Guest Runtime inside the Sandbox
├─ sdk/python/     Python SDK + CLI       sdk/typescript/  TS control-surface SDK
├─ images/linux/   Local Linux Sandbox image
├─ examples/       Runnable interop examples (CUA-Gym, Agentix, uni-agent — scripted, no model API)
├─ benchmarks/     Rerunnable benchmark suites + tracked raw results (local + remote CSVs)
├─ spikes/         a11y-coverage spike (E5) evidence
├─ docs/           Design canon (ADRs D1–D12), engineering status, benchmark report
└─ notes/          Working notes: per-domain deep dives, open questions, sources
```

- [Benchmark report](docs/benchmarks/README.md) — every headline number with provenance.
- [Implementation status](docs/engineering/status.md) — precise built-vs-designed map.
- [Design canon](docs/design/README.md) — scope, architecture, ADRs, tradeoffs.
