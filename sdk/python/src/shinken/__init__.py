"""Shinken — AI-native sandbox runtime for computer-use agents (Python SDK).

The elegant entry point is :func:`connect`, which opens a session to a running
Guest Runtime (``shinkend``) and completes the ACI handshake::

    import shinken

    env = shinken.connect()            # one-line, blocking
    print(env.platform)                # 'linux' | 'windows' | 'macos'
    print(env.screen_size())           # {'w': 1280, 'h': 800}
    env.close()

M0 implements the handshake + ``ping``/``query``. Observation, actions, and the
``observe``/``act``/``run``/``save``/``restore``/``fork``/``drive`` surface land in
later milestones (see docs/10-phase0-plan.md).
"""

from .client import (
    AsyncSandbox,
    Capabilities,
    Checkpoint,
    FrameCache,
    Sandbox,
    SandboxFleet,
    SharedLoop,
    aconnect,
    connect,
)
from .dialect import DialectError, parse_actions, parse_xml_actions
from .errors import (
    ACTION_STATUSES,
    ConnectError,
    ProviderRequired,
    SandboxDied,
    ScorerError,
    SessionClosed,
    ShinkenError,
    UnknownVerb,
    classify_exception,
)
from .gateway import CapabilityDenied
from .providers import (
    CriuDockerProvider,
    DockerLocalProvider,
    ExternalProvider,
    GcReport,
    SandboxSpec,
)

__version__ = "0.1.0"
__all__ = [
    "connect",
    "aconnect",
    "Sandbox",
    "AsyncSandbox",
    "SharedLoop",
    "Capabilities",
    "Checkpoint",
    "FrameCache",
    "SandboxFleet",
    "CapabilityDenied",
    "ShinkenError",
    "SessionClosed",
    "ConnectError",
    "UnknownVerb",
    "ProviderRequired",
    "SandboxDied",
    "ScorerError",
    "ACTION_STATUSES",
    "classify_exception",
    "parse_actions",
    "parse_xml_actions",
    "DialectError",
    "CriuDockerProvider",
    "DockerLocalProvider",
    "ExternalProvider",
    "GcReport",
    "SandboxSpec",
    "__version__",
]
