# Agent-quality study — codec tier × task success (Arm 2)

**Status: harness built and oracle-validated end to end (80/80 plumbing episodes across
the whole corpus, all 5 tiers); real-model pilot run; the full study is ready to run and
pending a budget decision.** Raw artifacts:
[`benchmarks/results/agent_quality_oracle_plumbing.json`](../../benchmarks/results/agent_quality_oracle_plumbing.json)
(scripted-oracle pass over the whole corpus) and
[`benchmarks/results/agent_quality_pilot.json`](../../benchmarks/results/agent_quality_pilot.json)
(real-model pilot). Harness: [`benchmarks/bench_agent_quality.py`](../../benchmarks/bench_agent_quality.py);
tasks: [`benchmarks/_study_tasks.py`](../../benchmarks/_study_tasks.py);
analysis: [`benchmarks/analyze_agent_quality.py`](../../benchmarks/analyze_agent_quality.py).

**Pilot headline** (Kimi K2.6 over an env-configured OpenAI-compatible endpoint,
temperature 0; 2 tasks × 3 tiers × 2 seeds = 12 episodes, 12/12 verdicts, zero infra
errors; ~37 k prompt + ~37 k completion tokens over 31 model calls):

| tier | success | what the verifier saw |
|---|---|---|
| `png-native` (control) | **4/4** | exact transcriptions (`TXJ7-67AN`, `43RNC3`), 1–3 steps |
| `q50-1024` | **0/4** | single-glyph JPEG misreads: `TXJ7→TX17` (J→1), `43RNC3→43TNC3` (R→T) |
| `q10-768` | **0/4** | same J→1 at 13 pt; fully hallucinated codes at 7 pt (`120943`, `452918`) |

The failure *mode* is the designed signal: the agent acts competently (finds the window,
focuses the prompt, types, presses Enter — steps and actuation identical to control) but
**transcribes what the codec shows it**, and the deterministic verifier catches the
defect at glyph granularity. With n=4/tier the Wilson CIs are wide; the −5 pp
non-inferiority calls need the full-study n (§8) — but a −100 pp point estimate with
disjoint CIs already says these two tiers are not free for fine-text-critical work on
this model. (The oracle passes every tier by construction, so the degradation is
model-visual, not environmental.)

## 1. The question

The codec ladder ([benchmarks.md §2](benchmarks.md)) showed the observation bandwidth
levers (JPEG quality × `max_long_edge` downscale) buy 1–70× per frame. Those numbers say
nothing about whether an *agent still succeeds* when fed the cheaper pixels. This study
measures the missing curve: **task success rate as a function of codec tier**, on tasks
that are deliberately *read-from-screen-critical* — success requires transcribing or
locating information that exists only in the pixels.

It is Arm 2 of a two-arm design:

- **Arm 1 (legibility)**: per-tier mechanical legibility scoring of the same frame
  content (produced separately; joined here on tier labels — §6).
- **Arm 2 (this)**: real agent, real episodes, deterministic verifiers.

## 2. Why fork is the methodology, not just the subject

Per task the harness builds **one golden checkpoint** (`shinken.gym.ShinkenGymEnv.make`:
boot → `task.setup` once → checkpoint), then materializes *every* episode — every codec
tier × seed — as a **fork of that single checkpoint**. All experimental conditions
therefore start from byte-identical disk state: identical task content, launcher bytes,
fonts, window geometry. A measured between-tier difference cannot be caused by
per-episode environment re-provisioning variance (boot races, different task
instantiation), because that variance class is eliminated by construction — the
platform's own checkpoint/fork wedge (D5, [benchmarks.md §1](benchmarks.md)) doing
methodology work. Two honest caveats:

- Processes do not survive the Docker **disk** tier, so the task UI is re-materialized
  per fork by the *same* `launch.sh` captured in the checkpoint (deterministic
  `-geometry`/font flags); the byte-identical guarantee is the filesystem, the visual
  start state is launcher-deterministic.
- A uniform actuation guard (300 ms wait inserted between a click and a type/key in the
  same action block) removes the WM focus-race — an actuation noise source orthogonal to
  the codec under study — identically at every tier.

## 3. Task corpus (16 tasks, deterministic verifiers)

8 templates × {normal 13 pt, small 7 pt} font on the read-critical window (DejaVu Sans
Mono; the zenity dialog uses Pango `font="N"` markup). The small-font half is the
designed breaking case. All tasks are self-contained in the standard
`shinken/sandbox-linux` image (xterm/zenity/`exec`; no network), with fixed-seed content
so the corpus is identical across runs. Every verifier reads **guest state** back over
the typed in-guest `exec` channel and emits a `shinken.eval.VerifierReceipt` — never a
pixel match, never a judge model.

| template | the agent must read… | …and act | verifier |
|---|---|---|---|
| `code_prompt` | an 8-char access code in the TASK window | type it into the ANSWER prompt | `cat /tmp/answer.txt` == code |
| `key_value_lookup` | the named row in an 8-row name/code registry | type that row's code | answer == row code |
| `zenity_button` | which button label the dialog text names | click that button (among 3 decoys) | `/tmp/clicked.txt` == label |
| `transcribe_note` | a two-line note | type both lines | both lines exact, in order |
| `larger_number` | two close 4-digit readings | type the larger | answer == max |
| `run_script_by_code` | a RUN code + a script listing | execute the matching script in a SHELL terminal | `/tmp/opened.txt` == only that code |
| `long_serial` | a 19-char hyphenated serial | transcribe it | answer == serial |
| `find_extension` | a 6-file directory listing | type the only `.cfg` file's name | answer == filename |

