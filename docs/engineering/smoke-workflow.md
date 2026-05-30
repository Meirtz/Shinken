# Local smoke workflow — hygiene (#92)

How to run Shinken's first-party smoke agent (#91) and optional task-egress proxy
(#93) **locally** without leaking secrets or private details into this public repo.

> The tracked code is provider-neutral and env-driven. Real endpoints, keys, proxy
> details, and any private names live **only** in ignored local paths (e.g. an
> untracked env file) or an operator secret store — never in tracked files.

## Configuration (by role, not value)

| Variable | Role |
|----------|------|
| `SHK_SMOKE_MODEL_BASE_URL` | model endpoint base URL (OpenAI-compatible `/chat/completions`) |
| `SHK_SMOKE_MODEL_API_KEY`  | model API key |
| `SHK_SMOKE_MODEL_NAME`     | model name |
| `SHK_ADDR` / `SHK_TOKEN`   | `shinkend` address + dev token |
| `SHK_TASK_EGRESS_PROXY`    | optional task-egress proxy URL (`http[s]://[user:pass@]host:port`) |

Set these in your own ignored local environment. If the model variables are absent, the
smoke **skips cleanly** (it does not fail).

## Running

```bash
# from your ignored local env (never committed):
#   export SHK_SMOKE_MODEL_BASE_URL=... SHK_SMOKE_MODEL_API_KEY=... SHK_SMOKE_MODEL_NAME=...
python scripts/smoke_agent.py
```

The smoke exercises the full loop — observe (screenshot) → agent → typed action →
record → score — and writes a `.skn` replay. The task-egress proxy, if configured, is
applied to **task egress only** (never to client↔control-plane or sandbox-lifecycle
traffic) and is reported as `requested` / `skipped` / `failed` — **never** with host,
user, or password values.

## Result template (secret-free)

```json
{ "status": "pass | fail | skipped | error",
  "reason": "<short, no secrets>",
  "steps": 3,
  "bundle": "<path to .skn>",
  "proxy": { "task_egress_proxy": "requested | skipped | failed", "applied_to": "task_egress" } }
```

Distinguish **local import/config checks** (does the SDK import, does config parse,
does it skip cleanly) from a **real first-party harness run** (a credentialed model
round-trip executing actions against a live `shinkend`). CI runs only the former.

## Verify no secrets before opening a PR

```bash
bash scripts/check-no-internal.sh   # CI guard: no private identifiers/links in tracked files
git status --porcelain               # nothing under internal/ or other ignored paths is staged
git diff --cached                    # eyeball: no keys, endpoints, hostnames, or private names
```

Never commit keys, endpoint hostnames, domains, template IDs, account metadata, proxy
credentials, or private provider/repository/product names. If a helper is missing,
write it in Shinken from scratch (as `shinken.smoke` / `shinken.egress` do).
