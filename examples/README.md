# Examples

Every example is **scripted** — no model API key needed. Each file's docstring carries the
full story; this table is the index. The `backends_*` scripts and `uniagent_shinken.py`
bootstrap `sys.path` to the in-repo SDK, so a plain checkout is enough; the Docker-backed
ones additionally need the local sandbox image (`make sandbox-image`, see
[`images/linux/`](../images/linux)).

| Example | What it proves | Run | Needs |
|---|---|---|---|
| [`backends_cua_shinken.py`](backends_cua_shinken.py) | D15 backend: the Shinken ACI driven over a trycua/cua computer interface; honest fork-tier degrade | `python examples/backends_cua_shinken.py` | nothing (in-memory peer; no Docker) |
| [`backends_mcp_computer_shinken.py`](backends_mcp_computer_shinken.py) | D15 backend: a codex-style MCP AX server serving structured observe + `element_ref` (fills the macOS-AX gap) | `python examples/backends_mcp_computer_shinken.py` | nothing (in-memory peer; no Docker) |
| [`backends_browser_runtime_shinken.py`](backends_browser_runtime_shinken.py) | D15 BU backend: the designed Browser Runtime shape as a CDP backend — pixels, semantic node-ids, locator/script | `python examples/backends_browser_runtime_shinken.py` | nothing (in-memory peer; no Docker) |
| [`backends_e2b_shinken.py`](backends_e2b_shinken.py) | D15 backend: e2b-desktop cloud Linux desktop under the typed ACI — pixels + shell, honest no-structured/no-fork | `python examples/backends_e2b_shinken.py` | nothing (in-memory peer; no Docker) |
| [`backends_routed_cu_bu.py`](backends_routed_cu_bu.py) | `RoutedSession`: one operator loop over a CU + a BU surface with per-action `source` provenance | `python examples/backends_routed_cu_bu.py` | nothing (in-memory peers; no Docker) |
| [`gym_rollout.py`](gym_rollout.py) | Fork-native gym (`shinken.gym`): golden checkpoint once, `reset()` = fork, pool parallel reset, raw-model-text steps, HF export | `PYTHONPATH=sdk/python/src python examples/gym_rollout.py [n_envs]` | Docker + sandbox image |
| [`cua_gym_shinken.py`](cua_gym_shinken.py) | CUA-Gym task bundles with fork-native reset (bundle setup once, every reset forks the golden checkpoint) | `PYTHONPATH=sdk/python/src python examples/cua_gym_shinken.py [output/final]` | Docker + sandbox image (demo bundle auto-generated) |
| [`agentix_shinken.py`](agentix_shinken.py) | Shinken as an Agentix-shaped `SandboxProvider` (+ `golden=` so every `create()` forks instead of cold-booting) | `PYTHONPATH=sdk/python/src python examples/agentix_shinken.py` | Docker + sandbox image |
| [`uniagent_shinken.py`](uniagent_shinken.py) | Shinken behind the swerex deployment/runtime protocol — the uni-agent/verl seam | `python examples/uniagent_shinken.py` | Docker + sandbox image |
| [`nemo_gym/`](nemo_gym) | NeMo Gym resources server: computer-use RL environments with fork-native rollouts | see [`nemo_gym/README.md`](nemo_gym/README.md) | Docker + sandbox image |
