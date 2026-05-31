"""Docker-backed local sandbox provider."""

from __future__ import annotations

import json
import re
import secrets
import socket
import struct
import subprocess
import time
import uuid
import zlib
from dataclasses import replace
from typing import Any

from .base import (
    ProviderCapabilities,
    ProviderError,
    SandboxHandle,
    SandboxHealth,
    SandboxProvider,
    SandboxSpec,
)


def _free_port(host: str = "127.0.0.1") -> int:
    sock = socket.socket()
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


# Mask secret env values when rendering a command for an error/log (#153). Matches a
# `KEY=VALUE` arg whose KEY ends in TOKEN/SECRET/PASSWORD/KEY (e.g. SHINKEND_TOKEN).
_SECRET_ENV_RE = re.compile(r"^([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|KEY))=.+$")


def _redact_cmd(cmd: list[str]) -> str:
    """Render a Docker command for error messages with secret env values masked (#153)
    — e.g. ``SHINKEND_TOKEN=deadbeef`` → ``SHINKEND_TOKEN=***`` — so a failing invocation
    never echoes the runtime token into an exception or log."""
    return " ".join(_SECRET_ENV_RE.sub(r"\1=***", arg) for arg in cmd)


def _run(cmd: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ProviderError(f"command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        raise ProviderError(f"{_redact_cmd(cmd)} failed: {stderr}") from exc


class DockerLocalProvider(SandboxProvider):
    """Run the reference Linux sandbox image with the Docker CLI."""

    capabilities = ProviderCapabilities(
        name="docker-local",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=False,
        supports_fork=False,
        supports_gpu=False,
        supports_vsock=False,
        supports_egress_policy=False,
        reset_strategy="recreate",
        max_sessions=None,
        notes=("Local/CI compatibility provider; Docker is not the production isolation tier.",),
    )

    def __init__(
        self,
        image: str = "shinken/sandbox-linux",
        *,
        docker_bin: str = "docker",
        host: str = "127.0.0.1",
        name_prefix: str = "shinken-local",
        startup_timeout: float = 45.0,
    ) -> None:
        self.image = image
        self.docker_bin = docker_bin
        self.host = host
        self.name_prefix = name_prefix
        self.startup_timeout = startup_timeout

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        spec = replace(spec, image=spec.image or self.image) if spec is not None else SandboxSpec(
            image=self.image
        )
        token = secrets.token_hex(16)
        port = _free_port(self.host)
        name = f"{self.name_prefix}-{uuid.uuid4().hex[:10]}"
        cmd = [
            self.docker_bin,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            "shinken.provider=docker-local",
            "--label",
            f"shinken.name_prefix={self.name_prefix}",
            "-p",
            f"{self.host}:{port}:8765",
            "-e",
            # Dev-only: the token is delivered via env, so it is readable by any process
            # in the guest (and from /proc) — not a real boundary (#153). It is redacted
            # from provider errors/logs; a faithful boundary needs fd/mounted-secret
            # delivery plus the server-side Action Gateway (D6).
            f"SHINKEND_TOKEN={token}",
            "-e",
            f"SCREEN_GEOMETRY={spec.screen_geometry}",
        ]
        if spec.memory:
            cmd += ["--memory", spec.memory]
        if spec.cpus is not None:
            cmd += ["--cpus", str(spec.cpus)]
        if spec.pids_limit is not None:
            cmd += ["--pids-limit", str(spec.pids_limit)]
        if spec.shm_size:
            cmd += ["--shm-size", spec.shm_size]
        cmd.append(spec.image or self.image)

        created_at = time.time()
        result = _run(cmd, timeout=self.startup_timeout)
        handle = SandboxHandle(
            provider=self.capabilities.name,
            sandbox_id=name,
            addr=f"{self.host}:{port}",
            token=token,
            created_at=created_at,
            metadata={
                **spec.metadata,
                "container_id": result.stdout.strip(),
                "image": spec.image or self.image,
                "port": port,
                "screen_geometry": spec.screen_geometry,
                "resources": {
                    "memory": spec.memory,
                    "cpus": spec.cpus,
                    "pids_limit": spec.pids_limit,
                    "shm_size": spec.shm_size,
                },
            },
        )
        try:
            self._wait_ready(handle)
        except Exception:
            self.destroy(handle)
            raise
        return handle

    def _wait_ready(self, handle: SandboxHandle) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                health = self.health(handle)
                if health.ready:
                    return
                last_error = ProviderError(health.detail)
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        raise ProviderError(f"timed out waiting for ready sandbox {handle.addr}: {last_error}")

    def health(self, handle: SandboxHandle) -> SandboxHealth:
        started = time.perf_counter()
        try:
            env = self.connect(handle)
            try:
                rtt_ms = env.ping() * 1000.0
                shot_started = time.perf_counter()
                shot = env.screenshot()
                screenshot_ms = (time.perf_counter() - shot_started) * 1000.0
            finally:
                env.close()
        except Exception as exc:
            return SandboxHealth(
                ok=False,
                ready=False,
                detail=str(exc),
                rss_bytes=self._container_rss(handle),
            )

        non_black = _png_has_non_black_pixel(shot["png"])
        ready = non_black is not False
        return SandboxHealth(
            ok=ready,
            ready=ready,
            detail="ready" if ready else "screenshot is all black",
            rtt_ms=rtt_ms,
            screenshot_ms=screenshot_ms,
            screenshot_bytes=len(shot["png"]),
            rss_bytes=self._container_rss(handle),
            metadata={
                "readiness_ms": (time.perf_counter() - started) * 1000.0,
                "screenshot_non_black": non_black,
            },
        )

    def reset(self, handle: SandboxHandle) -> SandboxHandle:
        spec = SandboxSpec(
            image=str(handle.metadata.get("image") or self.image),
            screen_geometry=str(handle.metadata.get("screen_geometry") or "1280x800x24"),
            metadata={
                k: v
                for k, v in handle.metadata.items()
                if k not in {"container_id", "port"}
            },
        )
        resources: dict[str, Any] = dict(handle.metadata.get("resources") or {})
        spec.memory = resources.get("memory")
        spec.cpus = resources.get("cpus")
        spec.pids_limit = resources.get("pids_limit")
        spec.shm_size = resources.get("shm_size")
        self.destroy(handle)
        return self.create(spec)

    def destroy(self, handle: SandboxHandle) -> None:
        try:
            subprocess.run(
                [self.docker_bin, "rm", "-f", handle.sandbox_id],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"timed out destroying sandbox {handle.sandbox_id}") from exc
        handle.metadata["destroyed"] = True

    def cleanup_orphans(self) -> int:
        # Select strictly by the labels we stamp at create time (#157). Docker's `name`
        # filter is substring-based, not the anchored regex `name=^prefix-` implies, so
        # it could miss intended containers (or match unintended ones) and leave orphans;
        # the two labels already identify our containers precisely.
        result = _run(
            [
                self.docker_bin,
                "ps",
                "-aq",
                "--filter",
                "label=shinken.provider=docker-local",
                "--filter",
                f"label=shinken.name_prefix={self.name_prefix}",
            ]
        )
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not ids:
            return 0
        _run([self.docker_bin, "rm", "-f", *ids], timeout=30.0)
        return len(ids)

    def _container_rss(self, handle: SandboxHandle) -> int | None:
        try:
            result = _run(
                [
                    self.docker_bin,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    handle.sandbox_id,
                ],
                timeout=10.0,
            )
        except ProviderError:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return _parse_mem_usage(data.get("MemUsage", ""))


def _parse_mem_usage(value: str) -> int | None:
    used = value.split("/", 1)[0].strip()
    if not used:
        return None
    units = {
        "b": 1,
        "kb": 1000,
        "kib": 1024,
        "mb": 1000**2,
        "mib": 1024**2,
        "gb": 1000**3,
        "gib": 1024**3,
    }
    number = "".join(ch for ch in used if ch.isdigit() or ch == ".")
    suffix = used[len(number) :].strip().lower()
    if not number:
        return None
    return int(float(number) * units.get(suffix, 1))


def _png_has_non_black_pixel(data: bytes) -> bool | None:
    """Best-effort PNG readiness check without adding an image dependency."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
    if not width or not height or bit_depth != 8 or color_type not in {0, 2, 6}:
        return None
    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        return None
    prev = bytearray(stride)
    offset = 0
    for _ in range(height):
        filter_type = raw[offset]
        offset += 1
        row = bytearray(raw[offset : offset + stride])
        offset += stride
        _unfilter(row, prev, channels, filter_type)
        if color_type == 0:
            if any(row):
                return True
        else:
            for idx in range(0, len(row), channels):
                if any(row[idx : idx + 3]):
                    return True
        prev = row
    return False


def _unfilter(row: bytearray, prev: bytearray, bpp: int, filter_type: int) -> None:
    for idx, value in enumerate(row):
        left = row[idx - bpp] if idx >= bpp else 0
        up = prev[idx]
        upper_left = prev[idx - bpp] if idx >= bpp else 0
        if filter_type == 1:
            row[idx] = (value + left) & 0xFF
        elif filter_type == 2:
            row[idx] = (value + up) & 0xFF
        elif filter_type == 3:
            row[idx] = (value + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            row[idx] = (value + _paeth(left, up, upper_left)) & 0xFF


def _paeth(left: int, up: int, upper_left: int) -> int:
    p = left + up - upper_left
    pa = abs(p - left)
    pb = abs(p - up)
    pc = abs(p - upper_left)
    if pa <= pb and pa <= pc:
        return left
    if pb <= pc:
        return up
    return upper_left
