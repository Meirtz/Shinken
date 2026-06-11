# references/ — external assets

This directory holds **cloned/vendored external projects** we study for design input.
It is **git-ignored** (see root `.gitignore`); only this README is tracked, so the repo
stays lean while provenance and re-clone steps are preserved.

## How to re-create

```bash
cd references

# OSWorld — the primary prior-art reference (a benchmark + primitive runtime for
# computer-use agents; X11/Linux + pyautogui based). We critique it in docs/notes.
git clone --depth 1 https://github.com/xlang-ai/OSWorld.git

# The other 8 prior-art repos studied for design (vendored, git-ignored):
git clone --depth 1 https://github.com/trycua/cua.git                         # cua
git clone --depth 1 https://github.com/openai/codex.git                       # codex
git clone --depth 1 https://github.com/anthropics/anthropic-quickstarts.git   # anthropic-quickstarts
git clone --depth 1 https://github.com/m1k1o/neko.git                         # neko
git clone --depth 1 https://github.com/OpenAdaptAI/OpenAdapt.git              # OpenAdapt
git clone --depth 1 https://github.com/e2b-dev/desktop.git e2b-desktop        # e2b-desktop
git clone --depth 1 https://github.com/bytedance/UI-TARS-desktop.git          # UI-TARS-desktop
git clone --depth 1 https://github.com/microsoft/OmniParser.git               # OmniParser

# Added 2026-06 (training-era / agent-env-bridge landscape refresh):
git clone --depth 1 https://github.com/verl-project/uni-agent.git             # uni-agent
git clone --depth 1 https://github.com/xlang-ai/CUA-Gym.git                   # CUA-Gym
git clone --depth 1 https://github.com/Agentix-Project/Agentix.git            # Agentix
git clone --depth 1 --branch stable https://github.com/NVIDIA-NeMo/ProRL-Agent-Server.git  # ProRL-Agent-Server
```

## What's here

| Path | Source | Why we keep it |
|------|--------|----------------|
| `OSWorld/` | https://github.com/xlang-ai/OSWorld | Closest prior art: in-VM action server, gym-like client env, multi-cloud VM providers, evaluators, ~40 agent impls. We mine its patterns and document where it is too primitive (single-platform, polling, no streaming/replay/permissions). |
| `cua/` | https://github.com/trycua/cua | Computer-use agent stack (cua-bench/cua-gym, in-sandbox computer-server, lume/lumier QEMU+Docker virtualization, SoM grounding). Studied for its virtualization and in-sandbox execution layering. **Deep teardown 2026-06-11** (refreshed to `origin/main`, pinned at `2925b491c20595ae850e3e4a05d6fea188e8f40a`, 2026-06-08): all `file:line` receipts in [`notes/cua-teardown.md`](../notes/cua-teardown.md) refer to that pin. The monorepo contains every component (`libs/python/{computer-server,cua-sandbox,agent,som}`, `libs/lume` Swift, `libs/cua-bench`, `libs/cua-driver`, docs site in `docs/`) — no separate org repos worth cloning (`pylume` is archived/absorbed into `libs/lume`). |
| `codex/` | https://github.com/openai/codex | OpenAI Codex CLI (codex-rs). Studied for its sandboxing + permission UX and Rust agent-runtime patterns. |
| `anthropic-quickstarts/` | https://github.com/anthropics/anthropic-quickstarts | Anthropic computer-use reference loop (tool schema, screenshot↔tool_result rendering). The adapter contract our Anthropic adapter targets. |
| `neko/` | https://github.com/m1k1o/neko | WebRTC remote-desktop in a container (GStreamer pipeline, multi-user). Studied for the real-time streaming/transport design. |
| `OpenAdapt/` | https://github.com/OpenAdaptAI/OpenAdapt | Record-and-replay of GUI process automation. Studied for the session-recording/replay model. |
| `e2b-desktop/` | https://github.com/e2b-dev/desktop | E2B desktop sandbox (sandbox-as-a-service, SDK shape). Studied for the provider/SDK boundary. |
| `UI-TARS-desktop/` | https://github.com/bytedance/UI-TARS-desktop | ByteDance UI-TARS desktop agent. Studied for its action grammar and coordinate conventions ([0,1000] integers — contrast with our normalized targets). |
| `OmniParser/` | https://github.com/microsoft/OmniParser | Microsoft OmniParser/OmniTool screen-parsing → structured elements. Studied for the set-of-marks / structured-observation angle (D3). |
| `uni-agent/` | https://github.com/verl-project/uni-agent | verl-team unified build/run/**train** agent framework (RL rollout collection + training backend). Studied for the train-lane interop seam (#223): Shinken as a stateful env/substrate under verl-style rollout collection. |
| `CUA-Gym/` | https://github.com/xlang-ai/CUA-Gym | OSWorld-team follow-up: scaled, verifiable training environments + task hub for computer-use agents (arXiv:2605.25624). Studied for the task/verifier pipeline and as a candidate second Workload behind the OSWorld one. |
| `Agentix/` | https://github.com/Agentix-Project/Agentix | "Universal bridge between agents and environments" (eval / RL / rollout collection without bespoke glue). Closest conceptual neighbor to our narrow-waist agent-runtime; studied for bridge/plugin abstractions and interop positioning. |
| `ProRL-Agent-Server/` | https://github.com/NVIDIA-NeMo/ProRL-Agent-Server | Rollout-as-a-service control plane for agent RL (Apache-2.0; arXiv:2603.18815): HTTP rollout API, gateway INIT→RUN→EVAL assembly line, token-in/token-out trajectory capture, pluggable per-session runtimes via `RuntimeSpec.import_path`. Studied for the runtime plugin contract our `integrations/prorl_agent_server.py` implements. |

### OSWorld submodules (not initialized)

`OSWorld/.gitmodules` references two private SSH repos used by its `surferH` agent:

- `mm_agents/surferH/rdds` → `git@github.com:hcompai/remote-desktop-driver-server.git`
- `mm_agents/surferH/agp_client` → `git@github.com:hcompai/agp_client_public.git`

These hint at a **real-time remote-desktop driver** approach (relevant to our streaming
goal). They require SSH access we don't assume; left uninitialized.

## Adding a new reference

1. `git clone --depth 1 <url>` into this directory.
2. Add a row to the table above with the source URL and a one-line rationale.
