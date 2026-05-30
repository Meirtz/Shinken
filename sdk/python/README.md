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

> **Status:** the SDK has moved beyond the original M0 handshake. It supports connect/query/ping,
> typed actions, screenshot/focused capture, screencast consumption, and minimal `.skn` recording.
> v0.0.1 fills in agent-native dialects/adapters, structured observation, capabilities, artifacts,
> and tiny eval. See [`docs/user/quickstart.md`](../../docs/user/quickstart.md),
> [`docs/engineering/v0.0.1-plan.md`](../../docs/engineering/v0.0.1-plan.md), and
> [`docs/design/aci-spec.md`](../../docs/design/aci-spec.md).

## Develop

```bash
cd sdk/python
pip install -e ".[dev]"
ruff check .
pytest -q          # uses an in-process mock shinkend; no Rust binary needed
shinken connect    # against a running shinkend (see ../../shinkend)
```
