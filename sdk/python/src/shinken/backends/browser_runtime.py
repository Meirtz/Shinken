"""Browser Runtime backend — drive the Shinken operation layer over a browser (CDP), the
BU half of computer-use. Wraps iFurySt/open-browser-use's client (a tab-scoped CDP channel
over a Unix socket + Chrome extension) as a duck-typed Sandbox.

This is Shinken's designed Browser Runtime (D13 §10, operation-layer.md) realized as a
backend — the SAME three tab surfaces the desktop tier mirrors:
  1. **pixels**     — ``screenshot`` (CDP ``Page.captureScreenshot``) + ``click(x,y)`` /
                      ``scroll`` (CDP ``Input.dispatch*``)
  2. **semantic node-ids** — ``observe(structured=True)`` pulls the CDP a11y tree
                      (``Accessibility.getFullAXTree`` + ``DOMSnapshot`` bounds) and reuses
                      ``shinken.cdp.parse_ax_tree`` → ``shinken.a11y`` to mint stable
                      ``element_ref``s; ``click(ref=…)`` acts by node, same as the guest engine
  3. **locator/script** — ``eval(expr)`` / ``navigate(url)`` (Playwright-locator subset is the
                      designed extension)

Why a SEPARATE browser facade (not desktop ACI verbs): codex keeps CU.app and BU.app apart —
BU operates Chrome via an extension (no OS-level trust), does browser-specific work CU can't
(tabs, cross-origin, DOM selectors), and is policy-scoped by URL origin, not the full sandbox
envelope. Honest capabilities here: no ``exec`` (a browser has no shell), no fork tier (tabs
are ephemeral → ``supports_fork=False``, ``checkpoint`` raises ``UnsupportedProviderOperation``).

``client_factory(spec) -> (client, tab_id)`` is injectable: the default connects to a running
open-browser-use socket and claims/creates a tab; tests/examples pass a protocol-faithful
in-memory CDP client (no socket, no Chrome).
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Callable
from typing import Any

from ..a11y import observe_structured
from ..cdp import bounds_from_snapshot, parse_ax_tree
from ..client import Capabilities
from ..providers.base import (
    ProviderCapabilities,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
)


class _BuAxSource:
    """An :class:`~shinken.a11y.A11ySource` over a BU client's CDP channel — feeds the AX
    tree + DOMSnapshot bounds through the shared ``parse_ax_tree`` normalizer, so the BU
    structured observation is byte-identical in shape to the desktop guest engine's."""

    source_name = "cdp"

    def __init__(self, cdp: Callable[[str, dict], dict]) -> None:
        self._cdp = cdp

    def tree(self):
        ax = self._cdp("Accessibility.getFullAXTree", {})
        bounds = {}
        try:
            snap = self._cdp("DOMSnapshot.captureSnapshot", {"computedStyles": []})
            bounds = bounds_from_snapshot(snap)
        except Exception:  # noqa: BLE001 - bounds are best-effort enrichment
            pass
        return parse_ax_tree(ax.get("nodes", []), bounds)


