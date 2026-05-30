# Eval & Benchmarks

> Working note feeding `docs/`. The authoritative decision is **D7** (eval layer = thin orchestration on the runtime, inverting OSWorld); it also touches **D1** (fast fork), **D5** (`.skn` replay), **D8** (SDK/MCP facade), **D9** (control plane), **D12** (positioning).
> Opinionated and citation-heavy; dated **2026-05-30**. Every speed / density / cost figure is **vendor-published, unverified** unless explicitly marked first-party.
> Siblings: [replay.md](replay.md) (D5, `.skn` event-sourced replay), [ai-native-interface.md](ai-native-interface.md) (D2/D3 ACI + observation rungs), [sandbox-infra.md](sandbox-infra.md) (D1 fork tier), [permissions.md](permissions.md) (D6 capability gates), [osworld-teardown.md](osworld-teardown.md) (the baseline we invert), [open-questions.md](open-questions.md) (unverified assumptions). Sibling docs: [`../docs/03-osworld-analysis.md`](../docs/03-osworld-analysis.md), [`../docs/05-tech-decisions.md`](../docs/05-tech-decisions.md), [`../docs/07-glossary.md`](../docs/07-glossary.md).

Shinken's eval layer is **not a benchmark of its own**. It is thin orchestration on the *same* Sandbox runtime that serves production agents, and that inversion is the entire thesis (**D7**). The prior generation of computer-use (CUA) evaluation is two messes stacked on top of each other: (1) the *scores* are saturating and increasingly vendor-self-reported, so the headline number tells you less every quarter; and (2) the *harnesses* that produce those scores are brittle, slow, and stringly-typed — the original OSWorld shipped with 300+ task/grader bugs and re-downloaded gold artifacts at grade time with no checksum. Shinken keeps OSWorld's one good idea — the declarative *task-as-data* unit plus the getter/metric split — and inverts everything else: a **typed verifier DAG** instead of `getattr`, a **golden snapshot per task**, **N≥5 copy-on-write-forked replicas** reporting **pass@k / pass^k** with confidence intervals, and **readiness probes** instead of fixed sleeps. Cheap fork (**D1**) is what makes statistically honest eval affordable; event-sourced replay (**D5**) is what makes re-grading and counterfactual debugging possible offline.

The first adopters are **CUA model and eval teams** — labs that build and benchmark computer-use agents, acquire OSWorld-class datasets, and need a pinned, traced, parallel harness whose trajectories double as RL/SFT training data (**D12**). This note covers the benchmark landscape and dated SOTA, the three grading paradigms, the eval-harness design, the lessons from the two closest comparables (HUD and cua-bench), and how all of it reconciles to D7.

---

## 1. The benchmark landscape (mid-2026)

The space splits cleanly by modality. Shinken ships the major benchmarks as **built-in conformance suites with task + grader + environment versioned together** (D7), because none of them are reproducible out of the box without that pinning.

| Benchmark | Modality | Tasks | Env / scoring | Why it matters to Shinken |
|---|---|---:|---|---|
| **OSWorld-Verified** | Desktop (Ubuntu) | 369 (361 ex-GDrive) | Real VM; 134 execution graders | The canonical desktop bar (D7); the schema we invert |
| **WindowsAgentArena (WAA)** | Desktop (Windows) | ~154 | Docker/Azure; OSWorld-style state graders | The Windows leg; **least-saturated** major desktop bench |
| **AndroidWorld** | Mobile | 116 (20 apps) | Live emulator; state reward, parameterized | The mobile leg (Android = roadmap, D1); **saturated** |
| **WebArena** | Web | 812 | Self-hosted sites; element-ID, functional checks | Reproducible-snapshot + element-ID pattern (D3) |
| **VisualWebArena** | Web (visual) | 910 | Self-hosted; Set-of-Marks | The SoM observation pattern (D3 Rung 1) |
| **WebVoyager** | Web (live) | 643 (15 sites) | Live sites; LLM-as-judge (~85% human agr.) | LLM-judge pattern; drift cautionary tale |
| **Mind2Web v1** | Web (offline) | 2,350 | Static HTML snapshots; action-match | Deterministic fast regression suite |
| **Mind2Web-2** | Agentic search | 130 | Live web; **Agent-as-a-Judge** (~99% agr.) | Best-in-class rubric judge methodology |
| **Online-Mind2Web / WebJudge** | Web (live) | 300 (136 sites) | Live; open WebJudge-7B (~87% agr.) | "Illusion of progress" — methodology drives scores |
| **TheAgentCompany** | Enterprise / long-horizon | 175 | Self-hosted GitLab+Plane+RocketChat+ownCloud; checkpoint partial-credit | The frontier with most headroom; multi-service envs |
| **GAIA** | Tool-use assistant | 466 (300 private) | Exact-match; private test set | Proof scaffolding ≈ 30 absolute points |
| **SheetBench-50** | Spreadsheet | 50 | Hosted; gold ANSWER-sheet exact-match | A common second conformance bench |

