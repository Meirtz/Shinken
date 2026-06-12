"""trycua/cua backend — drive the Shinken operation layer over cua's computer interface.

cua (`github.com/trycua/cua`) ships ``BaseComputerInterface``: an async verb surface
(``left_click``/``right_click``/``double_click``/``move_cursor``/``type_text``/``press_key``/
``hotkey``/``scroll``/``screenshot``/``get_screen_size``/``run_command``/
``get_accessibility_tree``/``copy_to_clipboard``/``set_clipboard``) that its per-OS handler
factory backs with native drivers or VNC. :class:`CuaSandbox` adapts that surface to the
duck-typed Sandbox the Shinken operation layer drives; :class:`CuaBackendProvider` wraps it
in the provider lifecycle so ``provider.session()`` works.

What carries over (honest capabilities): pointer/keyboard/scroll/screenshot/exec/clipboard
and — when the cua interface exposes ``get_accessibility_tree`` — a structured ``observe``
returning ``tree_text`` (cua's a11y tree serialized; element_ref actions need Shinken's own
guest engine and are NOT advertised). What does NOT: checkpoint/fork/resume — cua has no
content-addressed snapshot tier, so ``supports_fork=False`` and the inherited
``checkpoint``/``resume`` raise ``UnsupportedProviderOperation``.

``interface_factory(spec) -> BaseComputerInterface`` is injectable: the default builds a real
cua ``Computer``; tests/examples pass a protocol-faithful in-memory interface so the wiring
runs with no cua install and no VM.
"""

from __future__ import annotations

import asyncio
import os
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

# The verbs this adapter actuates over a cua interface (kept in sync with CuaSandbox below).
_CUA_VERBS = [
    "click",
    "type_text",
    "key",
    "scroll",
    "screenshot",
    "exec",
    "clipboard_get",
    "clipboard_set",
]


def _computer_kwargs(spec: SandboxSpec | None) -> dict:
    """Resolve the real cua ``Computer()`` kwargs from the spec metadata + ``SHINKEN_CUA_*``
    env. The ctor's own default provider is the macOS lume daemon, which can never serve a
    Linux sandbox — cua's docs say Linux runs over the docker provider, so that is our
    Linux default. Knobs: metadata ``provider_type``/``name``/``image``/``api_key``/
    ``use_host_computer_server``, or env ``SHINKEN_CUA_PROVIDER``/``SHINKEN_CUA_NAME``/
    ``SHINKEN_CUA_IMAGE``/``CUA_API_KEY``/``SHINKEN_CUA_HOST_SERVER=1``."""
    os_type = (spec.os if spec else "linux") or "linux"
    meta = (spec.metadata if spec else None) or {}
    if meta.get("use_host_computer_server") or os.environ.get("SHINKEN_CUA_HOST_SERVER") == "1":
        return {"os_type": os_type, "use_host_computer_server": True}
    kwargs: dict[str, Any] = {
        "os_type": os_type,
        "provider_type": (
            meta.get("provider_type")
            or os.environ.get("SHINKEN_CUA_PROVIDER")
            or ("docker" if os_type == "linux" else "lume")
        ),
    }
    for key, env_key in (
        ("name", "SHINKEN_CUA_NAME"),
        ("image", "SHINKEN_CUA_IMAGE"),
        ("api_key", "CUA_API_KEY"),
    ):
        val = meta.get(key) or os.environ.get(env_key)
        if val:
            kwargs[key] = val
    return kwargs


def _default_interface_factory(spec: SandboxSpec | None):  # pragma: no cover - needs cua + VM
    """Build a real cua computer interface. Imported lazily so the package never hard-depends
    on cua. Returns an object with cua's async ``BaseComputerInterface`` surface."""
    try:
        from computer import Computer  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "the cua backend's default factory needs the `cua-computer` package "
            "(pip install cua-computer) and a reachable cua sandbox; pass interface_factory= "
            "to inject your own cua BaseComputerInterface"
        ) from exc
    computer = Computer(**_computer_kwargs(spec))
    loop = asyncio.new_event_loop()
    loop.run_until_complete(computer.run())
    return computer.interface, computer, loop


