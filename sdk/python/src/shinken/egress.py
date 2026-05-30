"""Task egress proxy configuration surface (#93) — provider-neutral and secret-safe.

An optional proxy applied **only** to sandbox/task egress for Shinken-owned smoke/eval
runs — never to client↔control-plane or sandbox-lifecycle traffic. Concrete proxy
details (host/user/password) live in operator-managed env / ignored local paths; this
surface only reports whether a proxy was *requested / skipped / failed* and never logs
host, user, or password values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

#: Env var holding the task-egress proxy URL, e.g. ``http[s]://[user:pass@]host:port``.
TASK_EGRESS_PROXY_ENV = "SHK_TASK_EGRESS_PROXY"


@dataclass
class ProxyConfig:
    """A resolved task-egress proxy. ``url`` may embed credentials — never log it
    directly; use :func:`redact_proxy_url` or :func:`proxy_status`."""

    url: str
    scheme: str

    def as_handlers(self) -> dict:
        """Proxy mapping for a urllib ``ProxyHandler`` — task egress only."""
        return {"http": self.url, "https": self.url}


def resolve_task_egress_proxy(env: dict | None = None) -> ProxyConfig | None:
    """Read the task-egress proxy from the environment, or ``None`` if unset."""
    environ = os.environ if env is None else env
    url = (environ.get(TASK_EGRESS_PROXY_ENV) or "").strip()
    if not url:
        return None
    return ProxyConfig(url=url, scheme=urlparse(url).scheme or "http")


def redact_proxy_url(url: str) -> str:
    """Mask everything but the scheme — credentials and host are never surfaced."""
    return f"{urlparse(url).scheme or 'http'}://<redacted>"


def proxy_status(cfg: ProxyConfig | None, *, failed: bool = False) -> dict:
    """Smoke metadata describing task-egress proxy setup, with **no** host/user/password.

    ``task_egress_proxy`` is ``requested`` (a proxy was configured), ``skipped`` (none
    configured), or ``failed`` (setup error). Only the scheme is ever included."""
    if failed:
        state = "failed"
    elif cfg is None:
        state = "skipped"
    else:
        state = "requested"
    status = {"task_egress_proxy": state, "applied_to": "task_egress"}
    if cfg is not None:
        status["scheme"] = cfg.scheme
    return status
