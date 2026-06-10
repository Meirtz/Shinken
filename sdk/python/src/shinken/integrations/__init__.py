"""Interop adapters — Shinken behind the interfaces trainer-camp frameworks already speak.

Each module here is a *consumer-side* adapter (`TaskSource`/`Scorer`/provider shims live in
consumer libs, never in the runtime waist — see ``docs/design/agent-runtime.md`` §5): it
exposes Shinken sandboxes through another framework's expected protocol shape so that stack
becomes a distribution channel for the runtime. Hard rule: nothing in this package imports
the external framework at module import time — integrations are duck-typed against the
published protocol shape and fixture-tested, lazily importing the real package only when it
is installed, so the Shinken SDK never grows a dependency on the frameworks it serves.

- :mod:`shinken.integrations.cua_gym` — xlang-ai/CUA-Gym task bundles as a ``TaskSource``
  plus their VM-env method surface over a Shinken provider, with **fork-native reset**
  (golden checkpoint once, fork per reset) replacing their fresh-cloud-VM-per-use lifecycle.
- :mod:`shinken.integrations.agentix` — Agentix-Project/Agentix ``SandboxProvider``-shaped
  provider exposing ``DockerLocalProvider`` + the typed ACI through their
  create/delete/get/session lifecycle.
- :mod:`shinken.integrations.swerex` — the SWE-ReX deployment/runtime protocol, the
  sandbox seam used by uni-agent (and the verl ecosystem it feeds).
- :mod:`shinken.integrations.prorl_agent_server` — a ProRL-Agent-Server runtime plugin:
  Shinken sandboxes as rollout-server sessions (rollout-as-a-service).

Import the submodule you need directly (no eager imports here)::

    from shinken.integrations.cua_gym import CuaGymTaskSource, ShinkenCuaGymEnv
    from shinken.integrations.agentix import ShinkenAgentixProvider
    from shinken.integrations.swerex import ShinkenDeployment
    from shinken.integrations.prorl_agent_server import ShinkenRuntime
"""

__all__ = ["agentix", "cua_gym", "prorl_agent_server", "swerex"]
