"""Inject ``shinkend`` into a provider-backed sandbox at runtime, via a user-chosen method.

Getting ``shinkend`` *into* a sandbox is substrate-specific: Docker has ``docker cp`` +
``docker exec``, a VM has ssh, an OSWorld-style image has its controller's ``/execute``. This
module abstracts that as pluggable **Injectors** selected **by name** (same register/get
pattern as providers/workloads). The caller picks the method **explicitly**; if that method
cannot reach the sandbox (missing target fields, command/transport failure), injection raises
:class:`InjectionError` — there is **no silent fallback and no guessing**.

``shinkend`` is configured purely by env (``SHINKEND_ADDR``, ``SHINKEND_TOKEN``); a non-loopback
bind requires the token. So to be reachable from outside the sandbox we bind ``0.0.0.0:<port>``
with a token (generated if not supplied) and return ``(addr, token)`` for the SDK to connect.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

DEFAULT_PORT = 8765
REMOTE_BIN = "/usr/local/bin/shinkend"


class InjectionError(RuntimeError):
    """A chosen injection method could not place/start shinkend in the sandbox."""


@dataclass
class InjectionTarget:
    """What an injector needs to reach one sandbox. Fields are method-specific; each injector
    validates the ones it uses and raises :class:`InjectionError` if a required field is absent.

    ``token`` (auto-generated if None) gates a non-loopback bind so the in-sandbox shinkend is
    reachable; ``reachable_addr`` is the host ``host:port`` the SDK connects to when the
    substrate maps the in-sandbox port elsewhere (e.g. a published Docker port)."""

    port: int = DEFAULT_PORT
    token: str | None = None
    host: str = "127.0.0.1"
    reachable_addr: str | None = None
    # where to place the binary in the guest; must be writable+executable by the injecting
    # user (an OSWorld controller runs non-root → use e.g. /tmp/shinkend, not /usr/local/bin)
    remote_bin: str = REMOTE_BIN
    # docker
    container: str | None = None
    docker_bin: str = "docker"
    # ssh
    ssh_host: str | None = None
    ssh_port: int = 22
    ssh_user: str | None = None
    ssh_key: str | None = None
    # osworld controller
    controller_url: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class InjectionResult:
    addr: str  # host:port the SDK connects to
    token: str | None  # bearer token shinkend was started with (None => loopback, no auth)


@runtime_checkable
class Injector(Protocol):
    name: str

    def inject(self, target: InjectionTarget, binary: str, *, args: list[str] | None = None) -> str:
        """Copy ``binary`` into the sandbox and start it on ``target.port``; return the
        reachable ``host:port``. Raise :class:`InjectionError` if the method can't reach it."""
        ...


