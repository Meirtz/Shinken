"""e2b-desktop backend — drive the Shinken operation layer over an E2B cloud desktop sandbox.

`e2b-desktop` (`github.com/e2b-dev/desktop`) is a cloud Linux desktop with a thin
pixel-driver surface: ``screenshot``, ``left_click``/``right_click``/``double_click``/
``move_mouse``/``scroll``/``drag`` (xdotool under the hood), ``write``/``press`` (xdotool key
syntax), ``get_screen_size``, ``launch``/``open``, and ``commands.run`` (a real shell).
:class:`E2bDesktopSandbox` adapts that surface to the duck-typed Sandbox the operation layer
drives; :class:`E2bDesktopBackend` wraps it in the provider lifecycle.

Honest capabilities: pointer/keyboard/scroll/screenshot/**exec** (e2b ships a shell, unlike
the AX-only backends) + ``launch_app``. What it does NOT have: a structured observation tier
(no accessibility tree → ``structured_observation=False``, pixel-only ``observe``) and no
Shinken-style content-addressed fork (``supports_fork=False`` → ``checkpoint``/``spawn`` raise
``UnsupportedProviderOperation``; e2b's own cloud pause/resume is a different, 1:1 tier).

``sandbox_factory(spec) -> e2b_desktop.Sandbox`` is injectable: the default constructs a real
E2B sandbox (needs ``E2B_API_KEY`` + network); tests/examples pass a protocol-faithful
in-memory peer so the wiring runs with no key and no cloud.
"""

from __future__ import annotations

import shlex
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..client import Capabilities
from ..providers.base import (
    ProviderCapabilities,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)

_E2B_VERBS = ["click", "type_text", "key", "scroll", "screenshot", "exec", "launch_app"]


def _default_sandbox_factory(spec: SandboxSpec | None):  # pragma: no cover - needs E2B + key
    try:
        from e2b_desktop import Sandbox  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "the e2b-desktop backend's default factory needs the `e2b-desktop` package "
            "(pip install e2b-desktop) and an E2B_API_KEY; pass sandbox_factory= to inject "
            "your own e2b_desktop.Sandbox"
        ) from exc
    return Sandbox()


