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

> **M0 status:** handshake + `ping`/`query`. The full surface
> (`observe`/`act`/`run`/`save`/`restore`/`fork`/`drive`/`unlock`) lands in later milestones —
> see [`docs/10-phase0-plan.md`](../../docs/10-phase0-plan.md) and the ACI spec
> [`docs/11-aci-spec.md`](../../docs/11-aci-spec.md) (forthcoming, #9).

## Develop

```bash
cd sdk/python
pip install -e ".[dev]"
ruff check .
pytest -q          # uses an in-process mock shinkend; no Rust binary needed
shinken connect    # against a running shinkend (see ../../shinkend)
```
