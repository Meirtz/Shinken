"""MCP computer-use backend — drive the Shinken operation layer over an MCP server that
exposes a desktop computer-use tool surface (e.g. iFurySt/open-codex-computer-use).

That server (Swift/Go, macOS AX / Windows UIA / Linux AT-SPI; **non-invasive**, Accessibility
-based) ships the closed "one enumerate + one observe + eight act" surface — 9 MCP tools:
``list_apps``, ``get_app_state`` (screenshot + numbered AX tree, once per turn), ``click``
(by ``element_index`` OR pixel ``x,y``), ``set_value``, ``type_text``, ``press_key``
(xdotool syntax — same key model Shinken uses), ``scroll``, ``drag``,
``perform_secondary_action``. :class:`McpComputerSandbox` maps the Shinken Sandbox verb
surface onto those tools over MCP ``tools/call``; :class:`McpComputerBackend` wraps it in the
provider lifecycle.

Honest capabilities: pointer/keyboard/scroll/screenshot + a **structured** ``observe``
(``get_app_state`` returns a numbered AX tree → this is one of the few backends that can
serve ``element_ref`` targets, since the tool clicks by ``element_index``). What it does NOT
have: a shell (``exec`` is unadvertised — these servers are non-invasive AX, no command
channel) and no snapshot tier (``supports_fork=False`` → ``checkpoint``/``spawn`` raise
``UnsupportedProviderOperation``).

Targeting: the server is **app-scoped** (every tool takes ``app``). The target app comes from
``SandboxSpec.metadata["app"]`` (or the ``app=`` provider kwarg). ``mcp_call(tool, args) ->
dict`` is injectable: the default spawns the MCP server over stdio; tests/examples pass an
in-memory faithful peer so the wiring runs with no server install.
"""

from __future__ import annotations

import base64
import re
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

McpCall = Callable[[str, dict], dict]  # (tool_name, args) -> MCP tools/call result


def _content_text(result: dict) -> str:
    return "\n".join(
        c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
    )


def _content_image(result: dict) -> bytes | None:
    for c in result.get("content", []):
        if c.get("type") == "image" and c.get("data"):
            try:
                return base64.b64decode(c["data"])
            except Exception:  # noqa: BLE001
                return None
    return None


def _is_error(result: dict) -> bool:
    return bool(result.get("isError"))


# numbered-tree lines look like "‹12› button "OK"" / "12 button ...": pull the leading index.
_ELEM_RE = re.compile(r"^\s*[‹<\[]?(\d+)[›>\]]?\s+(\S+)(?:\s+\"([^\"]*)\")?", re.M)


