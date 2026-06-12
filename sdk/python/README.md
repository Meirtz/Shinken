# shinken (Python SDK)

The Python client + Operator for [Shinken](https://github.com/Meirtz/Shinken). Connects to a
Guest Runtime (`shinkend`) over the ACI and exposes an elegant, blocking API.

```python
import shinken

env = shinken.connect()          # one-line; completes the ACI handshake
print(env.platform)              # 'linux' | 'windows' | 'macos'
print(env.screen_size())         # {'w': 1280, 'h': 800}
env.close()
```

> **Status:** the full v0.0.1 surface is built. Connect/query/ping, the 22 typed action verbs,
> screenshot/focused capture + screencast consumption, **structured observation**
> (`observe(structured=True)`, `element_ref`, `invoke_action`/`set_value`), model
> adapters/dialects (Anthropic/OpenAI/Kimi-VL + XML action grammars), checkpoint/fork/resume
> (Docker disk tier + CRIU memory tier), the **fork-native gym** (`shinken.gym`:
> `reset()` = fork) and `run_eval_forked`, **operation-layer backends** (`shinken.backends`:
> cua / MCP / CDP browser / e2b under the same ACI), a pipelined `step()` (~1 RTT per
> k-action step), and a local capability-gateway shim. Authoritative map:
> [`docs/engineering/status.md`](../../docs/engineering/status.md); spec:
> [`docs/design/aci-spec.md`](../../docs/design/aci-spec.md).

## Develop

```bash
cd sdk/python
pip install -e ".[dev]"
ruff check .
pytest -q          # uses an in-process mock shinkend; no Rust binary needed
shinken connect    # against a running shinkend (see ../../shinkend)
```
