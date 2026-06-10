# OSWorld Evaluation Bring-Up

Audience: implementers preparing Shinken to run OSWorld-style evaluations.

This document is the next implementation target for eval work. It is deliberately practical:
OSWorld tasks and images are fixed benchmark assets, so Shinken should not require replacing the
official image with a Shinken image. The right integration is to inject a small Shinken guest layer
into the running OSWorld VM, drive it through ACI, and let OSWorld's existing task setup and verifier
logic remain the source of truth.

## Goal

Run OSWorld tasks under Shinken's control loop:

```text
OSWorld provider/image/snapshot -> inject shinkend -> Shinken Operator/SDK drives ACI ->
OSWorld evaluator checks final state
```

The first milestone is **one real OSWorld task end to end**. The second milestone is the public
small manifest (`test_small` in the upstream OSWorld layout). Only after those pass should we spend
time on full-suite throughput, dashboards, or training-data capture.

## How OSWorld Normally Runs

OSWorld has four load-bearing pieces:

- A fixed VM/image/snapshot per task family.
- A task config with setup operations, instruction text, and evaluator metadata.
- A guest server inside the VM, traditionally a Flask process on port 5000, that exposes screenshot,
  accessibility, file, shell, and Python execution routes.
- A host runner that loops: reset snapshot, run setup, observe, ask an agent for actions, execute
  actions, then call the evaluator.

The public corpus is selected by manifest files such as `test_all`, `test_small`, and
`test_infeasible` in the OSWorld checkout. Exact filenames can vary by upstream revision; the
integration must discover or accept the manifest path rather than hard-code one layout.

## Shinken Integration Shape

Shinken should use OSWorld for what it is good at:

- task selection,
- official images/snapshots,
- setup fixtures,
- final verifiers.

Shinken should replace the action/observation transport:

- no model-supplied Python as the action primitive,
- no unauthenticated broad control path as the long-term interface,
- no full-frame polling as the only observation mode once streaming exists.

For the first version, the compatibility path is:

1. Let OSWorld boot/reset the official VM for a single task.
2. Upload or copy `shinkend` into the guest.
3. Start `shinkend` inside the guest with a dev token and reachable port.
4. Connect from the host with `shinken.connect(addr, token=...)`.
5. Drive the task through `shinken.osworld.DesktopEnv` or the Operator loop.
6. Call the OSWorld evaluator against the same VM.

This means Shinken can be benchmarked against fixed OSWorld assets without baking a custom image for
every benchmark release.

## Single-Task Gate

Before any "Small set" work, implement and run one real task.

The single-task gate must prove:

- The official OSWorld provider can start or restore the selected task snapshot.
- Shinken can install/start `shinkend` in that guest without permanently modifying the base image.
- `shinken.connect()` can handshake with the injected runtime.
- `env.screenshot()` returns the live OSWorld desktop.
- At least one typed action (`click`, `type_text`, or `key`) changes the live desktop state.
- The task's official evaluator can still run and produce a score.
- The run result records task id, image/snapshot id, model/agent id, step count, wall-clock time,
  final score, and failure reason when applicable.

Recommended first-task shape:

- Choose a deterministic task from the upstream small manifest that uses a simple GUI surface.
- Prefer a task whose evaluator is programmatic and not dependent on a model judge.
- Prefer a task that can be solved by a scripted agent for the first pass.

Avoid using a task that requires web credentials, external network, video playback, or long
multi-application workflows for the first gate.

## Small-Set Gate

After one task passes, run the small manifest.

Small-set requirements:

- Sequential execution is acceptable.
- Each task starts from its official clean state.
- Failures are classified as setup, connection, action, verifier, timeout, or harness error.
- Results are emitted as JSONL plus a summary JSON:
  - `task_id`,
  - `instruction`,
  - `manifest`,
  - `snapshot`,
  - `agent`,
  - `steps`,
  - `wall_s`,
  - `score`,
  - `passed`,
  - `error`.
- The run can resume from the result directory by skipping completed task ids.

The small-set goal is not a high score. It is to prove that Shinken can act as the driver while
OSWorld remains the official environment and scorer.

## Full-Suite Gate

Full OSWorld evaluation comes after the small-set gate.

Full-suite requirements:

- Use the upstream manifest as the source of task ids.
- Keep official images/snapshots unchanged.
- Bound max steps and per-task wall time.
- Save raw stdout/stderr logs for setup and verifier failures.
- Produce aggregate metrics:
  - pass rate,
  - mean / median steps,
  - mean / median wall time,
  - failure taxonomy,
  - per-app pass rate where task metadata allows grouping.
- Support concurrency only after sequential correctness is stable.

## Parity discipline