class McpComputerSandbox:
    """Duck-typed :class:`~shinken.client.Sandbox` over an app-scoped MCP computer-use server."""

    def __init__(self, mcp_call: McpCall, *, app: str, platform: str = "linux") -> None:
        self._call = mcp_call
        self._app = app
        self._platform = platform
        self._closed = False
        self._provider = None
        self._handle = None
        self._elements: dict[str, dict] = {}  # ref -> {element_index, role, name}

    def _invoke(self, tool: str, **args: Any) -> dict:
        if self._closed:
            raise RuntimeError("McpComputerSandbox is closed")
        args.setdefault("app", self._app)
        res = self._call(tool, {k: v for k, v in args.items() if v is not None})
        if _is_error(res):
            raise RuntimeError(f"mcp {tool} failed: {_content_text(res)[:200]}")
        return res

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            schema_version=1,
            verbs=[
                "observe",
                "screenshot",
                "click",
                "type_text",
                "key",
                "scroll",
                "set_value",
                "invoke_action",
            ],
            targets=["point_px", "element_ref"],  # element_ref via get_app_state's element_index
            observation_types=["screenshot", "tree"],
            structured_observation=True,
        )

    @property
    def platform(self) -> str:
        return self._platform

    def _set_provider_context(self, provider: Any, handle: Any) -> None:
        self._provider, self._handle = provider, handle

    # -- observation -----------------------------------------------------------------
    def ping(self) -> float:
        t0 = time.perf_counter()
        self._call("list_apps", {})
        return time.perf_counter() - t0

    def screenshot(self, scope: str = "screen", **_kw: Any) -> dict:
        res = self._invoke("get_app_state")
        png = _content_image(res) or b""
        return {
            "type": "observation",
            "png": png,
            "w": None,
            "h": None,
            "scope": scope,
            "format": "png",
        }

    def observe(self, structured: bool = False, source: Any = None, **_kw: Any) -> dict:
        res = self._invoke("get_app_state", show_full_text=True if structured else None)
        tree_text = _content_text(res)
        png = _content_image(res)
        if not structured:
            return {
                "type": "observation",
                "tree": "full",
                "image": {"scope": "screen"},
                "png": png or b"",
            }
        elements = self._parse_elements(tree_text)
        return {
            "type": "observation",
            "tree": "full",
            "available": True,
            "tree_text": tree_text,
            "elements": elements,
            "png": png or b"",
        }

    def _parse_elements(self, tree_text: str) -> list[dict]:
        """Turn the server's numbered AX tree into Shinken-shaped elements with stable refs
        (``e<element_index>``), so ``act_on(ref)``/``invoke_action(ref)`` can target by index."""
        self._elements = {}
        out = []
        for idx, role, name in _ELEM_RE.findall(tree_text):
            ref = f"e{idx}"
            el = {"ref": ref, "role": role, "name": name or "", "element_index": idx}
            self._elements[ref] = el
            out.append(el)
        return out

    # -- actions ---------------------------------------------------------------------
    def click(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        ref: str | None = None,
        button: str = "left",
        count: int = 1,
        **_kw: Any,
    ) -> dict:
        if ref is not None:
            el = self._elements.get(ref)
            if el is None:
                raise KeyError(f"unknown element_ref {ref!r}; call observe(structured=True) first")
            self._invoke(
                "click", element_index=el["element_index"], click_count=count, mouse_button=button
            )
        elif x is not None and y is not None:
            self._invoke("click", x=x, y=y, click_count=count, mouse_button=button)
        else:
            raise ValueError("mcp-computer click needs ref= (element) or x,y (pixel)")
        return {"ok": True}

    def act_on(self, ref: str, verb: str = "click", **kwargs: Any) -> dict:
        if verb == "click":
            return self.click(ref=ref, **kwargs)
        if verb == "set_value":
            return self.set_value(ref, kwargs.get("text", kwargs.get("value", "")))
        raise ValueError(f"mcp-computer act_on supports click/set_value, not {verb!r}")

    def invoke_action(self, ref: str, action: str | None = None) -> dict:
        el = self._elements.get(ref)
        if el is None:
            raise KeyError(f"unknown element_ref {ref!r}")
        self._invoke(
            "perform_secondary_action",
            element_index=el["element_index"],
            action=action or "AXPress",
        )
        return {"ok": True}

    def set_value(self, ref: str, value: str) -> dict:
        el = self._elements.get(ref)
        if el is None:
            raise KeyError(f"unknown element_ref {ref!r}")
        self._invoke("set_value", element_index=el["element_index"], value=value)
        return {"ok": True}

    def type_text(self, text: str, **_kw: Any) -> dict:
        self._invoke("type_text", text=text)
        return {"ok": True}

    def key(self, keys: str, **_kw: Any) -> dict:
        self._invoke("press_key", key=keys)  # server takes xdotool key syntax verbatim
        return {"ok": True}

    def scroll(self, dx: int = 0, dy: int = 0, *, ref: str | None = None, **_kw: Any) -> dict:
        direction = "down" if dy >= 0 else "up"
        if dx:
            direction = "right" if dx > 0 else "left"
        ei = self._elements.get(ref, {}).get("element_index") if ref else None
        self._invoke("scroll", direction=direction, pages=abs(dy or dx) or 1, element_index=ei)
        return {"ok": True}

    def list_windows(self) -> list[dict]:
        res = self._call("list_apps", {})
        return [{"app": line} for line in _content_text(res).splitlines() if line.strip()]

    # -- runtime-state family: loud degrade (no snapshot tier) -----------------------
    def checkpoint(self, name: str | None = None, **_kw: Any) -> Any:
        return self._provider.checkpoint(self._handle, name=name)

    def spawn(self, **_kw: Any) -> Any:
        return self._provider.resume(self._handle)

    def close(self) -> None:
        self._closed = True


