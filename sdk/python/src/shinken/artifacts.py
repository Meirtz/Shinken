"""File / artifact transfer with content hashes and replay refs (#85).

File transfer is a core v0.0.1 semantic surface: an agent puts a file into the sandbox,
gets a result file back, and the run's ``.skn`` bundle must record *what* moved — by
content hash, path, and scope — without inlining large payloads into the JSON event log.

This module provides the durable contract: content-addressed hashing
(:func:`sha256_file`), a typed :class:`ArtifactRef` (path + sha256 + size + scope +
direction), and a :class:`LocalArtifactStore` — the M0 **co-located / reference**
transport, a directory standing in for the sandbox-accessible filesystem. Every transfer
is checksummed and every path is contained within the store root (no ``..`` escape, no
absolute paths). Over-the-wire transfer through ``shinkend`` is the Phase-1 follow-up;
the API, checksums, and replay refs land now.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_CHUNK = 1 << 20  # 1 MiB streaming reads — hash arbitrarily large files in bounded memory


class FileScopeError(ValueError):
    """A transfer path escapes the sandbox filesystem scope (absolute or ``..`` escape)."""


class HashMismatch(ValueError):
    """A fetched file's content hash does not match the expected digest."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    """Streaming SHA-256 of a file's contents (bounded memory for large artifacts)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    """A content-addressed reference to one transferred file — the ``.skn`` payload shape.

    Carries the hash/path/scope (never the bytes), so a replay records *what* moved
    without embedding large payloads in JSON."""

    path: str  # sandbox-relative path
    sha256: str
    size: int
    scope: str = "session"
    direction: str = "put"  # "put" (host → sandbox) or "get" (sandbox → host)

    def to_event(self) -> dict:
        return {
            "direction": self.direction,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "scope": self.scope,
        }


class LocalArtifactStore:
    """Co-located/reference transfer transport: a directory that stands in for the
    sandbox-accessible filesystem (M0). Containment is enforced on every access — a path
    that is absolute or escapes the root via ``..`` raises :class:`FileScopeError`."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, sandbox_path: str) -> Path:
        if os.path.isabs(sandbox_path):
            raise FileScopeError(f"absolute path not allowed in sandbox scope: {sandbox_path}")
        target = (self.root / sandbox_path).resolve()
        root = self.root.resolve()
        if root not in target.parents:
            raise FileScopeError(f"path escapes sandbox scope: {sandbox_path}")
        return target

    def put(
        self, local_path: str | os.PathLike, sandbox_path: str, scope: str = "session"
    ) -> ArtifactRef:
        """Copy a host file into the sandbox scope; return its content-addressed ref."""
        dst = self._resolve(sandbox_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dst)
        return ArtifactRef(sandbox_path, sha256_file(dst), dst.stat().st_size, scope, "put")

    def get(
        self,
        sandbox_path: str,
        local_path: str | os.PathLike,
        *,
        expect_sha256: str | None = None,
        scope: str = "session",
    ) -> ArtifactRef:
        """Copy a file out of the sandbox scope to the host, verifying its hash.

        Raises :class:`HashMismatch` if ``expect_sha256`` is given and does not match —
        the file is **not** written to ``local_path`` in that case."""
        src = self._resolve(sandbox_path)
        if not src.is_file():
            raise FileNotFoundError(f"no such artifact in sandbox scope: {sandbox_path}")
        digest = sha256_file(src)
        if expect_sha256 is not None and digest != expect_sha256:
            raise HashMismatch(
                f"{sandbox_path}: expected {expect_sha256[:12]}…, got {digest[:12]}…"
            )
        out = Path(local_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out)
        return ArtifactRef(sandbox_path, digest, src.stat().st_size, scope, "get")

    def read_bytes(self, sandbox_path: str) -> bytes:
        return self._resolve(sandbox_path).read_bytes()
