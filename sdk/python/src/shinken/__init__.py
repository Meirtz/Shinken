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

from .client import AsyncSandbox, Capabilities, Sandbox, aconnect, connect
from .gateway import CapabilityDenied
from .skn import Recorder, Replay

__version__ = "0.0.0"
__all__ = [
    "connect",
    "aconnect",
    "Sandbox",
    "AsyncSandbox",
    "Capabilities",
    "CapabilityDenied",
    "Recorder",
    "Replay",
    "__version__",
]