The code alphabet excludes confusable glyphs (0/O, 1/I/l, …) so a control-tier failure
means agent error, not font ambiguity.

## 4. Conditions and protocol

Codec tiers (the observation actually served to the model — both the post-step fused
frame and the episode's initial capture use the tier's settings; labels are the Arm-1
join key):

| tier | format | quality | max_long_edge |
|---|---|---|---|
| `png-native` (control) | PNG | — | native 1280×800 |
| `q80-1024` | JPEG | 80 | 1024 |
| `q50-1024` | JPEG | 50 | 1024 |
| `q50-512` | JPEG | 50 | 512 |
| `q10-768` | JPEG | 10 | 768 |

Episode protocol: fork from golden → run `launch.sh` → wait for the task windows to map
→ serve the tier observation → up to **10 model turns** (one `<actions>` block per turn,
parsed by `shinken.dialect.parse_actions(format="auto")`; an unparseable turn costs a
step and returns a teaching note) → episode ends on `<done/>` or the step budget →
verifier runs. Model pixel coordinates are interpreted in the served image's own space
and rescaled to native by the harness. Infra deaths (`sandbox_died`) are typed and
excluded from denominators, never scored as failures.

**Model layer**: one pluggable OpenAI-compatible vision endpoint —
`SHINKEN_STUDY_BASE_URL` / `SHINKEN_STUDY_API_KEY` / `SHINKEN_STUDY_MODEL` (falls back
to the repo's `SHK_SMOKE_MODEL_*` convention). Temperature 0, fixed minimal CU prompt,
latest screenshot only (no image history). Without an endpoint the harness runs the same
plumbing with the **scripted oracle** (ground truth from task metadata; emits raw
dialect text through the same parse path, and clicks the zenity button via structured
observation + `element_ref`) — it validates fork/observe/parse/verify, and says nothing
about legibility by design.

## 5. Statistics

`analyze_agent_quality.py` reports, per tier: success rate with a **Wilson 95% score
interval**, and **non-inferiority vs the lossless control** using the Newcombe score
interval for the difference p_tier − p_control with a **−5 pp margin** — a tier is
declared non-inferior when the *lower* bound of the difference CI is ≥ −0.05 (the
deployment question is "can we serve this tier without losing success", not
superiority). Steps-to-success (mean/median over passing episodes) catches the
degradation mode where success survives but costs extra read-retry turns.

## 6. Join point with Arm 1 (legibility)

`analyze_agent_quality.py --legibility <file.json>` merges per-tier legibility scores on
the **canonical tier labels** above (defined once, in `bench_agent_quality.TIERS`).
Accepted shapes: `{tier: score}`, `{tier: {…}}`, or a list of rows carrying a `tier`
key (wrappers `per_tier`/`tiers`/`legibility` are unwrapped). The file is optional by
design — the analysis never depends on Arm-1 artifacts existing.

## 7. How to run

```bash
# plumbing check (scripted oracle, no model): 1 task x {control, q10-768}
python benchmarks/bench_agent_quality.py --mode smoke

# the full oracle plumbing receipt (16 tasks x 5 tiers x 1 seed)
python benchmarks/bench_agent_quality.py --mode full --agent oracle --seeds 1 \
    --out agent_quality_oracle_plumbing

# real-model pilot: 2 tasks (one normal + one small-font) x 3 tiers x 2 seeds
set -a; . ./.env; set +a   # or export SHINKEN_STUDY_{BASE_URL,API_KEY,MODEL}
python benchmarks/bench_agent_quality.py --mode pilot

# the full study
python benchmarks/bench_agent_quality.py --mode full --seeds 8

# analysis (+ optional Arm-1 join and figure)
python benchmarks/analyze_agent_quality.py results/agent_quality_full.json \
    --legibility results/legibility_scores.json --plot
```

## 8. Budget math (full study)

16 tasks × 5 tiers × 8 seeds = **640 episodes**, ≤ 10 model calls each → ≤ 6,400 calls
(read-and-type tasks settle in 3–5 turns, so ~3,200 calls expected). Each call carries
one screenshot (the tier's own encoding — the cheap tiers are also the cheap-token
tiers) plus ~600 prompt tokens of text; with a reasoning-class model budget ~1–4 k
completion tokens per call. Pilot-measured per-episode token costs are recorded in
`agent_quality_pilot.json` (`tokens` and per-episode `prompt_tokens`/
`completion_tokens`) — multiply by ~640/12 to project the full bill for a given
endpoint. Wall time is model-latency-dominated; the sandbox side is ~1–2 s per episode
(warm-pool fork ~0.1 s, launcher+readiness ~1 s).

## 9. Limitations

- One model per run; the pilot's numbers characterize that endpoint, not "agents".
- The corpus is text-legibility-centric by design (that is the lever under test);
  icon/affordance recognition degradation is not covered.
- Disk-tier fork determinism covers state, not the X server's scheduling; the
  remaining run-to-run variance is what the seed replicates measure.
- `zenity` window placement is WM-decided (everything else is `-geometry`-pinned);
  observed stable across forks in practice.