# --- monkeypatchable seams (tests stub these; live runs use the real implementations) ---
def _run(cmd: list[str], *, timeout: float = 60.0) -> str:
    """Run a subprocess; raise InjectionError on non-zero exit."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InjectionError(f"{cmd[0]!r} failed to launch: {exc}") from exc
    if proc.returncode != 0:
        joined = " ".join(shlex.quote(c) for c in cmd)
        raise InjectionError(
            f"command failed (rc={proc.returncode}): {joined}\n{proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def _controller_exec(url: str, command: str, *, timeout: float = 60.0) -> str:
    """Run a shell command via an OSWorld-style controller's ``/execute`` endpoint."""
    import json
    import urllib.request

    body = json.dumps({"command": ["bash", "-lc", command], "shell": False}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/execute", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-supplied URL)
            raw = resp.read().decode("utf-8", "replace")
    except OSError as exc:
        raise InjectionError(f"controller /execute failed at {url}: {exc}") from exc
    # The controller answers 200 even when the command itself fails; surface a non-zero exit
    # so a permission/path error isn't silently swallowed (no silent failures).
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and data.get("returncode") not in (0, None):
        detail = (data.get("error") or data.get("output") or "").strip()[:200]
        raise InjectionError(
            f"controller command failed (rc={data.get('returncode')}): {command[:80]} :: {detail}"
        )
    return raw


def _gen_token() -> str:
    return "shk_" + os.urandom(16).hex()


def _start_command(target: InjectionTarget, args: list[str] | None) -> str:
    """Shell snippet that starts shinkend bound for reachability (0.0.0.0 + token when set)."""
    host = "0.0.0.0" if target.token else "127.0.0.1"
    env = f"SHINKEND_ADDR={shlex.quote(f'{host}:{target.port}')}"
    if target.token:
        env += f" SHINKEND_TOKEN={shlex.quote(target.token)}"
    return f"{env} {shlex.quote(target.remote_bin)} {' '.join(args or [])}".rstrip()


def _nohup_bg(start_cmd: str) -> str:
    """Background a start command robustly. ``nohup VAR=val binary`` fails (nohup treats the
    assignment as the program name), so run it through a shell that parses the env."""
    return f"nohup sh -c {shlex.quote(start_cmd)} >/tmp/shinkend.log 2>&1 &"


def _addr(target: InjectionTarget) -> str:
    return target.reachable_addr or f"{target.host}:{target.port}"


class DockerExecInjector:
    """``docker cp`` the binary in, ``docker exec -d`` to start it. Needs ``target.container``."""

    name = "docker"

    def inject(self, target: InjectionTarget, binary: str, *, args: list[str] | None = None) -> str:
        if not target.container:
            raise InjectionError("docker injection requires target.container")
        db, rb = target.docker_bin, target.remote_bin
        _run([db, "cp", binary, f"{target.container}:{rb}"])
        _run([db, "exec", target.container, "chmod", "+x", rb])
        _run([db, "exec", "-d", target.container, "bash", "-lc", _start_command(target, args)])
        return _addr(target)


class SshInjector:
    """``scp`` the binary in, ``ssh`` to start it (``nohup … &``). Needs ``target.ssh_host``."""

    name = "ssh"

    def inject(self, target: InjectionTarget, binary: str, *, args: list[str] | None = None) -> str:
        if not target.ssh_host:
            raise InjectionError("ssh injection requires target.ssh_host")
        ident = ["-i", target.ssh_key] if target.ssh_key else []
        dest = f"{target.ssh_user + '@' if target.ssh_user else ''}{target.ssh_host}"
        rb = target.remote_bin
        _run(["scp", "-P", str(target.ssh_port), *ident, binary, f"{dest}:{rb}"])
        start = f"chmod +x {shlex.quote(rb)} && {_nohup_bg(_start_command(target, args))}"
        _run(["ssh", "-p", str(target.ssh_port), *ident, dest, start])
        return target.reachable_addr or f"{target.ssh_host}:{target.port}"


class OSWorldExecInjector:
    """Upload (base64-through-exec) + start via an OSWorld controller's ``/execute``. Needs
    ``target.controller_url`` — the single ``exec`` primitive is enough (no upload endpoint)."""

    name = "osworld-exec"

    #: Base64 chars per ``/execute`` call. A multi-MB binary's base64 sent as a single
    #: command overruns ARG_MAX / the controller's request limit (HTTP 500), so we append
    #: fixed-size pieces to a remote file, then decode it once.
    CHUNK = 100_000

    def inject(self, target: InjectionTarget, binary: str, *, args: list[str] | None = None) -> str:
        if not target.controller_url:
            raise InjectionError("osworld-exec injection requires target.controller_url")
        import base64

        with open(binary, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        url, rb = target.controller_url, target.remote_bin
        remote_b64 = rb + ".b64"
        _controller_exec(url, f": > {shlex.quote(remote_b64)}")  # truncate any stale upload
        for i in range(0, len(b64), self.CHUNK):
            chunk = shlex.quote(b64[i : i + self.CHUNK])
            _controller_exec(url, f"printf %s {chunk} >> {shlex.quote(remote_b64)}")
        q_b64, q_rb = shlex.quote(remote_b64), shlex.quote(rb)
        _controller_exec(url, f"base64 -d {q_b64} > {q_rb} && rm -f {q_b64}")
        _controller_exec(url, f"chmod +x {q_rb}")
        _controller_exec(url, _nohup_bg(_start_command(target, args)))
        return _addr(target)


# --- registry (same shape as providers / workloads) ---
_REGISTRY: dict[str, Callable[[], Injector]] = {}


def register(name: str, factory: Callable[[], Injector]) -> None:
    _REGISTRY[name] = factory


def get(name: str) -> Injector:
    factory = _REGISTRY.get(name)
    if factory is None:
        raise InjectionError(f"unknown injection method {name!r}; available: {available()}")
    return factory()


def available() -> list[str]:
    return sorted(_REGISTRY)


register("docker", DockerExecInjector)
register("ssh", SshInjector)
register("osworld-exec", OSWorldExecInjector)


def inject_shinkend(
    target: InjectionTarget,
    binary: str,
    *,
    method: str,
    args: list[str] | None = None,
    require_token: bool = True,
) -> InjectionResult:
    """Place ``binary`` into the sandbox ``target`` and start it on ``target.port`` via the
    chosen ``method`` (an injector name). The user MUST pick ``method``; an unknown method or a
    method that cannot reach the sandbox raises :class:`InjectionError` (no silent fallback).

    A token is generated (so a reachable ``0.0.0.0`` bind is allowed) unless ``require_token``
    is False or ``target.token`` is preset. Returns the ``(addr, token)`` the SDK connects with."""
    if not os.path.exists(binary):
        raise InjectionError(f"shinkend binary not found: {binary}")
    if target.token is None and require_token:
        target.token = _gen_token()
    addr = get(method).inject(target, binary, args=args)
    return InjectionResult(addr=addr, token=target.token)