class E2bDesktopSandbox:
    """Duck-typed :class:`~shinken.client.Sandbox` over an e2b-desktop ``Sandbox`` (pixel-only)."""

    def __init__(self, sandbox: Any, *, platform: str = "linux") -> None:
        self._sb = sandbox
        self._platform = platform
        self._closed = False
        self._provider = None
        self._handle = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            schema_version=1,
            verbs=list(_E2B_VERBS),
            targets=["point_px"],  # no a11y tree → no element_ref
            observation_types=["screenshot"],
            structured_observation=False,
        )

    @property
    def platform(self) -> str:
        return self._platform

    def _set_provider_context(self, provider: Any, handle: Any) -> None:
        self._provider, self._handle = provider, handle

    def _guard(self) -> None:
        if self._closed:
            raise RuntimeError("E2bDesktopSandbox is closed")

    # -- observation (pixel-only) ----------------------------------------------------
    def ping(self) -> float:
        self._guard()
        t0 = time.perf_counter()
        self._sb.get_screen_size()
        return time.perf_counter() - t0

    def screen_size(self) -> dict:
        self._guard()
        w, h = self._sb.get_screen_size()
        return {"w": w, "h": h}

    def screenshot(self, scope: str = "screen", **_kw: Any) -> dict:
        self._guard()
        png = self._sb.screenshot("bytes")
        w = h = None
        try:
            w, h = self._sb.get_screen_size()
        except Exception:  # noqa: BLE001 - size is best-effort metadata
            pass
        return {
            "type": "observation",
            "png": bytes(png),
            "w": w,
            "h": h,
            "scope": scope,
            "format": "png",
        }

    def observe(self, structured: bool = False, source: Any = None, **_kw: Any) -> dict:
        if structured:  # e2b has no accessibility tree — degrade honestly, never fake one
            return {
                "type": "observation",
                "tree": "none",
                "available": False,
                "detail": "e2b-desktop is pixel-only (no accessibility tree)",
            }
        shot = self.screenshot()
        return {
            "type": "observation",
            "tree": "full",
            "image": {"w": shot["w"], "h": shot["h"], "scope": "screen"},
            "png": shot["png"],
        }

    # -- actions ---------------------------------------------------------------------
    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        button: str = "left",
        count: int = 1,
        **_kw: Any,
    ) -> dict:
        self._guard()
        if x is None or y is None:
            raise ValueError("e2b-desktop click requires pixel x,y (no element_ref support)")
        if count >= 2:
            self._sb.double_click(x, y)
        elif button == "right":
            self._sb.right_click(x, y)
        elif button == "middle":
            self._sb.middle_click(x, y)
        else:
            self._sb.left_click(x, y)
        return {"ok": True}

    def type_text(self, text: str, **_kw: Any) -> dict:
        self._guard()
        self._sb.write(text)
        return {"ok": True}

    def key(self, keys: str, **_kw: Any) -> dict:
        self._guard()
        parts = [p for p in keys.replace(" ", "").split("+") if p]
        self._sb.press(parts if len(parts) > 1 else (parts[0] if parts else keys))
        return {"ok": True}

    def scroll(self, dx: int = 0, dy: int = 0, **_kw: Any) -> dict:
        self._guard()
        direction = "down" if dy >= 0 else "up"
        self._sb.scroll(direction=direction, amount=max(1, abs(dy) or abs(dx) or 1))
        return {"ok": True}

    def drag(
        self,
        target: Any = None,
        to: Any = None,
        *,
        x: int | None = None,
        y: int | None = None,
        to_x: int | None = None,
        to_y: int | None = None,
        **_kw: Any,
    ) -> dict:
        self._guard()
        sx, sy = (x, y) if x is not None else _xy(target)
        ex, ey = (to_x, to_y) if to_x is not None else _xy(to)
        self._sb.drag((sx, sy), (ex, ey))
        return {"ok": True}

    def exec(
        self,
        argv: list[str] | None = None,
        *,
        shell: str | None = None,
        timeout: float | None = None,
        **_kw: Any,
    ) -> dict:
        self._guard()
        cmd = shell if shell is not None else shlex.join(argv or [])
        res = self._sb.commands.run(cmd)
        rc = getattr(res, "exit_code", 0)
        return {
            "stdout": getattr(res, "stdout", "") or "",
            "stderr": getattr(res, "stderr", "") or "",
            "exit_code": rc,
            "returncode": rc,
            "timed_out": False,
        }

    def launch_app(self, app: str, args: list[str] | None = None, **_kw: Any) -> dict:
        self._guard()
        self._sb.launch(app)
        return {"ok": True}

    # -- runtime-state family: loud degrade (e2b cloud pause/resume is a different tier) --
    def checkpoint(self, name: str | None = None, **_kw: Any) -> Any:
        return self._provider.checkpoint(self._handle, name=name)

    def spawn(self, **_kw: Any) -> Any:
        return self._provider.resume(self._handle)

    def close(self) -> None:
        self._closed = True


def _xy(t: Any) -> tuple[int, int]:
    if isinstance(t, dict):
        return int(t["x"]), int(t["y"])
    if isinstance(t, tuple | list) and len(t) >= 2:
        return int(t[0]), int(t[1])
    raise ValueError(f"cannot read an (x, y) from {t!r}")


class E2bDesktopBackend(SandboxProvider):
    """Operation-layer backend over an E2B cloud desktop. Pixel-only, has a shell (exec),
    no Shinken fork tier — ``supports_fork=False``."""

    capabilities = ProviderCapabilities(
        name="e2b-desktop",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=False,
        supports_fork=False,
        reset_strategy="recreate",
        isolation="provider_managed",
        tier="external-backend",
        notes=(
            "E2B cloud desktop: pixel pointer/keyboard/scroll/screenshot + shell exec +"
            " launch_app; no accessibility tree, no Shinken fork tier",
        ),
    )

    def __init__(
        self,
        sandbox_factory: Callable[..., Any] = _default_sandbox_factory,
        *,
        platform: str = "linux",
    ) -> None:
        self._factory = sandbox_factory
        self._platform = platform
        self._live: dict[str, Any] = {}

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        sb = self._factory(spec)
        sid = getattr(sb, "sandbox_id", None) or f"e2b-{uuid.uuid4().hex[:12]}"
        self._live[sid] = sb
        return SandboxHandle(
            provider="e2b-desktop",
            sandbox_id=sid,
            addr="e2b://cloud",
            created_at=time.time(),
            metadata={"os": self._platform},
        )

    def connect(self, handle: SandboxHandle, **_kw: Any) -> E2bDesktopSandbox:  # type: ignore[override]
        env = E2bDesktopSandbox(self._live[handle.sandbox_id], platform=self._platform)
        env._set_provider_context(self, handle)
        return env

    def destroy(self, handle: SandboxHandle) -> None:
        sb = self._live.pop(handle.sandbox_id, None)
        if sb is not None and hasattr(sb, "kill"):
            try:  # pragma: no cover - real cloud teardown
                sb.kill()
            except Exception:  # noqa: BLE001
                pass