Eval scores are only defensible if they are comparable to the official OSWorld harness, so the bring-up keeps a parity ledger and emits parity warnings:
- Whenever a knob deviates from the upstream OSWorld defaults (e.g. setup-settle wait, max-steps, sleep-after-action, max-tokens, screenshot history depth), log an explicit one-line **OSWorld parity warning** at run start naming the deviation — silent divergence from upstream defaults is the fastest way to produce an unreproducible number.
- Maintain a behavior-alignment ledger in this doc recording every intentional divergence from upstream OSWorld behavior (e.g. WAIT semantics, DONE/FAIL screenshot timing) and the reference parameters/scores it was validated against. Vendor the official evaluator and run its getters/metrics unmodified so a score is defensible against the upstream harness (see [OSWorld](https://github.com/xlang-ai/OSWorld)).

## Implementation Work Items

### 1. Guest Injection — IMPLEMENTED

`shinken.inject` injects `shinkend` over a user-chosen transport (`docker`/`ssh`/`osworld-exec`),
readiness-polls the bound port, and fails loudly with no silent fallback. Build the target-arch
binary with `scripts/build_shinkend_linux.sh` (reuses the image's `build` stage, `--platform
linux/amd64`, copies the binary out) so it is a one-liner, not tribal knowledge:

```text
scripts/build_shinkend_linux.sh -> dist/shinkend-linux-x86_64
osworld_single.py --inject-method osworld-exec --shinkend-binary dist/shinkend-linux-x86_64
  -> upload + set SHINKEND_ADDR/TOKEN + DISPLAY + SHINKEND_EXECUTOR=x11_xtest
  -> start process -> readiness poll -> ACI handshake
```

The injection **pins the X11 backend** (`pin_x11_display` → `SHINKEND_EXECUTOR=x11_xtest`,
`DISPLAY=:0`): a missing/unreachable guest display then fails the readiness poll loudly instead of
silently binding the no-op virtual backend (which screenshots a dead display and scores every task
0). This uses OSWorld's existing setup/execute channel for bootstrap, not the model-facing action
path.

### 2. Address Discovery

The runner needs a typed handle:

```python
{
    "task_id": "...",
    "provider": "osworld",
    "guest_addr": "host:port",
    "token": "...",
    "snapshot": "...",
}
```

Do not rely on colon-packed strings long term. Normalize OSWorld provider output into this handle.

### 3. Action Bridge

Support the OSWorld action spaces through typed ACI:

- `computer_13` dicts -> ACI actions,
- simple PyAutoGUI code strings -> ACI actions for compatibility,
- Anthropic/OpenAI adapters -> ACI actions through the existing adapter layer.

No arbitrary model-supplied Python should reach the Shinken action path.

### 4. Observation Bridge

Minimum:

- screenshot bytes from `env.screenshot()`,
- instruction text from the OSWorld task config.

Later:

- accessibility tree through a guest-side source,
- CDP for browser tasks,
- Set-of-Marks or region zoom for pixel-only surfaces.

### 5. Verifier Bridge

Do not rewrite OSWorld graders first. Call the upstream evaluator against the same VM and record the
result. Later, convert high-value graders into typed Shinken verifier DAGs.

### 6. Runtime-State Upgrade

Once single-task correctness works, replace slow reset loops with Shinken runtime-state primitives
where the provider allows it:

```text
official clean state -> checkpoint golden -> fork N replicas -> run agents -> score each branch
```

For official OSWorld VM providers that do not support fork, keep sequential reset and report that
limitation honestly.

## Proposed CLI Shape

The CLI can start as an internal script before becoming a public command:

```bash
python scripts/osworld_single.py \
  --osworld-root /path/to/OSWorld \
  --task-id <task-id> \
  --provider vmware \
  --agent scripted \
  --out runs/osworld-single
```

Small set:

```bash
python scripts/osworld_eval.py \
  --osworld-root /path/to/OSWorld \
  --manifest test_small \
  --provider vmware \
  --agent shinken-scripted \
  --out runs/osworld-small
```

The first implementation may be more specific, but it should keep this shape in mind.

## What Not To Do

- Do not require a custom Shinken OSWorld image for the first pass.
- Do not replace OSWorld evaluators before one official task passes.
- Do not optimize concurrency before sequential correctness.
- Do not route model actions through arbitrary Python.
- Do not report a headline OSWorld score from a non-official task subset.

## Acceptance Checklist

Single task (in-repo readiness done; remaining items need an external OSWorld VM + model endpoint):

- [ ] Official OSWorld task config selected.
- [ ] Official image/snapshot started.
- [x] `shinkend` injection path implemented — `shinken.inject` (readiness-polled, X11-pinned, no
  silent fallback) + `scripts/build_shinkend_linux.sh` for the target-arch binary.
- [x] Shinken SDK observes the desktop (the OSWorld `DesktopEnv` shim → ACI screenshot).
- [x] Shinken SDK executes typed actions (pixel-pyautogui → ACI, scroll units reconciled).
- [ ] Official evaluator returns a score (needs a live VM run).
- [x] JSON result record implemented — `osworld_single.py --out` writes task id, task_id, snapshot,
  model, steps, wall_s, score, error, and parity warnings; written even when the run raises.
- [ ] **Execute the gate for real** and commit the result receipt (the one remaining external step).

Small set:

- [ ] Manifest-driven task loop.
- [ ] Skip/resume completed task ids.
- [ ] Per-task failure taxonomy.
- [ ] Aggregate summary.
- [ ] At least one scripted baseline agent and one model-adapter path.

Full:

- [ ] Official manifest and official scorer.
- [ ] Reproducible run config.
- [ ] Sequential correctness first.
- [ ] Optional concurrency/forking only after correctness.