class McpComputerBackend(SandboxProvider):
    """Operation-layer backend over an MCP desktop computer-use server (codex-style 9 tools).

    No snapshot tier — ``supports_fork=False``; inherited ``checkpoint``/``resume`` raise
    ``UnsupportedProviderOperation``."""

    capabilities = ProviderCapabilities(
        name="mcp-computer",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=False,
        supports_fork=False,
        reset_strategy="recreate",
        tier="external-backend",
        notes=(
            "MCP desktop computer-use server (e.g. open-codex-computer-use): non-invasive"
            " Accessibility; structured observe + element_ref; no shell exec, no fork tier",
        ),
    )

    def __init__(
        self,
        transport_factory: Callable[..., McpCall] | None = None,
        *,
        app: str | None = None,
        platform: str = "linux",
    ) -> None:
        self._transport_factory = transport_factory or _default_stdio_transport
        self._app = app
        self._platform = platform
        self._live: dict[str, tuple[McpCall, str]] = {}

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        app = (spec.metadata.get("app") if spec and spec.metadata else None) or self._app
        if not app:
            raise ValueError(
                "mcp-computer backend is app-scoped: set app= on the provider or "
                "SandboxSpec.metadata['app']"
            )
        mcp_call = self._transport_factory(spec)
        sid = f"mcp-{uuid.uuid4().hex[:12]}"
        self._live[sid] = (mcp_call, app)
        return SandboxHandle(
            provider="mcp-computer",
            sandbox_id=sid,
            addr="mcp://stdio",
            created_at=time.time(),
            metadata={"app": app},
        )

    def connect(self, handle: SandboxHandle, **_kw: Any) -> McpComputerSandbox:  # type: ignore[override]
        mcp_call, app = self._live[handle.sandbox_id]
        env = McpComputerSandbox(mcp_call, app=app, platform=self._platform)
        env._set_provider_context(self, handle)
        return env

    def destroy(self, handle: SandboxHandle) -> None:
        entry = self._live.pop(handle.sandbox_id, None)
        if entry and hasattr(entry[0], "close"):
            try:  # pragma: no cover - real stdio teardown
                entry[0].close()
            except Exception:  # noqa: BLE001
                pass


def _default_stdio_transport(
    spec: SandboxSpec | None,
) -> McpCall:  # pragma: no cover - needs the server
    """Spawn an MCP computer-use server over stdio and return an ``mcp_call`` closure.

    Speaks minimal MCP JSON-RPC (``initialize`` → ``tools/call``) line-framed over the
    subprocess's stdio. Override via ``transport_factory=`` to point at a server you manage
    (e.g. a long-lived ``open-computer-use mcp`` process, or a remote bridge)."""
    import json
    import shutil
    import subprocess

    exe = shutil.which("open-computer-use")
    if not exe:
        raise RuntimeError(
            "the default mcp-computer transport needs `open-computer-use` on PATH "
            "(npm i -g open-computer-use); or pass transport_factory= with your own "
            "mcp_call(tool, args) -> result"
        )
    proc = subprocess.Popen(
        [exe, "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1
    )
    _rpc_id = [0]

    def _rpc(method: str, params: dict) -> dict:
        _rpc_id[0] += 1
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": _rpc_id[0], "method": method, "params": params})
            + "\n"
        )
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("mcp server closed stdout")
            msg = json.loads(line)
            if msg.get("id") == _rpc_id[0]:
                if "error" in msg:
                    raise RuntimeError(f"mcp error: {msg['error']}")
                return msg.get("result", {})

    _rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "shinken", "version": "0"},
        },
    )

    def mcp_call(tool: str, args: dict) -> dict:
        return _rpc("tools/call", {"name": tool, "arguments": args})

    mcp_call.close = proc.terminate  # type: ignore[attr-defined]
    return mcp_call
