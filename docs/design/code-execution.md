# CLI / code execution as a separate capability surface (#60)

GUI action execution and **CLI / code execution** are different things and must not be
conflated. Clicking, typing, and scrolling are *typed ACI verbs* dispatched to a GUI
backend (D2). Running a shell command, a Python snippet, an installer, or an editor is a
**powerful, side-effecting capability** that belongs behind the D6 `tool_runner` policy
boundary — not as "just another GUI backend." This doc defines the Phase-0 boundary so
the two never blur. It is a **design/spec**; the runtime lands later and does not block
the typed GUI act/observe work (#4). How code-agent **RL workloads** compose this
boundary (headless profile, fork-N rollouts, reward seams) is
[`code-agent-rl.md`](code-agent-rl.md) — this doc stays the exec-semantics anchor.

## What goes where

| Concern | Surface | Why |
|---------|---------|-----|
| `click / type_text / key / scroll / move / screenshot / screencast / wait` | **ACI typed verbs** (D2) → GUI Executor | Deterministic, low-authority, the universal computer-use loop. |
| Run a command / script / installer; read-write files; manage processes | **`tool_runner` capability** (D6), *not* ACI GUI verbs | High authority, arbitrary side effects; must be policy-gated and audited, never implicit. |
| Arbitrary RCE server / unauthenticated exec endpoint | **out of scope** | The OSWorld anti-pattern Shinken explicitly rejects ([osworld-analysis](osworld-analysis.md)). |

Command execution is therefore **not** an ACI action verb in v0. The agent loop reaches it
through the Operator's gateway-checked tool surface, so every invocation passes a capability
decision (allow / ask / deny) before it runs — exactly like other boundary powers.

## Phase-0 command-execution semantics

A single, minimal, typed request — **not** free-form code over the wire:

```jsonc
// request (behind the tool_runner boundary; never an ACI GUI action)
{ "cmd": ["python", "-c", "print(1)"],   // argv vector, NOT a shell string (no implicit shell)
  "cwd": "/work",                          // within the granted fs.scope
  "env": { "FOO": "bar" },                 // explicit allowlist; inherited env is not leaked
  "timeout_ms": 30000,                     // bounded; killed on expiry
  "stdin": null }
// result
{ "exit_code": 0,
  "stdout": "...", "stderr": "...",        // captured, size-capped, truncation flagged
  "truncated": false,
  "duration_ms": 12,
  "timed_out": false,
  "error": null }                          // structured error (spawn failure, denied, timeout)
```

- **argv, not shell.** Commands are an argv vector executed without an implicit shell, so
  there is no string-interpolation injection surface. A shell, if needed, is an explicit
  `["bash","-lc", ...]` the policy can recognize and gate.
- **Bounded.** `timeout_ms` is required-with-a-default and enforced (process group killed);
  stdout/stderr are size-capped with a `truncated` flag — never unbounded buffering.
- **Errors are structured**, not exceptions leaked to the model: spawn failure, policy
  denial, timeout, and non-zero exit are distinct, machine-readable outcomes.

## Security invariants

- **Filesystem**: confined to the granted `fs.scope` capability; `cwd` and any path args are
  validated against it. No ambient access to the host or other sessions.
- **Egress**: a spawned process inherits the sandbox's `net.egress` posture (deny-by-default
  at the boundary); command execution does not widen egress.
- **Credentials**: brokered by handle/injection (D6); secrets are never passed as plaintext
  argv/env that the model can read back, and are redactable in replay.
- **Replay redaction**: `cmd`, `env`, `stdin`, and captured `stdout`/`stderr` are
  redaction-eligible fields (same machinery as `action.text` / media bytes, #151), so a
  recorded run need not leak command contents or output.

## How it appears in `.skn`

Reuse the existing event model rather than inventing a parallel one:

- The request records as an **`action`** event with `src: "exec:run_command"` and an
  `action_id`; the result records as the **paired** event (`action_id` match) carrying
  `{ exit_code, duration_ms, timed_out, truncated }` plus content-addressed (and
  redaction-eligible) refs for large `stdout`/`stderr`.
- The capability decision that admitted it is a first-class **`permission`** event
  (allow / ask / deny), so the audit trail shows *why* the command was allowed.

This keeps replay one timeline: GUI actions, observations, command executions, and the
permission events that gated them, all paired and scrubbable.

## Non-goals (v0.0.1)

- No command-execution **runtime** is built here — this is the boundary spec. Implementation
  follows behind `tool_runner` + the server-side Action Gateway (D6).
- It does **not** block typed GUI action work (#4); the two surfaces are independent.
- No arbitrary-code ACI verb, ever — that authority lives only behind the policy boundary.
