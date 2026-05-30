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
```

## What's here

| Path | Source | Why we keep it |
|------|--------|----------------|
| `OSWorld/` | https://github.com/xlang-ai/OSWorld | Closest prior art: in-VM action server, gym-like client env, multi-cloud VM providers, evaluators, ~40 agent impls. We mine its patterns and document where it is too primitive (single-platform, polling, no streaming/replay/permissions). |

### OSWorld submodules (not initialized)

`OSWorld/.gitmodules` references two private SSH repos used by its `surferH` agent:

- `mm_agents/surferH/rdds` → `git@github.com:hcompai/remote-desktop-driver-server.git`
- `mm_agents/surferH/agp_client` → `git@github.com:hcompai/agp_client_public.git`

These hint at a **real-time remote-desktop driver** approach (relevant to our streaming
goal). They require SSH access we don't assume; left uninitialized.

## Adding a new reference

1. `git clone --depth 1 <url>` into this directory.
2. Add a row to the table above with the source URL and a one-line rationale.