*Sources: OSWorld ([arXiv 2404.07972](https://arxiv.org/abs/2404.07972)), OSWorld-Verified ([xlang.ai/blog/osworld-verified](https://xlang.ai/blog/osworld-verified)), WAA ([arXiv 2409.08264](https://arxiv.org/abs/2409.08264)), AndroidWorld ([arXiv 2405.14573](https://arxiv.org/abs/2405.14573)), WebArena, VisualWebArena ([arXiv 2401.13649](https://arxiv.org/abs/2401.13649)), WebVoyager ([arXiv 2401.13919](https://arxiv.org/abs/2401.13919)), Mind2Web ([osu-nlp-group site](https://osu-nlp-group.github.io/Mind2Web/)), Mind2Web-2 ([arXiv 2506.21506](https://arxiv.org/abs/2506.21506)), Online-Mind2Web ([GitHub](https://github.com/OSU-NLP-Group/Online-Mind2Web)), TheAgentCompany ([arXiv 2412.14161v2](https://arxiv.org/html/2412.14161v2)), GAIA ([arXiv 2311.12983](https://arxiv.org/pdf/2311.12983)).*

The taxonomy that matters for design is **what each benchmark grades against**, not what it tests. The closest fit to Shinken is the **desktop, real-OS, execution-graded** tier (OSWorld-Verified, WAA) — JSON tasks against a real VM, scripted checkers reading the *actual end state*; this is the infra pain the fork tier attacks. **Mobile state-reward** (AndroidWorld) is a good deterministic-teardown pattern, but its touch/gesture action space does not map onto the synchronous desktop pointer loop (a note for the Android roadmap, D1/D10). **Web self-hosted element-ID** (WebArena, VisualWebArena) is the reproducible-snapshot pattern that validates structured-first observation (D3); **web live, judged** (WebVoyager, Online-/Mind2Web-2) is the drift cautionary tale — pin/precache or do not compare across dates. **Enterprise / long-horizon** (TheAgentCompany's four self-hosted services with checkpoint partial credit; GAIA) is the unsaturated frontier.

### Three grading paradigms (Shinken supports all three, composable)

1. **Execution / state-based** — scripted checkers verify the actual end state (OSWorld's 134 eval functions, WAA, AndroidWorld state-reward, WebArena functional checks, TheAgentCompany checkpoints). **Reproducible but brittle** to UI/site drift and historically full of grader bugs. This is Shinken's *primary* paradigm.
2. **LLM / VLM-as-judge over trajectories** — flexible for open-ended goals but gameable and non-deterministic (the WebVoyager GPT-4V judge reports ~85% human agreement; open WebJudge-7B ~87%). Shinken uses this only as a **constrained fallback** (D7), never as the sole grader for a state-checkable task.
3. **Agent-as-a-Judge with tree-structured rubrics** — Mind2Web-2's per-task judge agents reach **~99% human agreement** while scoring answer correctness *and* source attribution ([arXiv 2506.21506](https://arxiv.org/abs/2506.21506)). This is the methodology to adopt for genuinely open-ended grading; always report judge-vs-human agreement and never trust a single judge.

A fourth mode, **offline action-match** (Mind2Web v1: element accuracy + operation F1 + step/task success on static HTML snapshots), is fully deterministic but does not test execution, recovery, or exploration — useful only as a fast regression tripwire.

The load-bearing empirical finding: **programmatic verifiers decisively beat LLM judges on state-checkable tasks.** OpenComputer ([arXiv 2605.19769](https://arxiv.org/html/2605.19769v1)) measured **94.1% human agreement for programmatic verifiers versus 79.2% for an LLM-as-judge.** Screenshots and judges miss subtle state errors a checker catches — two tokens written into one spreadsheet cell versus two cells looks identical from pixels. Hence D7's ordering: **programmatic-primary, model-verifier-as-fallback**, with the hard rule that a model-verifier may never silently override a programmatic FAIL.

---

## 2. Current SOTA (dated 2026-05-30) — and what the scores hide

Computer-use on OSWorld climbed from **~12.24%** (best 2024 model) to **above the 72.36% human baseline** in roughly eighteen months. Several benchmarks are now saturating, which means **Shinken should route users toward the unsaturated frontiers** and treat saturated benches as smoke tests, not differentiators.

**OSWorld-Verified leaderboard** ([llm-stats](https://llm-stats.com/benchmarks/osworld-verified) snapshot, 2026-05-30; all vendor/leaderboard-published, unverified):

| Model | Score | Notes |
|---|---:|---|
| Claude Opus 4.8 | **83.4%** | Top; well above human 72.36% |
| Claude Mythos Preview | 79.6% | |
| GPT-5.5 | 78.7% | |
| Gemini 3.5 Flash | 78.4% | |
| GPT-5.4 | 75.0% | *self-reported; not independently reproduced* |
| Claude Opus 4.6 | 72.7% | best *independently verified* in early 2026 |
| GPT-5.3 Codex | 64.7% | |
| Qwen3.6 Plus | 62.5% | best-open frontier |

Trailing the leaders: Kimi K2.6 73.1%, GPT-5.4 mini 72.1%, and open-weight Holo3-35B-A3B 82.6% / Holo3-122B-A10B 78.8% ([BenchLM, 2026-04-29](https://benchlm.ai/benchmarks/osWorldVerified)) — the open frontier is now within a few points of the closed leaders.

Other modalities, dated:

| Benchmark | SOTA (2025-2026) | Reading |
|---|---|---|
| **WindowsAgentArena** | Agent S3 56.6% (with bBoN) / 50.2% solo; Agent S2 29.8%; Navi baseline 19.5%; human 74.5% | Hardest major desktop bench; the right place to spend eval budget |
| **AndroidWorld** | Minitap mobile-use claims **100%** (Feb 2026, +20pts over human); AGI-0 97.4% multi-agent; AutoGLM-Mobile 80.2% | Effectively solved — no longer differentiates top models |
| **WebArena** | OpAgent 71.6% (Jan 2026); Claude Mythos Preview ~68.7% (May 2026) | Mid-saturation |
| **WebVoyager** | Surfer 2 97.1% (Oct 2025); Magnitude 93.9%; Surfer-H 92.2% at **$0.13/task** | Saturated; scores non-comparable across harnesses |
| **TheAgentCompany** | Gemini 2.5 Pro 30.3% full / 39.3% partial, ~27 steps, ~$4.2/task; open-weights ≤7.4% | The most headroom; cost/step matters as much as success |
| **GAIA** | Claude Sonnet 4.5 on Princeton HAL scaffold 74.6% (Anthropic sweeps top six, 2026); OpenAI Deep Research 72.57% val | Scaffold ≈ 30 absolute points |

*Sources: [llm-stats OSWorld-Verified](https://llm-stats.com/benchmarks/osworld-verified), Agent S3 ([arXiv 2510.02250](https://arxiv.org/html/2510.02250v1)), AndroidWorld ([leaderboard](https://google-research.github.io/android_world/); 100% claim [arXiv 2602.07787](https://arxiv.org/pdf/2602.07787)), OpAgent on WebArena ([arXiv 2602.13559](https://arxiv.org/pdf/2602.13559)), WebVoyager ([Magnitude](https://github.com/magnitudedev/webvoyager), [Aime Browser-Use](https://aime-browser-use.github.io/)), TheAgentCompany ([arXiv 2412.14161v2](https://arxiv.org/html/2412.14161v2)), GAIA ([Princeton HAL](https://hal.cs.princeton.edu/gaia)).*

### What the headline number hides

A ~72% OSWorld score means **~1 in 4 tasks still fails**, and the failure surface is exactly what office-task benchmarks (~10-15 steps) are too short to expose. Three findings from the deep efficiency/failure literature drive Shinken's runtime features:

- **Grounding is the silent killer.** High ScreenSpot-v2 (~93%) does *not* transfer to professional high-res UIs — ScreenSpot-Pro crushes most models (GPT-4o 0.8%, OS-Atlas-7B 18.9%; only RL-trained grounders like GTA1-7B hit 93.4%). OSWorld-Human attributes **23% of agent errors to bad grounding**, and **66% of steps in long (>50-step) failures are grounding-loop thrash** ([arXiv 2506.16042](https://arxiv.org/pdf/2506.16042)). A single bad click can burn 100 steps, **$8.47, and 27 minutes**.
- **Blind Goal-Directedness.** Across nine frontier models on BLIND-ACT, the mean BGD rate is **80.8%** — agents march toward the goal regardless of feasibility/safety; prompting only drops it to ~61% ([arXiv 2510.01670](https://arxiv.org/pdf/2510.01670)). The best (Claude Opus 4 63.3%, Sonnet 4 65.5%) still fail to detect infeasibility a fifth of the time. This is a *model* limit a *runtime* can only partly gate — via the permission panel (D6) and loop detection.
- **Efficiency, not just accuracy.** Best agents take **2.7-4.3× more steps than humans**; planning + reflection + judging LLM calls are **76-96% of task latency**; step latency grows ~3× as a task lengthens; one GTA1 OSWorld task averages **$2.43** (87% on planning). The Weighted Efficiency Score (OSWorld-Human) puts a 41.4%-success agent at only 15.6% WES.

The runtime levers that lift scores are exactly Shinken's pillars: **structured-first / a11y + Set-of-Marks observation** (attacks grounding; ~4-6× token savings, D3), **loop / no-progress detection on the streaming channel** (mitigates BGD thrash, D4), **fast fork to make best-of-N affordable** (bBoN took Agent S3 62.6%→69.9% at N-fold cost, D1), **bash + text_editor alongside GUI control** (UI-TARS-2 jumped 7%→~50% on web research by escaping pixels, D2), and **permission gates wired to vendor safety primitives** (OpenAI `pending_safety_checks`, Anthropic HITL, D6). For the eval layer the consequence is sharp: **a held-constant harness means the score reflects the model, not the scaffold.** Report not just success rate but **steps-vs-human, cost/task, wall-clock, and BGD rate** — the dimensions a raw OSWorld number hides. See [ai-native-interface.md](ai-native-interface.md) and [permissions.md](permissions.md).

---

## 3. Why the harness is the product: critique of the OSWorld evaluator

OSWorld's evaluator is the de-facto standard CUA eval and the baseline Shinken inverts (full teardown in [osworld-teardown.md](osworld-teardown.md), [`../docs/03-osworld-analysis.md`](../docs/03-osworld-analysis.md)). Its one durable idea — keep it — is the **declarative task-as-data unit** (`{id, snapshot, config, evaluator{func, result, expected, conj}}`) and the clean **getter (introspect state) vs metric (pure comparison)** split, where `expected` can itself be a getter so one task handles both static-answer and golden-file grading. Discard the rest:

| OSWorld failure mode | Why it breaks | Shinken's inversion (D7) |
|---|---|---|
| Stringly-typed `getattr` resolution (`get_` + type) | A typo'd func or mismatched parallel-list length fails only at grade time | **Typed, schema-validated verifier DAG**; validate the whole spec at task-load |
| Host-side per-OS/arch path branching (Chrome getter has an admittedly-untested 4-way Win/Mac/arm/x86 branch) | Any app-version bump silently breaks grading | **Verifiers run inside the guest via the ACI** over stable channels (CDP, D-Bus, LibreOffice UNO, SQLite, FS) — one cross-OS introspection abstraction |
| `eval()` on guest/file strings; `is_utc_0` parses `timedatectl` line index 3 | A non-numeric `result.txt` silently scores 1.0 | Typed assertion nodes; no `eval()` of untrusted strings |
| Single destructive end-of-episode diff (`postconfig` runs `pkill` + force `ctrl+s` right before measuring) | Grading mutates the state it grades; no partial credit | **Read-only, idempotent verification**; normalize via API not `pkill`; milestone/checkpoint checks at step boundaries |
| Fixed `sleep(60)` after reset, `sleep(20)` before evaluate | Pure latent flake (too short = race, too long = waste) | **Readiness probes** with fail-fast retry on a fresh fork; phase-aware timeouts |
| Gold artifacts re-downloaded from a model-hub URL at grade time, no checksum | Link rot / drift breaks grading non-deterministically | **Oracle artifacts content-addressed *inside* the golden snapshot** with checksums; never fetched at grade time |
| `fuzzy_match` returns a continuous ratio but the harness treats results as binary | Partial scores averaged then summed as 0/1 | **Keep continuous scores continuous**; explicit per-check pass threshold; report score *and* binary outcome |
| `get_replay` re-emits 3 pyautogui action types, flagged fixme; `traj.jsonl` write-only | No re-grade, no counterfactual | **Event-sourced `.skn` replay** (D5): re-grade offline against fixed verifiers without re-running the agent |
| `results.json` rewritten whole on every append (O(n²)); `all_result.json` saved as `str(dict)` | Ad-hoc, fragile aggregation | **Append-only run store** keyed by `task_id / verifier_version / runtime_version / replica`; leaderboards as queries |

The crucial 2026 evidence that motivates the inversion: OSWorld-Verified fixed **300+ task/grader bugs over ~15 months** (collected by the community, executed by a ~10-person team over ~2 months), and OpenComputer's verifier-first redesign showed how much *leniency* lurks in lenient graders — an open-weight model that scored 52.3% on OSWorld dropped to **5.7%** on OpenComputer's tested verifiers. **Graders are tested artifacts, not scripts.** OpenComputer's self-evolving repair loop (diff programmatic verdict vs model reference vs human spot-check) auto-fixed **89.4% of 76 checker bugs** and lifted human agreement 85.2%→94.1%. Shinken adopts this as a routine offline batch job — because env final/milestone state is a forkable snapshot and the LLM is a *recorded input*, a fixed verifier can re-grade the entire historical corpus with no agent re-run. That is "fix-300-graders" turned from a 15-month slog into a query.

---

## 4. The eval-harness design (D7)

Shinken's eval layer is a **thin, stateless orchestration tier** over three things the production runtime already provides: fork-from-snapshot Sandboxes (D1), the typed ACI (D2/D3), and the event-sourced `.skn` replay log (D5). It schedules, verifies, and aggregates — it does not own a sandbox model or an action protocol.

```
                          ┌──────────────────────────────────────────────┐
                          │            Eval Service (stateless)            │
   taskset (pinned) ──────▶  INIT pool  ──▶  RUN pool  ──▶  VERIFY pool    │
   task+grader+env ver.   │  (IO-bound)     (inference)    (variable lat.) │
                          └───────┬───────────────┬───────────────┬───────┘
                                  │               │               │
              fork golden  ◀──────┘               │               └──────▶  verifier DAG
              snapshot (D1)                        │                         (typed, in-guest
              + uniqueness reseed                  │                          via ACI)
                                                   ▼
                                            Operator drives
                                          Sandbox via ACI (D2/D3)
                                                   │
                                                   ▼
                                          .skn replay bundle (D5)
                                          ── re-grade offline ──▶  re-runnable
                                                                   pure function
```

Five mechanisms, each answering a specific OSWorld failure:

**(a) Deterministic setup = an immutable golden snapshot per task.** Bake the init state (seeded files, app config, profile DBs, gold/oracle artifacts *with checksums*) into a Sandbox snapshot once at task-build time. Each replica forks from it. On the fork tier (D1) the target is **sub-10 ms per-replica reset** (Morph reports CoW fork P99 ~1.3 ms with ~93% pages shared; forkd ~1 ms/child — vendor-published, unverified). A **mandatory post-fork uniqueness hook** reseeds the kernel CSPRNG and userspace PRNGs, regenerates MAC/IP/hostname/boot-id, and resyncs the clock — the documented hard part of CoW forking ([Restoring Uniqueness in MicroVM Snapshots, arXiv 2102.12892](https://arxiv.org/abs/2102.12892)); skip it and any time/seed/crypto-sensitive verifier silently breaks. Grading then *reads* state and never mutates it. See [sandbox-infra.md](sandbox-infra.md).

**(b) Verification = a typed, schema-validated check DAG run inside the guest via the ACI.** A task's success spec is *data*: an ordered/weighted set of check nodes, each declaring `{channel, query, assertion, weight, optional milestone-step}`, validated at task-load (catch typos, missing args, unavailable channels) — never failing at grade time the way OSWorld's parallel-list asserts do. Verifiers are **tested, versioned, app-specific modules** (CLI subcommands emitting JSON) that introspect real state through reliable channels (CDP, D-Bus, LibreOffice UNO, SQLite, filesystem), following OpenComputer's verifier-first model. Running them *inside* the guest over the one ACI abstraction kills OSWorld's host-side per-OS/arch path branching. Outcome-based checks test properties of the end *and milestone* state — never the agent's command sequence — so any valid solution path passes (the Terminal-Bench/Harbor lesson, [arXiv 2601.11868](https://arxiv.org/html/2601.11868v1)).

**(c) Programmatic-primary, model-verifier-as-fallback.** Programmatic checks decide every state-checkable task (94.1% vs 79.2% — do not let a model judge own those). A constrained **rubric-scoring verifier model** (reads the recorded trajectory + final screenshots, emits per-criterion scores *with rationale*) is reserved for genuinely open-ended goals or as a tie-breaker, and its rationale is always logged into the `.skn` bundle. For the hardest open-ended cases, adopt Mind2Web-2's tree-structured Agent-as-a-Judge (~99% agreement). The model-verifier **never silently overrides a programmatic FAIL**.

**(d) Flake control = N≥5 forked replicas + pass@k / pass^k + CIs.** Single-run pass@1 is a coin flip that hides **10-30 points of variance** (tau-bench's ~80% pass@1 collapses on pass@8; up to 24.9pp best-vs-worst gaps; [arXiv 2602.07150](https://arxiv.org/pdf/2602.07150), [2512.06710](https://arxiv.org/pdf/2512.06710)). Every task runs **k≥5-10** forked replicas (more for high-variance tasks); Shinken reports **Average Score** (capability), **Pass@k** (ceiling), **Pass^k** (the reliability floor — probability *all* k succeed), confidence intervals, and **Intraclass Correlation** to separate true-capability variance from noise. **Pass^k is the headline production metric**, not pass@1. Regression alerts gate on ICC-significant deltas so the team never chases within-variance noise. This is only affordable because CoW fork makes a replica nearly free — the same primitive D1 uses for instant reset and D5 uses for replay branching.

**(e) Readiness probes, not sleeps; phase-aware pipeline.** The guest exposes app-ready/quiescence signals; the harness polls with a timeout and, on timeout, fails fast and retries on a fresh fork. Following the rollout-as-a-service pattern ([ProRL, arXiv 2603.18815](https://arxiv.org/html/2603.18815v1)), the eval service runs three independently-sized worker pools — **INIT** (IO-bound boot/fork), **RUN** (inference-bound agent loop), **VERIFY** (variable-latency scoring) — with phase-aware timeouts (accumulate only during active stages), per-job cancellation, and per-stage fallback results so one stuck Sandbox cannot stall the pool. While one job verifies, another rolls out and a third boots — this is how thousands of tasks run in parallel cheaply.

### The task contract

One thin contract binds the eval layer to the runtime: **`setup_tool` (seed state) → `run` (agent loop via the Operator) → `verify` (read real state → score in [0,1] with subscores)**. This is the HUD/cua-bench shape, and adopting it verbatim means Shinken can ingest OSWorld v4-style task JSON and the HUD `{prompt, mcp_config, setup_tool, evaluate_tool}` schema as **drop-in formats** — instant ecosystem compatibility rather than a fork. The Operator (D2) is the single human-takeover seam and the only model-facing surface; the eval layer drives it through one neutral reference scaffold so the measurement is of the *model*, not scaffold quirks (the Terminus-2 / "hold the harness constant" lesson — recall GAIA's ~30 absolute points come from scaffolding alone). One neutral runner also collapses OSWorld's ~15 near-duplicate `run_single_example_*` variants into a single driver.

### Re-grade and counterfactual from the `.skn` bundle

Because the environment's final/milestone state is a forkable snapshot and the LLM responses are *recorded inputs*, grading is a **re-runnable pure function** over the immutable `.skn` bundle (D5), not a one-shot live measurement. This unlocks:

- **Offline re-grade**: a fixed/new verifier re-scores the entire historical corpus with no agent re-run — OSWorld-Verified's "fix 300 graders" as a routine batch job.
- **Verifier A/B**: run two grader versions over the same corpus and diff.
- **Counterfactual branch**: flip from recorded-input to live-inference at step N to ask "what if the model had chosen differently?" — defaulting side-effecting tool calls to mocked/recorded so a branch does not double-execute real-world effects.
- **Milestone/partial-credit**: re-run only the failed tail from a step-boundary snapshot, turning a 100-step failure into a recoverable checkpoint (TheAgentCompany-style checkpoint scoring becomes cheap).

The one discipline this demands: **deterministic replay only holds if all nondeterminism is captured** (timing, RNG, tool results, async ordering), and old recordings stay re-gradable only with **versioned events + upcasting**. Schema versioning is an explicit open item — see [replay.md](replay.md) and [open-questions.md](open-questions.md).

---

## 5. Lessons from the two closest comparables

### HUD — the direct reference for "eval-on-top-of-runtime"

HUD (hosted platform + the open-source `hud-python` SDK, MIT) is the clearest comparable. It hosts the canonical OSWorld-Verified (369) and SheetBench-50 datasets as one-line, provider-agnostic, traced, parallel evals; `run_dataset(max_concurrent=30, group_size=N)` fans tasks out and re-runs each N times for pass@k variance out of the box ([hud-evals/hud-python](https://github.com/hud-evals/hud-python), [docs.hud.ai](https://docs.hud.ai/)).

**Adopt:** (1) the **task contract** `setup_tool → run → evaluate_tool → reward[0,1]` with execution-based scoring against real env state (never string-match) — this is what makes HUD datasets portable; (2) **default, non-blocking trace recording** (a `ThreadPoolExecutor` queues spans so telemetry never stalls the agent loop), replayable as scorecards from day one — exactly Shinken's `.skn`-as-first-class-artifact pattern (D5); (3) the **LOCAL-vs-REMOTE distinction** as the concurrency primitive — only horizontally-spawnable, stateless-per-run remote sandboxes parallelize, so Shinken's runtime exposes a "spawn fresh isolated instance" API the eval layer fans out over with a bounded semaphore plus `group_size`; (4) **model-agnostic per-model native tool translation** (Anthropic `computer_20250124`, OpenAI `computer_use_preview`) so any provider runs the same eval with zero glue (D2).

**Beat / differentiate (D12):** HUD's all-roads-lead-to-`*.hud.ai` design (MCP, inference gateway, telemetry, RL API) is vendor lock-in, and a centralized inference gateway routes all model traffic, trust, and billing through one intermediary — Shinken keeps the eval layer **fully self-hostable/offline**, hosted only as an option, and **never routes the hot action/observation/media loop through MCP** (D8; the facade is a model-agnostic *task* surface only). HUD bills per env-hour (~$0.25+/env-hour, unverified) and "one container per run" cold-start plus screenshot-heavy observation dominate at high concurrency; Shinken's fork-tier cold-start and **structured/a11y-first, delta/region-encoded** observation (D3/D4) directly lower $/task. Finally, HUD leans on upstream OSWorld for desktop; Shinken's one runtime + one ACI across Linux/Windows/macOS (D10) is a real differentiation surface.

### cua-bench — the closest prior art, and a cautionary mirror

cua-bench (the "cua gym") is a Gym-style harness (`make/reset/step/evaluate`) sitting on the cua production runtime, with a self-hosted HTTP worker pool for RL rollouts ([trycua/cua](https://github.com/trycua/cua)). Its standout idea to **match**: the WindowsArena adapter ports OSWorld's declarative evaluator but moves grading **out of the guest** into async getters that call the VM over the runtime's own RPC (`run_command` / `read_bytes` / `get_accessibility_tree`) from a worker container — exactly "eval layer as a thin client of the production runtime API." Also worth matching: **provider polymorphism** (one task spec runs on a fast simulated Playwright backend for CI *or* a real Docker/QEMU VM for fidelity, selected by a `computer` config dict), the **task-as-code lifecycle with a per-task oracle solver** (the oracle is a built-in "does our own reference solution still score 1.0?" regression check), and **QCOW2 CoW overlays over a read-only golden disk** for parallel-safe reset.

The four areas where cua-bench is weak define what Shinken must **beat** — and they map one-to-one onto Shinken's pillars:

1. **No deterministic replay.** Its `tracing.py` is a write-only HuggingFace-dataset log of screenshots + JSON actions — no seed, no pre/post state hashes, not re-executable or re-gradable. The *same* gap OSWorld has. Shinken's `.skn` (D5) is the differentiator.
2. **Single end-of-episode boolean scoring.** Reward is computed only on a `Done` action; no milestones, no partial credit, no live reward. Shinken does per-step/milestone assertions.
3. **Bandwidth-naive.** Full base64 PNGs per step over HTTP, and gold artifacts re-downloaded at grade time with no checksum. Shinken diffs/delta-encodes observations and content-addresses gold inside the snapshot (D3/D4).
4. **Two incompatible evaluator paradigms.** Native tasks use expressive-but-unschematized imperative Python `@evaluate_task`; the WindowsArena path uses brittle OSWorld JSON that inherits the stringly-typed runtime-only failure modes. There is no single validated schema spanning both. Shinken defines **one typed, machine-checkable verifier schema** (declarative core + escape-hatch code + optional model-judge fallback), validated before the run.

cua-bench also runs grading with a thin permission model (arbitrary `run_command` shell in the VM, `postconfig` runs `taskkill`/execute steps) and a coarse worker pool (`MAX_ENVS=2`/server, scale by spawning many processes). Shinken adds an explicit **agent-vs-grader trust boundary** with capability scoping (D6) and a multiplexed runtime serving many sessions per process (D9). Note on positioning: HUD is the hosted eval cloud that cua's *agent* plugs into; cua-bench is the self-hosted gym. Shinken competes with the **cua-bench / HUD-runtime layer**, not the agent.

---

## 6. Reconciliation to the decisions

| Decision | How the eval layer uses it |
|---|---|
| **D7** (this note's anchor) | Typed verifier DAG; golden snapshot per task; N≥5 forked replicas → pass@k/pass^k + CIs + ICC; readiness probes; programmatic-primary + constrained model-verifier fallback; task+grader+env versioned together; ship OSWorld-Verified, WAA, AndroidWorld, WebArena/VisualWebArena/WebVoyager, Mind2Web/-2, TheAgentCompany, GAIA as conformance suites; "graders are tested artifacts." |
| **D1** (fast fork) | Each replica = a sub-10 ms CoW fork from the golden snapshot + mandatory uniqueness reseed; the *same* primitive as instant reset and replay branching. This is what makes k-trial reliability affordable. |
| **D5** (`.skn` replay) | Grading is a re-runnable pure function over the immutable bundle: offline re-grade, verifier A/B, counterfactual branch, milestone partial-credit. Replay trajectories double as RL/SFT training data — the adoption wedge. |
| **D2 / D3** (ACI + observation) | Verifiers introspect via the ACI over stable channels (one cross-OS abstraction, no host-side path branching); structured-first/a11y + Set-of-Marks observation attacks the 23%-of-errors grounding problem and cuts grading-evidence bandwidth. One neutral Operator scaffold holds the harness constant. |
| **D6** (permissions) | Explicit agent-vs-grader trust boundary; capability scoping per eval; map OpenAI `pending_safety_checks` / Anthropic HITL onto the permission panel so safety primitives work out of the box (also a partial BGD mitigation). Approvals/denials are first-class replay events. |
| **D8** (interfaces) | The eval task contract rides the native SDK; the MCP facade is the model-agnostic *task* surface only — never the hot action/observation/media loop. |
| **D9** (control plane) | The eval service is a stateless INIT/RUN/VERIFY pipeline over the Fleet Manager's warm fork pool; the Action Gateway meters cost; Sandbox health is a circuit-breakable dependency. |
| **D12** (positioning) | Self-hostable eval + runtime (vs HUD's hosted lock-in); first adopters are CUA model/eval teams whose `.skn` trajectories are RL/SFT data. |

### What Shinken must measure first-party (open items)

Every speed/cost number above is **vendor-published and unverified**. Before any of this is load-bearing, Shinken needs a first-party measurement plan for: (1) actual per-replica fork+reseed latency on each substrate tier; (2) verifier-vs-human agreement on Shinken's own verifier DAG (target OpenComputer's 94.1%) plus a continuous calibration/auto-repair loop; (3) a11y-tree observation coverage on Electron/Qt/canvas/WebGL/native apps — the **single load-bearing unverified assumption** for the whole structured-first thesis (pixel + zoom fallback is mandatory regardless); (4) realized $/task and Pass^k distributions at the concurrency Shinken targets; and (5) event-schema versioning + upcasting so historical `.skn` corpora stay re-gradable. These are tracked in [open-questions.md](open-questions.md).

---

## 7. Bottom line

The CUA benchmark layer in 2026 has a credibility problem the *models* cannot fix: scores saturate, vendors self-report, live sites drift, graders carry hundreds of bugs, and the scaffold — not the model — frequently sets the number. The fix is a harness, not a model. Shinken keeps OSWorld's declarative task-as-data unit and inverts everything brittle: a **typed verifier DAG** validated at load and run *inside the guest* via the ACI; a **golden snapshot** that content-addresses its own oracles; **N≥5 CoW-forked replicas** reporting **pass^k** as the headline reliability floor; a phase-aware INIT/RUN/VERIFY pipeline with **readiness probes**; **programmatic-primary grading** with a constrained model-verifier strictly as fallback; and **offline re-grade** off the `.skn` bundle that turns "fix 300 graders" into a query. It matches HUD's task contract and default-trace ergonomics while staying self-hostable and bandwidth-frugal, and beats cua-bench on the four axes that matter — deterministic replay, milestone scoring, observation bandwidth, and one typed evaluator schema. It is affordable only because it rides the *same* fast-fork runtime that serves production agents — the point of D7, and of Shinken.