class BrowserRuntimeSandbox:
    """Duck-typed :class:`~shinken.client.Sandbox` over a tab-scoped browser CDP channel."""

    def __init__(self, client: Any, tab_id: int, *, platform: str = "browser") -> None:
        self._client = client
        self._tab = tab_id
        self._platform = platform
        self._closed = False
        self._provider = None
        self._handle = None
        self._elements: dict[str, dict] = {}

    def _cdp(self, method: str, params: dict | None = None) -> dict:
        if self._closed:
            raise RuntimeError("BrowserRuntimeSandbox is closed")
        res = self._client.execute_cdp(self._tab, method, params or {})
        return res if isinstance(res, dict) else {}

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
                "navigate",
                "eval",
                "clipboard_get",
                "clipboard_set",
            ],
            targets=["point_px", "element_ref"],
            observation_types=["screenshot", "tree"],
            structured_observation=True,
        )

    @property
    def platform(self) -> str:
        return self._platform

    def _set_provider_context(self, provider: Any, handle: Any) -> None:
        self._provider, self._handle = provider, handle

    # -- observation (pixels + semantic node-ids) ------------------------------------
    def ping(self) -> float:
        t0 = time.perf_counter()
        self._cdp("Runtime.evaluate", {"expression": "1", "returnByValue": True})
        return time.perf_counter() - t0

    def screenshot(self, scope: str = "screen", **_kw: Any) -> dict:
        res = self._cdp("Page.captureScreenshot", {"format": "png"})
        data = res.get("data", "")
        png = base64.b64decode(data) if data else b""
        return {
            "type": "observation",
            "png": png,
            "w": None,
            "h": None,
            "scope": scope,
            "format": "png",
        }

    def observe(self, structured: bool = False, source: Any = None, **_kw: Any) -> dict:
        if not structured:
            shot = self.screenshot()
            return {
                "type": "observation",
                "tree": "full",
                "image": {"scope": "screen"},
                "png": shot["png"],
            }
        obs = observe_structured(_BuAxSource(self._cdp))
        self._elements = {e["ref"]: e for e in obs.get("elements", []) if e.get("ref")}
        return obs

    # -- actions (pixels + element_ref + locator/script) -----------------------------
    def _mouse(self, x: float, y: float, button: str = "left", count: int = 1) -> None:
        for ev in ("mousePressed", "mouseReleased"):
            self._cdp(
                "Input.dispatchMouseEvent",
                {"type": ev, "x": x, "y": y, "button": button, "clickCount": count},
            )

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
            bbox = el.get("bbox")
            if not bbox:
                raise ValueError(f"element {ref!r} has no bbox; use a pixel target")
            bx, by, bw, bh = bbox
            self._mouse(bx + bw / 2, by + bh / 2, button, count)
        elif x is not None and y is not None:
            self._mouse(x, y, button, count)
        else:
            raise ValueError("browser click needs ref= (node) or x,y (pixel)")
        return {"ok": True}

    def act_on(self, ref: str, verb: str = "click", **kwargs: Any) -> dict:
        if verb != "click":
            raise ValueError(f"browser act_on supports click, not {verb!r}")
        return self.click(ref=ref, **kwargs)

    def type_text(self, text: str, **_kw: Any) -> dict:
        self._cdp("Input.insertText", {"text": text})
        return {"ok": True}

    def key(self, keys: str, **_kw: Any) -> dict:
        # best-effort: send each chord component as a key event (CDP wants windowsVirtualKeyCode
        # for non-text keys; for the common Enter/Tab/arrows the `key` field is honored)
        for part in keys.split("+"):
            self._cdp("Input.dispatchKeyEvent", {"type": "keyDown", "key": part})
            self._cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": part})
        return {"ok": True}

    def scroll(self, dx: int = 0, dy: int = 0, *, x: int = 0, y: int = 0, **_kw: Any) -> dict:
        self._cdp(
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy * 100},
        )
        return {"ok": True}

    # browser-native surface (the locator/script tab surface + navigation)
    def navigate(self, url: str) -> dict:
        self._cdp("Page.enable", {})
        self._cdp("Page.navigate", {"url": url})
        return {"ok": True, "url": url}

    def eval(self, expression: str) -> Any:
        res = self._cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return res.get("result", {}).get("value")

    def clipboard_get(self) -> str:
        return self._client.read_clipboard_text(self._tab) or ""

    def clipboard_set(self, text: str, **_kw: Any) -> dict:
        self._client.write_clipboard_text(self._tab, text)
        return {"ok": True}

    # -- runtime-state family: loud degrade (tabs are ephemeral, no snapshot tier) ----
    def checkpoint(self, name: str | None = None, **_kw: Any) -> Any:
        return self._provider.checkpoint(self._handle, name=name)

    def spawn(self, **_kw: Any) -> Any:
        return self._provider.resume(self._handle)

    def close(self) -> None:
        self._closed = True


def _default_client_factory(spec: SandboxSpec | None):  # pragma: no cover - needs the BU host
    """Connect to a running open-browser-use socket and claim/create a tab. Override via
    ``client_factory=`` to inject a managed client or a remote bridge."""
    import os

    try:
        from open_browser_use import OpenBrowserUseClient  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "the default browser-runtime client needs the `open-browser-use-sdk` package "
            "and a running `open-browser-use` host; pass client_factory= to inject your own "
            "(client, tab_id)"
        ) from exc
    sock = os.environ.get("OPEN_BROWSER_USE_SOCKET", "/tmp/open-browser-use.sock")
    client = OpenBrowserUseClient(socket_path=sock)
    tab = client.create_tab()
    tab_id = tab.get("tabId") if isinstance(tab, dict) else tab
    return client, tab_id


class BrowserRuntimeBackend(SandboxProvider):
    """Operation-layer backend over a browser (CDP via open-browser-use). Shinken's designed
    Browser Runtime (D13). No shell, no fork tier — ``supports_fork=False``."""

    capabilities = ProviderCapabilities(
        name="browser-runtime",
        supports_lifecycle=True,
        supports_gui=True,
        supports_snapshot=False,
        supports_fork=False,
        reset_strategy="recreate",
        display="provider_managed",
        tier="browser-runtime",
        notes=(
            "CDP browser runtime (open-browser-use): pixel + semantic-node (element_ref) +"
            " locator/eval surfaces; no shell exec; tabs ephemeral, no fork",
        ),
    )

    def __init__(self, client_factory: Callable[..., Any] = _default_client_factory) -> None:
        self._factory = client_factory
        self._live: dict[str, tuple[Any, int]] = {}

    def create(self, spec: SandboxSpec | None = None) -> SandboxHandle:
        client, tab_id = self._factory(spec)
        sid = f"bu-{uuid.uuid4().hex[:12]}"
        self._live[sid] = (client, tab_id)
        return SandboxHandle(
            provider="browser-runtime",
            sandbox_id=sid,
            addr="bu://cdp",
            created_at=time.time(),
            metadata={"tab_id": tab_id},
        )

    def connect(self, handle: SandboxHandle, **_kw: Any) -> BrowserRuntimeSandbox:  # type: ignore[override]
        client, tab_id = self._live[handle.sandbox_id]
        env = BrowserRuntimeSandbox(client, tab_id)
        env._set_provider_context(self, handle)
        return env

    def destroy(self, handle: SandboxHandle) -> None:
        entry = self._live.pop(handle.sandbox_id, None)
        if entry and hasattr(entry[0], "close"):
            try:  # pragma: no cover - real socket teardown
                entry[0].close()
            except Exception:  # noqa: BLE001
                pass
