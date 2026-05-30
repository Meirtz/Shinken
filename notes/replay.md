# Replay — event-sourced trajectories, the `.skn` bundle, branching, and the replay panel

> **Status:** drafting · feeds [`docs/05-tech-decisions.md`](../docs/05-tech-decisions.md) **D5** · Last updated 2026-05-30
> Sibling notes: [streaming-bandwidth.md](streaming-bandwidth.md) (the event/media planes that produce this log), [sandbox-infra.md](sandbox-infra.md) (the CoW-fork substrate), [permissions.md](permissions.md) (permission events are first-class replay events), [ai-native-interface.md](ai-native-interface.md) (the action/observation schema the log carries), [eval-benchmarks.md](eval-benchmarks.md) (replay as eval/training data).

Replay is the first of Shinken's four headline features and the one that most sharply separates it from the [landscape](../docs/04-landscape.md). Fast-snapshot vendors ship the *fork* primitive but leave the timeline, event log, and human UX to you; web session-replay tools (rrweb, Playwright Trace Viewer, Sentry) ship beautiful *replay UX* but only ever *reconstruct* a recording — they never re-execute it. Shinken's bet, fixed in **D5**, is twofold: (1) **the event stream IS the replay log** — the same reliable-ordered data channel that drives a live session ([D4](../docs/05-tech-decisions.md)) is, byte-for-byte, what we persist; and (2) **branching/time-travel is the same primitive as instant reset** ([D1](../docs/05-tech-decisions.md)) — copy-on-write-fork a snapshot node and re-run. This note covers why we deliberately do **not** chase bit-deterministic replay, the bisected *snapshot + event-log + observation-log* model, a concrete `.skn` on-disk format sketch, branching over a CoW fork DAG, the replay-panel UX, and replay-as-training-data for computer-use-agent (CUA) model and eval teams. Every speed/density/cost number below is **(vendor-published, unverified)** unless explicitly first-party.

---

## 1. Why bit-deterministic replay is impractical for a full desktop

The seductive idea is to record *everything* so faithfully that you can re-execute the entire desktop instruction-for-instruction and reproduce the run exactly. The production tooling for this exists and is mature — and after surveying it we conclude, and **D5 codifies**, that full-desktop bit-determinism is the wrong target.