class CuaSandbox:
    """Duck-typed :class:`~shinken.client.Sandbox` over a cua ``BaseComputerInterface``.

    Implements the method surface Shinken consumers call (the operator loop, model adapters,
    the gym's screenshot/exec/observe). All cua methods are async; this runs them on one
    private event loop so the sync facade matches ``shinken.client.Sandbox``."""

    def __init__(
        self,
        interface: Any,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        platform: str = "linux",
        owns_loop: bool = False,
    ) -> None:
        self._if = interface
        self._loop = loop or asyncio.new_event_loop()
        self._owns_loop = owns_loop or loop is None
        self._platform = platform
        self._closed = False
        self._provider = None
        self._handle = None
        self._has_a11y = hasattr(interface, "get_accessibility_tree")

    # -- run a cua coroutine synchronously -------------------------------------------
    def _run(self, coro: Any) -> Any:
        if self._closed:
            raise RuntimeError("CuaSandbox is closed")
        return self._loop.run_until_complete(coro)

    # -- capability envelope (honest: only what cua serves) --------------------------
    @property
    def capabilities(self) -> Capabilities:
        verbs = list(_CUA_VERBS)
        obs = ["screenshot"]
        if self._has_a11y:
            verbs.append("observe")
            obs.append("tree")
        return Capabilities(
            schema_version=1,
            verbs=verbs,
            targets=["point_px"],  # no element_ref: that needs Shinken's guest engine
            observation_types=obs,
            structured_observation=self._has_a11y,
        )

    @property
    def platform(self) -> str:
        return self._platform

    def _set_provider_context(self, provider: Any, handle: Any) -> None:
        self._provider, self._handle = provider, handle

    # -- observation -----------------------------------------------------------------
    def ping(self) -> float:
        t0 = time.perf_counter()
        self._run(self._if.get_screen_size())
        return time.perf_counter() - t0

    def screen_size(self) -> dict:
        size = self._run(self._if.get_screen_size())
        w, h = _wh(size)
        return {"w": w, "h": h}

    def screenshot(self, scope: str = "screen", **_kw: Any) -> dict:
        png = self._run(self._if.screenshot())
        if not isinstance(png, bytes | bytearray):
            png = bytes(png)
        try:
            w, h = _wh(self._run(self._if.get_screen_size()))
        except Exception:  # noqa: BLE001 - size is best-effort metadata
            w = h = None
        return {
            "type": "observation",
            "png": bytes(png),
            "w": w,
            "h": h,
            "scope": scope,
            "format": "png",
        }

    def observe(self, structured: bool = False, source: Any = None, **_kw: Any) -> dict:
        if not structured:
            shot = self.screenshot()
            return {
                "type": "observation",
                "tree": "full",
                "image": {"w": shot["w"], "h": shot["h"], "scope": "screen"},
                "png": shot["png"],
            }
        if not self._has_a11y:
            return {
                "type": "observation",
                "tree": "none",
                "available": False,
                "detail": "cua interface exposes no get_accessibility_tree",
            }
        if self._closed:
            raise RuntimeError("CuaSandbox is closed")
        try:
            tree = self._run(self._if.get_accessibility_tree())
        except Exception as exc:  # noqa: BLE001 - the real cua RAISES when it can't serve the
            # tree (the method exists on every interface, so hasattr can never catch this)
            return {
                "type": "observation",
                "tree": "none",
                "available": False,
                "detail": f"cua a11y tree unavailable: {exc}",
                "error": type(exc).__name__,
            }
        if isinstance(tree, dict) and "tree" in tree:
            tree = tree["tree"]  # the real cua wraps the payload in a {"success", "tree"} result
        return {
            "type": "observation",
            "tree": "full",
            "available": True,
            "tree_text": _serialize_tree(tree),
            "elements": [],
            "raw": tree,
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
        if x is None or y is None:
            raise ValueError("cua backend click requires pixel x,y (no element_ref support)")
        if count >= 2 and hasattr(self._if, "double_click"):
            self._run(self._if.double_click(x, y))
        elif button == "right":
            self._run(self._if.right_click(x, y))
        else:
            self._run(self._if.left_click(x, y))
        return {"ok": True}

    def type_text(self, text: str, **_kw: Any) -> dict:
        self._run(self._if.type_text(text))
        return {"ok": True}

    def key(self, keys: str, **_kw: Any) -> dict:
        parts = [p for p in keys.replace(" ", "").split("+") if p]
        if len(parts) > 1 and hasattr(self._if, "hotkey"):
            self._run(self._if.hotkey(*parts))
        else:
            self._run(self._if.press_key(parts[-1] if parts else keys))
        return {"ok": True}

    def scroll(self, dx: int = 0, dy: int = 0, **_kw: Any) -> dict:
        self._run(self._if.scroll(dx, dy))
        return {"ok": True}

    def exec(
        self,
        argv: list[str] | None = None,
        *,
        shell: str | None = None,
        timeout: float | None = None,
        **_kw: Any,
    ) -> dict:
        cmd = shell if shell is not None else shlex.join(argv or [])
        res = self._run(self._if.run_command(cmd))
        return _command_result(res)

    def clipboard_get(self) -> str:
        return self._run(self._if.copy_to_clipboard())

    def clipboard_set(self, text: str, **_kw: Any) -> dict:
        self._run(self._if.set_clipboard(text))
        return {"ok": True}

    # -- runtime-state family: delegate to the provider so a backend with no snapshot
    # tier fails LOUDLY (UnsupportedProviderOperation), never silently or with AttributeError.
    def checkpoint(self, name: str | None = None, **_kw: Any) -> Any:
        return self._provider.checkpoint(self._handle, name=name)

    def spawn(self, **_kw: Any) -> Any:
        return self._provider.resume(self._handle)

    def spawn_many(self, n: int, **_kw: Any) -> Any:
        return self._provider.resume(self._handle)

    # -- lifecycle -------------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_loop:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass


def _wh(size: Any) -> tuple[int | None, int | None]:
    """cua get_screen_size returns {"width","height"} (or a (w,h) tuple) — normalize."""
    if isinstance(size, dict):
        return size.get("width") or size.get("w"), size.get("height") or size.get("h")
    if isinstance(size, tuple | list) and len(size) >= 2:
        return size[0], size[1]
    return None, None


def _command_result(res: Any) -> dict:
    """Normalize cua's CommandResult (stdout/stderr/returncode) to the Shinken exec shape."""
    if isinstance(res, dict):
        out, err = res.get("stdout", ""), res.get("stderr", "")
        rc = res.get("returncode", res.get("exit_code", 0))
    else:
        out = getattr(res, "stdout", "")
        err = getattr(res, "stderr", "")
        rc = getattr(res, "returncode", getattr(res, "exit_code", 0))
    return {
        "stdout": out or "",
        "stderr": err or "",
        "exit_code": rc,
        "returncode": rc,
        "timed_out": False,
    }


def _serialize_tree(tree: Any, depth: int = 0, lines: list[str] | None = None) -> str:
    """Best-effort legible serialization of cua's a11y tree (role/name/children dicts)."""
    if lines is None:
        lines = []
    if isinstance(tree, dict):
        role = tree.get("role") or tree.get("type") or "node"
        name = tree.get("name") or tree.get("title") or tree.get("value") or ""
        lines.append(f"{'  ' * depth}{role}" + (f' "{name}"' if name else ""))
        for child in tree.get("children", []) or []:
            _serialize_tree(child, depth + 1, lines)
    elif isinstance(tree, list | tuple):
        for child in tree:
            _serialize_tree(child, depth, lines)
    elif tree:
        lines.append(f"{'  ' * depth}{tree}")
    return "\n".join(lines[:400])  # line budget, like the guest engine


class CuaBackendProvider(SandboxProvider):
    """Operation-layer backend: drive Shinken verbs over a cua computer interface.

    No checkpoint/fork tier — ``supports_fork=False``; the inherited ``checkpoint``/``resume``
    raise ``UnsupportedProviderOperation`` so fork-native consumers degrade loudly."""

    capabilities = ProviderCapabilities(
        name="cua",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=False,
        supports_fork=False,
        reset_strategy="recreate",
        isolation="provider_managed",
        transport="provider_managed",
        display="provider_managed",
        tier="external-backend",
        notes=(
            "trycua/cua computer interface; pointer/keyboard/scroll/screenshot/exec"
            " + a11y-tree observe; no fork tier",
        ),
    )

    def __init__(
        self,
        interface_factory: Callable[..., Any] = _default_interface_factory,
        *,
        platform: str = "linux",
    ) -> None:
        self._factory = interface_factory
        self._platform = platform
        self._live: dict[str, tuple[Any, Any, asyncio.AbstractEventLoop | None]] = {}

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        built = self._factory(spec)
        # factory may return just the interface, or (interface, owner, loop)
        if isinstance(built, tuple):
            interface, owner, loop = (list(built) + [None, None])[:3]
        else:
            interface, owner, loop = built, None, None
        sid = f"cua-{uuid.uuid4().hex[:12]}"
        self._live[sid] = (interface, owner, loop)
        return SandboxHandle(
            provider="cua",
            sandbox_id=sid,
            addr="cua://local",
            created_at=time.time(),
            metadata={"os": self._platform},
        )

    def connect(self, handle: SandboxHandle, **_connect_kwargs: Any) -> CuaSandbox:  # type: ignore[override]
        interface, _owner, loop = self._live[handle.sandbox_id]
        env = CuaSandbox(interface, loop=loop, platform=self._platform, owns_loop=loop is None)
        env._set_provider_context(self, handle)
        return env

    def destroy(self, handle: SandboxHandle) -> None:
        entry = self._live.pop(handle.sandbox_id, None)
        if entry is None:
            return
        _interface, owner, loop = entry
        if owner is not None and hasattr(owner, "stop") and loop is not None:
            try:  # pragma: no cover - real cua teardown
                loop.run_until_complete(owner.stop())
            except Exception:  # noqa: BLE001
                pass
