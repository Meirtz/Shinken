# Shinken

> An **AI-native, cross-platform sandbox runtime + control plane** for computer-use agents.

Shinken gives AI agents a safe, observable, high-fidelity computer to operate — and gives
humans a control panel to watch, steer, gate, record, and replay everything the agent does.
It is the production-grade descendant of research harnesses like
[OSWorld](https://github.com/xlang-ai/OSWorld): cross-platform instead of Linux-only,
streaming instead of screenshot-polling, AI-native instead of `pyautogui`-as-an-afterthought.

## Why

Today's computer-use stacks are research artifacts. They assume one OS (Linux/X11), move the
whole screen as periodic JPEGs over HTTP, have no notion of capability/permission gating, and
can't replay or fork a session. Shinken treats the agent-computer interface as a first-class
product surface: a low-bandwidth structured **observation/action protocol**, real-time
**streaming**, deterministic **replay**, a **permission panel** that unlocks privileged image
features per session, and a runtime that scales to **very high concurrency** in the cloud.

## Headline capabilities

1. **Replay** — record every session as a scrubbable, forkable timeline (structured
   action+observation events, not just video) and re-run agents from any saved state.
2. **Permission management panel** — a layered capability model that gates and *unlocks*
   advanced image features (network egress, GPU, privileged installs, credentials, persistence)
   per session / per agent, with live human approval.
3. **Bandwidth optimization** — send structured operations and frame deltas instead of full
   screenshots; adaptive, hardware-accelerated video only when pixels are truly needed.
4. **Streaming operations (real-time)** — observe and act over a live bidirectional channel
   instead of request/response polling, so humans and agents see operations as they happen.

…plus the AI-native parts: a clean Agent-Computer Interface (ACI), optional MCP exposure,
accessibility-tree observations, and trajectory capture built into the runtime.

## Scope (v1 intent)

- **North star:** one platform serving *both* production agent deployment *and*
  eval/benchmarking, layered so eval sits on top of the production runtime.
- **Guests:** all desktop — **Linux, Windows, macOS**. Mobile (Android) is on the roadmap, not v1.
- **Isolation substrate:** to be recommended from research (see `docs/05-tech-decisions.md`).
- **Deployment:** designed for **cloud + ultra-high concurrency**; dev/test starts **local at
  small concurrency**.

> The name "Shinken" (真剣 / 神剣) — a real, live blade. Sharp by default, safe by design.

## Repository layout

```
shinken/
├─ docs/         Authoritative, relatively stable docs: vision, PRD, architecture,
│                competitive analysis, tech decisions, roadmap, glossary.
├─ notes/        Working notes & teardowns: raw research, open questions, deep dives.
├─ references/   Vendored external projects we study (git-ignored; see references/README.md).
└─ README.md     You are here.
```

- Start with **[docs/README.md](docs/README.md)** for the document index.
- Working research lives in **[notes/README.md](notes/README.md)**.

## Status

📐 **Design corpus complete; pre-implementation.** No runtime code yet. The design is grounded
in a teardown of OSWorld + 8 other cloned reference projects (`references/`) and two deep research
rounds (62 sub-agents) across the sandbox / streaming / replay / permission / agent-interface
landscape.

- **[`docs/`](docs/README.md)** — 11 authoritative docs (~57k words): vision, PRD, architecture,
  OSWorld teardown, landscape, ADRs (D1–D12), roadmap, glossary, threat model, economics, and the
  Phase-0 implementation plan.
- **[`notes/`](notes/README.md)** — 9 working notes (~45k words): per-domain deep dives,
  open questions, and sources.

Next: de-risking spikes (a11y coverage, CoW-fork density, dual-channel streaming latency) and a
Phase-0 local PoC — see [docs/06-roadmap.md](docs/06-roadmap.md) and the
[Phase-0 plan](docs/10-phase0-plan.md). Implementation tracked under the
[**Phase 0** milestone](https://github.com/Meirtz/Shinken/milestone/1).

## Build & run (M0)

Today M0 implements the **ACI v0 handshake**: the Rust Guest Runtime (`shinkend`) and the Python
SDK negotiate capabilities over a WebSocket. Actions, observation, and replay follow in M1+.

```bash
# 1) run the Guest Runtime
cargo run --manifest-path shinkend/Cargo.toml          # ws://127.0.0.1:8765

# 2) drive it from Python
cd sdk/python && pip install -e ".[dev]"
shinken connect                                        # prints platform, rtt, caps
```

```python
import shinken
env = shinken.connect()
print(env.platform, env.screen_size(), env.capabilities.verbs)
env.close()
```

Code layout: `schema/` (ACI + `.skn` JSON Schemas — source of truth) · `shinkend/` (Rust runtime) ·
`sdk/python/` (SDK + Operator) · `images/linux/` (Sandbox image) · `spikes/` · `eval/`.