| Tool | Mechanism | Why it doesn't fit a Shinken desktop |
|------|-----------|--------------------------------------|
| **rr** ([rr-project.org](https://rr-project.org/)) | Records inputs crossing the kernel boundary; deterministic replay on a **single core** | Single-core only; ~10–20× recording slowdown (vendor, unverified); Linux/x86-bound; no Windows/macOS. Borrow the *boundary* idea, not the tool. |
| **Hermit** ([facebookexperimental/hermit](https://github.com/facebookexperimental/hermit/blob/main/README.md)) | Reverie syscall interception + deterministic scheduler | **Maintenance mode**; no filesystem/network determinism; reference only. |
| **Antithesis** ([deterministic hypervisor](https://antithesis.com/blog/deterministic_hypervisor/)) | bhyve fork, one core, **simulated** environment | "Most complete," but achieves determinism *only by simulating the whole environment* — exactly what Shinken cannot do for real apps, GPUs, and live networks. It proves the approach is infeasible at our scope. |
| **gVisor C/R** ([`runsc`](https://gvisor.dev/docs/user_guide/checkpoint_restore/)) | Checkpoint/restore into a new container | Production — but a restore is a **restartable starting point, not a deterministic continuation**. This is the model we *do* adopt, for state-replay, not bit-replay. |
| **CRIU + cuda-checkpoint** ([CRIU](https://criu.org/Checkpoint/Restore), [CUDA C/R](https://developer.nvidia.com/blog/checkpointing-cuda-applications-with-criu/)) | Freeze a process tree; drain/restore GPU memory | x64-only, no UVM/MIG/MPS, driver 550+; GPU checkpointing is hard and platform-asymmetric. |

Three first-principles reasons bit-determinism is a dead end at desktop scope:

1. **Nondeterminism is intrinsic, not incidental.** A computer-use run depends on real wall-clock time, live network responses, GPU scheduling, and — fatally — the model. Both major model vendors document that **`temperature=0` and a fixed seed do NOT guarantee identical output**; the field has observed accuracy variance up to ~15% and best-vs-worst gaps up to ~70% across nominally "deterministic" runs (vendor-published, unverified; see [LLM determinism limits](https://medium.com/@2nick2patel2/llm-determinism-in-prod-temperature-seeds-and-replayable-results-8f3797583eb1)). You cannot bit-replay a black box you don't control.
2. **The only path to full-machine determinism is to simulate the machine** (Antithesis). Shinken runs *real* microVM/VM guests against the *real* world; simulation defeats the entire point of being a production runtime, and the simulated-vs-real gap is itself a source of eval invalidity.
3. **Cross-platform asymmetry.** rr/Hermit/CRIU are Linux/x86 stories. Windows and macOS — both v1 scope ([D10](../docs/05-tech-decisions.md)) — have no equivalent, and even on Linux, GPU-accelerated guests cannot be fast-forked ([D1/D11](../docs/05-tech-decisions.md)). A determinism strategy that only works on one of three guest OSes (and not the GPU tier) is not a strategy.

There is also a determinism-cost trap: rr-style full record carries the ~10–20× slowdown above, which is unacceptable for a streaming-first production runtime. The right lesson from rr is its *recorded-input boundary*: record inputs crossing a chosen boundary and re-execute inside it. **D5 draws that boundary around the agent reasoning core, not the OS.** Bit-determinism is achievable *only* for the agent's decision sequence, by recording each model/tool result as a replayable input (seed + response id + tool-call result). Everything below the agent — the desktop — is reproduced by the pragmatic **state-snapshot + event-log + observation-log** model: a snapshot is an honest *restartable starting point*, and the log is an honest *recording of what happened* — never a promise that re-execution diverges by zero bits. A snapshot you restore should be treated as resume-tolerant: expect a clock jump, a new IP/MAC, dropped TCP, and reused entropy, so we reseed RNG/VMGenID at restore (see §4).

---

## 2. The bisected model: snapshot + event-log + observation-log

The core architecture bisects state into two coordinated halves and pins both to **one logical clock**. The environment half is the OS/app state captured by the substrate (a microVM/VM/process snapshot); the agent half is the model orchestrator's state (message history, plan, scratchpad, RNG seed, step/token counters). They are snapshotted atomically at the *same* `seq` and the event log threads through both.

```
            ┌───────────────────────────────────────────────┐
            │  ONE LOGICAL CLOCK  (monotonic seq + interval dt)│
            └───────────────────────────────────────────────┘
   ENV half (substrate)                    AGENT half (orchestrator)
   ┌───────────────────┐                   ┌────────────────────────┐
   │ microVM / VM /     │                   │ messages, plan,         │
   │ process snapshot   │◄── same seq ────► │ scratchpad, RNG seed,   │
   │ (RAM+devices+disk) │   (atomic pair)   │ step/token counters     │
   └─────────┬─────────┘                   └──────────┬─────────────┘
             │                                        │
             ▼                                        ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  EVENT LOG (append-only)  kind ∈ {action, observation,         │
   │  decision, permission, marker, snapshot_ref, meta}             │
   │   action ──action_id──► observation   (before/after pairing)   │
   └──────────────────────────────────────────────────────────────┘
```

**Four channels, one timeline.** Drawing on the convergent prior art — terminal casts ([asciicast v2/v3](https://docs.asciinema.org/manual/asciicast/v3/)), web session replay ([rrweb](https://github.com/rrweb-io/rrweb), [Playwright trace.zip](https://playwright.dev/docs/trace-viewer)), agent observability ([OTel-GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/), [OpenInference](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md), [Langfuse](https://langfuse.com/docs/observability/data-model)), computer-use dumps ([OSWorld traj.jsonl](https://github.com/xlang-ai/OSWorld), [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt)), and agent checkpointing ([LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence)) — Shinken records four logical channels onto one clock:

| Channel | Carries | Borrowed shape |
|---------|---------|----------------|
| **Action** | Every ACI verb the Operator issues (click/type/scroll/key/tool_call/shell), with element-ref-first targeting | OpenAdapt `ActionEvent` (mouse/key/canonical-key + composite `parent_id`); paired to its observation by `action_id` (Playwright `callId`) |
| **Observation** | a11y/DOM tree snapshots + typed deltas, terminal output, window events; raw pixels only at the pixel rung | rrweb `FullSnapshot` + `IncrementalSnapshot`; OpenAdapt delta-encoded screenshots (`png_diff`) |
| **Decision** | The model trajectory: prompt/params/model id, **seed**, **response id**, tool calls with `call.id`, token usage | OTel-GenAI `gen_ai.*` spans (primary); OpenInference `llm.*`/`tool.*` as compat aliases |
| **Permission** | Every capability request, grant, denial, revoke, escalation | First-class events, the highest-priority marker class ([D6](../docs/05-tech-decisions.md)) |

The load-bearing rule is the bisection invariant from the trajectory survey: *snapshot only the agent half (LangGraph) and "re-run from state" diverges; snapshot only the env half and the agent forgets why it acted.* Both halves must be captured at the same `seq`, and **every snapshot pins an exact event-log offset** so that scrub-to-T and re-run-from-T cost **O(nearest snapshot + tail of events)**, never O(whole history). This is the snapshot-cadence economics: full bisected snapshots are anchored to *semantic* boundaries (each agent step, each permission gate, each side-effecting tool call), not wall-clock, with env diff-snapshots and event-log deltas in between. Too sparse and the replay tail is long; too frequent and storage balloons — step boundaries are the established sweet spot.

**One code path for live and replay.** The single most important anti-divergence rule: the *same canonical events* go to the live control plane and to `events.jsonl`. Live steering tails the stream with the playhead pinned to "now"; replay loads a finished bundle with the playhead free. There is no second serializer, so "works live, broken on replay" cannot happen. This is also why the replay panel and the live Control Panel are one component in two modes (§5).

---

## 3. A concrete `.skn` format sketch

`.skn` is a self-contained ZIP, modeled on the Playwright `trace.zip` packaging that opens in a zero-server offline viewer. The design fuses the proven primitives: a two-level discriminated envelope (rrweb `EventType` + `IncrementalSource`), interval timing + monotonic seq + dual wall/monotonic anchors (asciicast v3 + Playwright), out-of-band content-addressed media (Playwright `resources/<sha1>`, OSWorld screenshot-by-filename), an OTel-GenAI decision channel, a bisected snapshot, and an immutable checkpoint DAG (LangGraph parent pointers).

```
session-2026-05-30T14-02-11Z.skn   (a ZIP)
├── manifest.json          # schema_version, session/run ids, t0_wall + t0_mono,
│                          #   platform, capabilities, channel list, codec info,
│                          #   checkpoint_dag (parent-pointer tree)
├── events.jsonl           # the canonical ordered event log (source of truth)
├── decisions.jsonl        # OTel-GenAI / OpenInference spans for the agent trajectory
├── network.jsonl          # HAR-style egress records (optional)
├── index.json             # sidecar scrub index: seq → {abs_t, byte_offset, kind};
│                          #   snapshot/marker/keyframe locations (O(log n) seeks)
├── snapshots/
│   └── <checkpoint_id>/
│       └── meta.json      # {checkpoint_id, parent_checkpoint_id, run_id, step, seq, t,
│                          #  env_state{type: microvm|process|none, ref},
│                          #  agent_state{messages_ref, plan_ref, scratchpad_ref, rng_seed},
│                          #  side_effect_cursor}
├── resources/
│   └── <sha1>.{png,webp,html,json,bin}   # content-addressed obs snapshots & screenshots
└── media/
    ├── <track>.mp4        # optional on-demand encoded video (H.264/HEVC/AV1)
    └── <track>.kf.json    # keyframe → seq → byte-offset index
```

**`events.jsonl` row shape.** Line 1 is a `meta` header `{v, session_id, run_id, t0_wall, t0_mono, tz}`. Every subsequent row carries both an interval delta (asciicast v3 — stream-safe, small bytes, no rewrite on append) *and* a monotonic seq (so random seeks need no folding), plus a per-event schema version:

```jsonc
// header
{"kind":"meta","v":1,"session_id":"s_8f3a","run_id":"r_01","t0_wall":1748613731.412,"t0_mono":0.0,"tz":"UTC"}
// an action paired to its observation by action_id; element-ref-first (survives UI drift)
{"seq":412,"dt":0.140,"kind":"action","src":"click","v":1,"action_id":"a_412",
 "target":{"element_ref":"e_173","role":"button","name":"Save","bbox":[840,612,72,28]},
 "before_snapshot_ref":"sha1:9c2e…","idempotency_key":"ik_a412","mode":"recorded"}
{"seq":413,"dt":0.031,"kind":"observation","src":"a11y_delta","v":1,"action_id":"a_412",
 "after_snapshot_ref":"sha1:b71f…","adds":[…],"removes":[…],"attributes":[…]}
// a decision (OTel-GenAI) — seed + response.id + tool.call.id make it a REPLAYABLE input
{"seq":414,"dt":0.880,"kind":"decision","src":"inference","v":1,
 "gen_ai.request.model":"<model>","gen_ai.request.seed":7,"gen_ai.response.id":"resp_…",
 "gen_ai.usage.input_tokens":4210,"gen_ai.usage.output_tokens":96,
 "content_ref":"sha1:…","mode":"recorded"}
// a permission gate — first-class, marker-worthy
{"seq":415,"dt":0.005,"kind":"permission","src":"prompt","v":1,
 "capability":"fs.scope","risk_tier":"ask","decision":"granted","by":"reviewer:lmei","marker":true}
```

Design choices and the pitfalls they avoid:

- **Two-level discriminated envelope** (`kind` + `src`) makes channels filterable and the schema extensible without breaking parsers — the proven ~25-subtype granularity from rrweb (8 `EventType` × ~17 `IncrementalSource`).
- **Interval `dt` + monotonic `seq` + dual anchors.** Pure interval timing (asciicast) breaks random seek; absolute ms on every row (rrweb) wastes bytes — so we keep both `dt` and `seq` and reconstruct absolute time as `t0 + prefix-sum(dt)`, with `index.json` giving O(log n) seeks. Recording *both* a wall and a monotonic anchor (Playwright `wallTime`+`monotonicTime`) keeps video and event-log aligned over long sessions.
- **Out-of-band, content-addressed media.** Screenshots/a11y snapshots live in `resources/<sha1>` (40-hex SHA-1, Playwright-style), deduped automatically; the log stays small and grep-able. Inlining media as DB BLOBs (the OpenAdapt SQLite anti-pattern) or per-row base64 is rejected.
- **Element-ref-first targeting, not raw pixels.** Actions carry semantic locators (role/name/element-ref) so replay and branching survive layout/resolution drift; raw `x,y` appears only at the pixel rung ([D3](../docs/05-tech-decisions.md)). This is the OpenAdapt `element_state` lesson made portable.
- **Decisions as recorded inputs.** Storing `gen_ai.request.seed` + `gen_ai.response.id` + `tool.call.id` is precisely what makes a model decision a *replayable* recorded input and lets a branch flip to live re-inference (§4). Raw prompt/response bodies are gated behind an opt-in capture flag (otherwise a `content_ref` SHA-1); this encodes the privacy/size tradeoff as a first-class flag and keeps PII out by default.
- **Versioning + upcasting.** `manifest.schema_version` plus a per-event `v` field, with upcasters, so a v1 recording still opens in a v3 viewer — the classic event-sourcing discipline. `kind=meta` carries mid-stream changes (e.g., a resolution change, asciicast's `r`).
- **Optional video is an overlay, not the record.** The default capture is the structured observation channel (~150× cheaper than office H.264 video, vendor-published, unverified; see [streaming-bandwidth.md](streaming-bandwidth.md)). When pixels are truly needed, encode on demand and key the track to the same clock via `media/<track>.kf.json` (NVENC H.264/HEVC/AV1 on the optional GPU tier, [D4/D11](../docs/05-tech-decisions.md)).

**Importers, not lock-in.** Because the design *supersets* the prior art, we ship adapters that ingest existing corpora into `.skn`: OSWorld `traj.jsonl` + `recording.mp4` (numeric timestamps, sub-step events, snapshots, and a version field are added; the `action`/`response`/`reward`/`done`/screenshot fields map losslessly), Playwright `trace.zip`, asciicast, and rrweb. A team with a backlog of OSWorld traces can open them in the Shinken panel on day one.

---

## 4. Branching via copy-on-write fork

The central finding from the fast-fork survey is that **fast snapshot-fork is now a commodity** — the hard part is not the fork but (a) the data model pinning every snapshot to an exact event-log position, and (b) the divergence/uniqueness problem when many clones share identity. The mechanism is uniform across substrates: snapshot once (a memory image + device/VM-state file + disk), then restore many times by `mmap`-ing the memory file `MAP_PRIVATE` (or using a qcow2 backing file) so every clone shares unwritten pages copy-on-write and only diverging pages cost per-branch RAM/disk.

| Substrate | Fork/restore primitive | Vendor-published figures (unverified) |
|-----------|------------------------|----------------------------------------|
| **Firecracker microVM** | `MAP_PRIVATE` CoW restore + diff snapshots; warm-parent pool | restore 5–30 ms (28 ms measured warm); ~50 clones share most pages; sub-millisecond CoW forks reported (~0.79 ms) |
| **qcow2 backing-file (QEMU/KVM)** | redirect-on-write overlay tree over a read-only base | heavier; the on-disk overlay tree *is* the disk-side fork DAG; supports full GUI guests |
| **gVisor `runsc` / demand-paged restore** | restore = a new sandbox from a snapshot; lazy `--background` page-in | starts before full memory is resident; 3–10× faster cold start |
| **CRIU (+cuda-checkpoint)** | `fork()`-based process-tree restore; pre-dump incrementals | Linux-only; GPU C/R is x64-only, driver 550+, no UVM/MIG/MPS |
| **Commercial fast-fork platforms** | live-VM branch + CoW page sharing | branch/restore <250 ms; resume-from-standby <25 ms; ~0.12 MiB private RAM per child; "near-zero" per-branch storage |

**A branch is a bisected CoW operation.** Forking the world at step N means restoring the **env half** by CoW-forking the substrate snapshot *and* the **agent half** by deserializing the orchestrator state, both anchored to the same `seq`. The closest published analog to "fork the world and re-run down a different path" snapshots the *complete* orchestrator state (dialogue history, memory, tool DB, RNG seed, step/token counters) immediately before a chosen junction and records parent→child relationships in an evaluation tree — yielding a genuine counterfactual from a *mathematically identical* state, not an approximate prefix replay ([DIVERT](https://arxiv.org/html/2604.21480)). Crucially, that work avoids Python pickle's fragility lesson: Shinken serializes the agent half to a **versioned, language-neutral schema** with upcasters, never a framework-coupled pickle.

```
              ●  c0  (root snapshot @ seq 0)
              │
              ●  c1  @ step 4  (permission gate)
              │
              ●  c2  @ step 9  ← scrub here, fork
             ╱ ╲
   (original) ●   ● c3  branch: swap model / edit one action / flip seed
    c2a       │   │
              ●   ● c3a  re-run from c2 down a new path
                   │
                   ●  …branches MAY re-converge (DAG, not just tree)
```

The fork tree is an **immutable, git-style parent-pointer DAG** (Merkle-DAG semantics): each node `{checkpoint_id, parent_checkpoint_id[], branch_id, snapshot_id, created_from_step_id}`. A branch is a new child; the original `events.jsonl`/snapshots are **never mutated**; content-addressing dedups identical sub-states; and because a node can have multiple parents, branches may re-converge. This mirrors [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence)'s `update_state` (new `checkpoint_id`, `parent_config` pointer) and the conversation-branching pattern in modern agent CLIs (`/fork`, `/rewind <checkpoint>`).

**Per-event mode flag.** Every model/tool result is recorded with a mode `∈ {recorded | live | mock}`. *Pure replay* (all `recorded`) MUST reproduce the original bit-for-bit — we make this a CI determinism test for the harness itself. A *counterfactual branch* = restore at `seq`, then set the chosen events (and everything downstream) to `live` with an alternate model/prompt/seed; everything *before* the fork stays `recorded` and is never recomputed.

**Two hazards the design must close** (both flagged repeatedly in the research and unaddressed by the deterministic-replay blogs):

1. **Clone uniqueness.** Many CoW clones of one snapshot share RNG state, entropy pool, VMGenID, cached tokens, and the recorded model responses. Without reseeding, sibling branches are accidental identical twins, confounding both security and counterfactual validity. On every fork we drive **VMGenID** so the guest kernel reseeds its CSPRNG ([Firecracker random-for-clones](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/random-for-clones.md)), surface VMClock generation counters, rotate the MAC/hostname/boot-id, and rotate the agent RNG seed — and we always fork clones *from the snapshot* rather than running base and clone off live state. This is the same post-fork uniqueness hook as [D1](../docs/05-tech-decisions.md)'s instant-reset path; reset and branch are the same operation.
2. **Side effects on a live branch.** Re-running a side-effecting tool call (email, payment, DB write) on a live branch double-fires it unless gated. We classify each tool as `pure | idempotent(with key) | side-effecting`; on replay we default to `mock` (return the recorded result by `tool.call.id`); on a live branch, side-effecting calls route through a record/mock proxy by default, and real-world effects require explicit opt-in plus an `idempotency_key` (carried on every `action.tool_call`, with a `side_effect_cursor` in the snapshot). This aligns with the `tool_runner` policy boundary ([D2/D6](../docs/05-tech-decisions.md)).

**Cross-platform reality.** Sub-second live memory-fork is essentially a Linux/microVM story; Windows/macOS guests and the GPU tier generally cannot be live-forked ([D1/D11](../docs/05-tech-decisions.md)). So branching degrades gracefully into **two tiers**: (a) full memory-fork on the Linux fork tier for true mid-execution branching; (b) disk-snapshot + deterministic event-replay on Windows/macOS and GPU guests, where the structured action/observation log is re-issued against a restored disk image. Because Shinken records structured, element-ref-first events regardless of OS, tier (b) is feasible everywhere — and the fork/branch API is *uniform* above the substrate. We also heed the snapshot-chain compaction lesson (deep CoW overlay chains regress fork latency): periodically flatten chains so deep replay trees don't degrade.

**Economic payoff for eval.** Because all branches from step N share the identical pre-fork prefix, branch rollouts overlap heavily in exact tokens (34.8–58.4% vs ~0.5% for independent rollouts; DIVERT, vendor-published, unverified) — a KV-cache-aware serving layer amortizes the common prefix across the whole fork tree, so counterfactual A/B of many agents from one state is cheap rather than N full rollouts. This is the substrate for [D7](../docs/05-tech-decisions.md)'s "N≥5 CoW-forked replicas → pass@k/pass^k."

---

## 5. The replay panel UX

The replay panel is the read/scrub *face of the live control plane* — the same client-side viewer over the same `.skn` bundle, in two modes. The reference tools converge on a single copyable layout, and the panel adopts it almost verbatim while adding the agent-specific pieces none of them ship.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  FILMSTRIP  ▸ per-step thumbnails (hover to magnify)                        │
├──────────────┬────────────────────────────────────┬────────────────────────┤
│ STEP LIST    │       RECONSTRUCTION STAGE          │  INSPECTOR (tabs)       │
│ (T-A-O)      │  pluggable renderer:                │  • Snapshot (before/    │
│ □ step 7  ⚠  │   (a) on-demand video               │    action/after)        │
│ □ step 8     │   (b) rebuilt a11y / "what agent saw"│  • Decision (CoT, seed) │
│ ▣ step 9  🔒 │   (c) terminal cast                 │  • Agent state (diff)   │
│ □ step 10    │  acted-on element highlighted (pink)│  • Console / Network    │
├──────────────┴────────────────────────────────────┴────────────────────────┤
│ TIMELINE (master clock): multi-track lanes on one x-axis                    │
│  steps   ▕──▎────▎──▎─────▎──▎  tool-calls  ▕─▎──▎───▎                       │
│  decisions ▕─▎────▎───▎       permissions  🔒req  ✓grant  ✗deny  ⬆escalate   │
│  network ▕▬▬ status-colored duration bars   video-keyframes ▏ ▏ ▏ ▏         │
└───────────────────────────────────────────────────────────────────────────┘
```

The mechanics, each grounded in a production reference:

- **One master clock.** The event log's monotonic seq is the single timeline; every panel binds to a generalized `goto(logicalClock)` / `onTimeUpdate` engine — the [rrweb-player](https://github.com/rrweb-io/rrweb/blob/master/packages/rrweb-player/README.md) controller contract (`play/pause/setSpeed/toggleSkipInactive`) lifted across all panels. Skip-inactivity and variable speed are first-class to collapse idle "thinking" gaps.
- **Four-zone layout** (top filmstrip / left step list / center stage / tabbed inspector), cloned ~1:1 from [Playwright Trace Viewer](https://playwright.dev/docs/trace-viewer). The step list rows are **Thought-Action-Observation triples** (the unit proven by computer-use trajectory viewers such as [Cua's](https://cua.ai/blog/trajectory-viewer)), each with a status icon, latency whisker, and a tool/permission badge. **Before/Action/After** snapshots become *pre-observation / action / post-observation* per step.
- **Pluggable center stage.** Default to the lightest faithful layer — the rebuilt accessibility-tree / "what the agent saw" view (computed roles, like a browser DevTools a11y pane) — and let the reviewer flip to the on-demand video segment only when pixels matter, or a terminal cast for CLI steps. The acted-on element is highlighted (pink) exactly as Playwright pinks the action target. The stage is *labeled* with which layer is shown, because the rebuilt tree can drift from what a vision model actually consumed.
- **Multi-track timeline** on one x-axis, the [Chrome DevTools Performance panel](https://developer.chrome.com/docs/devtools/performance/reference) model: lanes for steps, tool calls, decisions, **permission/approval events**, console, network (status-colored duration bars), and video keyframes. The same lanes the live panel streams are the lanes replay scrubs. Breadcrumb nested zoom + WASD/trackpad pan + `Cmd/Ctrl-F` regex search handle very long sessions; latency whiskers split model-think vs tool-exec vs env-apply time so slow steps pop.
- **Permission events are the highest-priority marker class** — Shinken's analog of Sentry's errors. Distinct glyphs for requested/granted/denied/auto-approved/escalated; each is click-seekable and opens an inspector card with the request, the [Cedar](../docs/05-tech-decisions.md) policy decision, who/what approved, and the resulting capability unlock ([D6](../docs/05-tech-decisions.md) ↔ D5). Secondary markers: tool-call failures, retries/loops, errors.
- **Bidirectional seek + auto-scroll.** Clicking any inspector row (network/console/permission/tool call/step) seeks the player; the advancing player auto-scrolls the step list (with a "follow playhead" affordance that pauses on manual scroll so it doesn't fight the user) — the [Sentry](https://docs.sentry.io/product/explore/session-replay/replay-details/)/[LogRocket](https://docs.logrocket.com/docs/session-replay) pattern.
- **Agent-state inspector.** At any scrub point, show the agent's messages/scratchpad/plan/seed and the diff vs the previous step — the agent analog of LogRocket's Redux time-travel. Paired with an env-state summary from the snapshot, this is *also the launch point for "re-run from here"* (§4): **replay and branching are the same UI.**
- **Cross-session faceted search.** Above single-run replay, index every bundle by task type, model/agent version, environment image, tools invoked, permissions requested/denied, outcome/reward, error class, and duration — *plus full-text over reasoning and observations* (so a reviewer can find "the run where the agent tried to delete a file," not just match on metadata). Saved cohorts open one-click into replay at the matching step. The [FullStory](https://www.fullstory.com/blog/session-replay/)/Rollbar faceting model, agent-flavored.
- **Two-run diff.** Synced dual timelines + auto-trace-tracking (step the left run, the aligned step in the right advances) + a structured per-step diff of observation/action/decision — the [LangSmith comparison view](https://changelog.langchain.com/announcements/enhanced-trace-comparison-view-with-auto-trace-tracking-and-diff-viewer). Diverging runs (different step counts) align on stable semantic anchors (same tool call, same permission event, same screen), with the divergence point marked. A side-by-side/slider **image diff** (Playwright) is the secondary, reviewer-invoked check — never the primary signal, because pixel diffs are noisy (cursor, timestamps, animation). True counterfactual A/B uses a *fork from a shared snapshot* (§4), not two independent runs whose difference is confounded by plain model nondeterminism.
- **Triage entry + sharing.** Default the entry point to the first failure / denied-permission / divergence (the "start-from-the-failure, scrub backward" workflow), with the timeline pre-scrolled there. Every scrub position is a share-at-timestamp deep link; reviewers drop timeline annotations that travel with the bundle.
- **Privacy + offline.** The viewer is fully client-side over one self-contained bundle (the Playwright offline-viewer / Cua data-stays-local precedent): no server round-trip, strong privacy posture for sensitive sessions. Observations/console/network are **default-masked in the viewer** (in addition to capture-time masking), with explicit unmask + audit. The viewer version-detects and upcasts so old bundles don't render blank.

Two pitfalls the panel explicitly designs around: a per-step slider alone is too coarse (a single step can span seconds of OS activity) — the slider is always backed by the continuous logical-clock timeline; and seeking late into a long run must *not* fold the whole history — the viewer seeks to nearest snapshot + applies the tail, so scrub latency is bounded by snapshot cadence, not session length.

---

## 6. Replay as training data for CUA model and eval teams

The wedge for first adoption ([D12](../docs/05-tech-decisions.md)) is that **`.skn` is, by construction, RL/SFT trajectory data and an eval artifact** — the same recording that a human scrubs is the dataset a model team trains on and the golden trace an eval team regresses against. This is deliberately generic: it serves any team building or evaluating computer-use agents (model labs, RPA builders, researchers), not a specific customer.

**What `.skn` already contains that training needs.** The decision channel carries the four RL/eval essentials per step that benchmark dumps capture (action, raw response, reward, done) — superset of the OSWorld `traj.jsonl` record — but with sub-step events, numeric timestamps, snapshots, and a version field that the flat per-step format lacks. The observation channel carries element-ref-first locators and a11y trees, the structured ground truth that prior CUA data pipelines synchronize screen+input+a11y to produce state-action-CoT corpora. Because decisions store seed/response-id/token-usage and tools store call-id/args/result, a trajectory is directly consumable for:

| Consumer | Uses `.skn` as | Mechanism |
|----------|----------------|-----------|
| **SFT / behavior cloning** | (observation → action) pairs with reasoning | Decision channel CoT + element-ref-first action targets; OpenAdapt-style offline reducer collapses raw events into compact semantic actions |
| **RL / preference data** | (state, action, reward) tuples + branch outcomes | Per-step reward/done in the decision channel; branch DAG gives counterfactual (state, action_A vs action_B, outcome) triples from an *identical* fork point |
| **Eval / regression** | golden trace per task | Replay a `.skn` with all events `recorded` ⇒ deterministic re-run as a CI determinism check; verifier DAG ([D7](../docs/05-tech-decisions.md)) grades the post-action observation |
| **Failure mining** | searchable corpus of failures | Cross-session faceted search (§5): "all runs that requested `fs.scope` then failed" → labeling/dataset queue |
| **Model A/B** | counterfactual fork experiments | Fork at a junction, flip the decision to `live` with an alternate model/seed, diff outcomes — KV-cache prefix reuse makes fan-out cheap (§4) |

**Why fork-based eval beats re-run-twice.** A pivotal correctness point: comparing two independently generated runs is *descriptive, not causal* — their difference is partly plain model nondeterminism, so attributing it to an intervention is confounded. Only a fork from a *shared, pinned* snapshot isolates the intervention. So Shinken's eval forks (§4), it does not re-run twice; this is what lets a model team say "swapping the planner prompt changed the outcome" with a clean causal claim. The branch DAG plus the per-event mode flag turns the replay store into a counterfactual generator, not just an archive.

**Labeling and curation loop.** The replay panel doubles as a labeling tool: reviewer annotations travel with the bundle, the start-from-failure triage entry surfaces the interesting steps, and saved cohorts become datasets. Capture-time + viewer-time masking keep credentials/PII out of the training corpus by default (opt-in raw-content capture for the decision channel), so a `.skn` corpus is shareable without leaking secrets — secrets are brokered and never appear in plaintext ([D6](../docs/05-tech-decisions.md)). The same conformance suites the eval layer ships (OSWorld-Verified, WindowsAgentArena, AndroidWorld, WebArena family — [eval-benchmarks.md](eval-benchmarks.md)) emit `.skn` bundles, so eval output *is* training input, closing the loop between [D5](../docs/05-tech-decisions.md) and [D7](../docs/05-tech-decisions.md).

---

## 7. Reconciliation to D5 (and the decisions it touches)

| Claim in this note | Decision | Reconciliation |
|--------------------|----------|----------------|
| No bit-deterministic full-desktop replay; pragmatic state-snapshot + event-log + observation-log | **D5** | Exactly D5: "NOT bit-deterministic." Determinism is scoped to the seeded agent core; the desktop is a restartable starting point. |
| The event stream IS the replay log | **D5 ↔ D4** | The reliable-ordered data channel (D4 event plane) is persisted byte-for-byte as `events.jsonl`; one serializer, no live/replay divergence. |
| `.skn` = ZIP + manifest + `events.jsonl` (`kind` two-level envelope) + checkpoint DAG + content-addressed media | **D5** | Matches D5's `.skn` specification verbatim (Playwright-trace model, OTel-GenAI decision channel, `action_id` action→observation pairing). |
| Bisected snapshot at semantic step boundaries; O(nearest snapshot + tail) scrub/fork | **D5** | D5's "state-snapshot + event-log + observation-log"; cadence tied to step/permission/side-effect boundaries. |
| Branch = CoW-fork env snapshot + deserialize agent checkpoint → re-run from step N | **D5 ↔ D1** | D5's branch definition; the fork primitive is D1's instant-reset primitive (MAP_PRIVATE CoW + warm parent + uniqueness reseed). |
| Per-fork uniqueness reseed (VMGenID/RNG/MAC) | **D1, D5** | Same post-fork uniqueness hook as D1's reset path. |
| Permission events first-class; highest-priority markers | **D6 ↔ D5** | D6 states approvals/denials are first-class replay events; the panel makes them the top marker class. |
| Element-ref-first actions; structured-first observations; pixels on demand | **D3** | Replay-stability is a stated D3 benefit; raw `x,y` only at the pixel rung. |
| Optional NVENC video overlay, not the primary record | **D4, D11** | Structured channel is the default (~150× cheaper, unverified); video is on-demand, GPU-tier-accelerated, never on encode-incapable datacenter GPUs (D11). |
| Side-effect mock/idempotency on live branches via the `tool_runner` boundary | **D2, D6** | Code-as-action and side-effecting tools route through the controlled boundary; idempotency keys + record/mock proxy. |
| Replay = RL/SFT training data; fork-based causal eval | **D5, D7, D12** | D5/D12's adoption wedge; D7's N≥5 CoW-forked replicas → pass@k/pass^k rides the same fork primitive. |
| Cross-platform graceful degradation (memory-fork on Linux; disk-snapshot + event-replay elsewhere) | **D10, D1** | One uniform replay/branch API; per-OS handler-factory beneath; structured events make tier-(b) feasible on all guests. |

**Open questions carried forward** (see [open-questions.md](open-questions.md)): a11y-tree fidelity on Electron/Qt/canvas/games is the load-bearing unverified assumption for the structured observation channel and therefore for replay stability — it needs a measurement spike; macOS/Windows fast-fork is largely infeasible today, so tier-(b) event-replay quality must be measured, not assumed; the protocol/event-schema versioning + upcasting path must be specified and tested before the first long-lived corpus accrues; and snapshot-chain compaction policy (flatten cadence vs storage) needs a first-party benchmark. No first-party perf numbers exist yet — every fork/restore/overlap figure here is **(vendor-published, unverified)** and a first-party measurement plan is a prerequisite to committing the cadence and concurrency defaults.

---

*Sources for this note are consolidated in [sources.md](sources.md) §3–§4 (replay, branching, deterministic record/replay, streaming) and §1–§2 (runtimes, fast-fork infrastructure). External references are cited inline by URL; sibling docs by relative path.*
