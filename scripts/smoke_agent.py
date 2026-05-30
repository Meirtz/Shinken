"""Run the first-party provider smoke agent (#91) — provider-neutral, env-driven.

Configure via your *ignored local* environment (never commit values):
  SHK_SMOKE_MODEL_BASE_URL / SHK_SMOKE_MODEL_API_KEY / SHK_SMOKE_MODEL_NAME
  SHK_ADDR (+ SHK_TOKEN), optional SHK_TASK_EGRESS_PROXY

Skips cleanly when the model config is absent. Prints a secret-free JSON result
(proxy is reported as status only). Exit 0 on pass/skip, 1 on fail/error.
"""

import json
import sys

from shinken.smoke import run_smoke_agent

result = run_smoke_agent()
print(json.dumps(result.to_dict(), indent=2))
sys.exit(0 if result.status in ("pass", "skipped") else 1)
