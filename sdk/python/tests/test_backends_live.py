"""Env-gated LIVE smokes for the operation-layer backends (D15) — each drives a backend's
DEFAULT (real-driver) path against the actual third-party system, closing the
"fixture-faithful but never run against the real thing" gap the in-memory peers leave open.

None of these run in normal CI. Gates (one per backend, repo convention):

- ``SHINKEN_BROWSER_LIVE=1`` — launches a real headless Chrome/Chromium and drives
  ``browser-runtime`` over raw CDP (override the binary with ``SHINKEN_CHROME_BIN``).
  Self-contained: no install beyond a local Chrome.
- ``SHINKEN_E2B_LIVE=1`` — drives ``e2b`` through its default factory (``Sandbox.create()``);
  needs ``pip install e2b-desktop`` + ``E2B_API_KEY`` + network.
- ``SHINKEN_CUA_LIVE=1`` — drives ``cua`` through its default factory (a real
  ``computer.Computer``); needs ``pip install cua-computer`` + a reachable cua sandbox
  (Linux defaults to cua's docker provider; point elsewhere with ``SHINKEN_CUA_PROVIDER``/
  ``SHINKEN_CUA_NAME``/``SHINKEN_CUA_IMAGE``/``CUA_API_KEY``/``SHINKEN_CUA_HOST_SERVER=1``).
- ``SHINKEN_MCP_LIVE=1`` — drives ``mcp-computer`` through its default stdio transport;
  needs ``open-computer-use`` on PATH (npm i -g) + the OS **Accessibility** grant (and the
  **Screen Recording** grant for the screenshot part — without it the server honestly omits
  the image and the smoke accepts the typed degrade); the target app comes from
  ``SHINKEN_MCP_APP`` (default "Finder").

Once a gate is set, missing prerequisites FAIL (repo convention — the gate is the only
skip): a gated lane that silently skips reads as green coverage that doesn't exist.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from shinken.backends import get_backend
from shinken.providers.base import SandboxSpec, UnsupportedProviderOperation

# ---------------------------------------------------------------------------- browser-runtime


def _find_chrome() -> str | None:
    env = os.environ.get("SHINKEN_CHROME_BIN")
    if env:
        return env
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if Path(mac).exists():
        return mac
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        exe = shutil.which(name)
        if exe:
            return exe
    return None


class _RealCdpClient:
    """A minimal real-CDP client over one page-target WebSocket — the live stand-in for an
    open-browser-use host. Same duck-typed surface the backend calls:
    ``execute_cdp(tab, method, params)`` (+ clipboard stubs, unused by this smoke)."""

    def __init__(self, ws_url: str) -> None:
        from websockets.sync.client import connect

        # AX trees + base64 screenshots overflow the 1 MiB default frame cap
        self._ws = connect(ws_url, open_timeout=15, max_size=64 * 1024 * 1024)
        self._id = 0

    def execute_cdp(self, tab: int, method: str, params: dict) -> dict:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        deadline = time.monotonic() + 30
        while True:
            msg = json.loads(self._ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if msg.get("id") != self._id:
                continue  # CDP events / stale replies
            if "error" in msg:
                raise RuntimeError(f"CDP {method}: {msg['error']}")
            return msg.get("result", {})

    def read_clipboard_text(self, tab: int) -> str:
        return ""

    def write_clipboard_text(self, tab: int, text: str) -> None:
        raise NotImplementedError("clipboard is not part of this live smoke")

    def close(self) -> None:
        self._ws.close()


@pytest.fixture
def live_chrome():
    """Launch a real headless Chrome with CDP enabled; yield a page-target client factory."""
    chrome = _find_chrome()
    if chrome is None:
        pytest.fail("SHINKEN_BROWSER_LIVE=1 but no Chrome/Chromium found; set SHINKEN_CHROME_BIN")
    # snap-confined chromium gets a PRIVATE /tmp, so a /tmp profile dir's DevToolsActivePort
    # would never appear on the host side — keep the profile under $HOME on Linux
    tmp_parent = str(Path.home()) if platform.system() == "Linux" else None
    tmp = tempfile.mkdtemp(prefix="shinken-live-chrome-", dir=tmp_parent)
    args = [
        chrome,
        "--headless=new",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        f"--user-data-dir={tmp}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "about:blank",
    ]
    if os.name == "posix" and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")  # Chrome refuses to start sandboxed as root
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None
    try:
        # Chrome writes "port\nbrowser-guid" to DevToolsActivePort in the profile dir; the
        # write isn't atomic, so accept it only once both lines parse
        port_file = Path(tmp) / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        port = None
        while port is None:
            if proc.poll() is not None:
                pytest.fail(f"Chrome exited at startup (code {proc.returncode})")
            if time.monotonic() > deadline:
                pytest.fail("Chrome never wrote a parseable DevToolsActivePort")
            try:
                lines = port_file.read_text().splitlines()
                if len(lines) >= 2:
                    port = int(lines[0])
            except (FileNotFoundError, ValueError):
                pass
            if port is None:
                time.sleep(0.05)

        page_ws = None
        deadline = time.monotonic() + 10
        while page_ws is None and time.monotonic() < deadline:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
                targets = json.loads(r.read())
            page_ws = next(
                (t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page"), None
            )
            if page_ws is None:
                time.sleep(0.1)
        assert page_ws, "no page target exposed by Chrome"

        client = _RealCdpClient(page_ws)
        yield lambda spec: (client, 1)
    finally:
        if client is not None:
            client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


_PAGE = (
    "data:text/html,<title>Shinken live</title>"
    '<input id="box" aria-label="Name">'
    '<button onclick="window.__clicked=1">OK</button>'
    '<a href="%23">Docs</a>'
)


@pytest.mark.skipif(
    os.environ.get("SHINKEN_BROWSER_LIVE") != "1",
    reason="live headless-Chrome CDP smoke; set SHINKEN_BROWSER_LIVE=1 (needs a local Chrome)",
)
def test_browser_runtime_live_real_chrome(live_chrome):
    prov = get_backend("browser-runtime", client_factory=live_chrome)
    handle = prov.create()
    env = prov.connect(handle)
    try:
        env.navigate(_PAGE)
        deadline = time.monotonic() + 10
        while env.eval("document.readyState") != "complete":
            assert time.monotonic() < deadline, "page never finished loading"
            time.sleep(0.05)

        # locator/script surface against the real JS engine
        assert env.eval("1 + 1") == 2
        assert env.eval("document.title") == "Shinken live"

        # pixel surface: a real PNG out of a real renderer
        shot = env.screenshot()
        assert shot["png"].startswith(b"\x89PNG\r\n\x1a\n") and len(shot["png"]) > 1000

        # structured surface: real Chrome AX tree -> the shared parse_ax_tree path ->
        # element_refs with bboxes, same shape as the guest engine
        obs = env.observe(structured=True)
        assert obs["available"] is not False
        labels = {e.get("name") for e in obs["elements"]}
        assert "OK" in labels and "Docs" in labels and "Name" in labels
        # a missing DOMSnapshot bound serializes as [0,0,0,0] — require a REAL box, so a
        # bounds-enrichment failure fails loudly instead of silently clicking (0,0)
        ok_el = next(e for e in obs["elements"] if e.get("name") == "OK")
        bbox = ok_el.get("bbox") or [0, 0, 0, 0]
        assert bbox[2] > 0 and bbox[3] > 0, f"OK button has no layout bounds: {ok_el}"
        ok_ref = ok_el["ref"]

        # element_ref click on the REAL button — proven by the page's own state change
        env.click(ref=ok_ref)
        deadline = time.monotonic() + 5
        while env.eval("window.__clicked") != 1:
            assert time.monotonic() < deadline, "element_ref click never landed"
            time.sleep(0.05)

        # typing lands in the real focused input
        env.eval("document.getElementById('box').focus()")
        env.type_text("hello live")
        assert env.eval("document.getElementById('box').value") == "hello live"

        # honest degrades hold against the real thing too
        with pytest.raises(UnsupportedProviderOperation):
            env.checkpoint("golden")
    finally:
        env.close()
        prov.destroy(handle)


# ---------------------------------------------------------------------------- e2b-desktop


@pytest.mark.skipif(
    os.environ.get("SHINKEN_E2B_LIVE") != "1",
    reason="live E2B cloud smoke; set SHINKEN_E2B_LIVE=1 (needs e2b-desktop + E2B_API_KEY)",
)
def test_e2b_live_real_cloud_desktop():
    try:
        import e2b_desktop  # noqa: F401
    except ImportError:
        pytest.fail("SHINKEN_E2B_LIVE=1 but e2b-desktop is not installed (pip install e2b-desktop)")
    assert os.environ.get("E2B_API_KEY"), "E2B_API_KEY required for the live e2b smoke"
    prov = get_backend("e2b")  # DEFAULT factory: Sandbox.create() — boots Xvfb/xfce4
    with prov.session(SandboxSpec(metadata={"timeout": 120})) as env:
        assert env.screen_size()["w"] > 0
        shot = env.screenshot()
        assert shot["png"].startswith(b"\x89PNG") and len(shot["png"]) > 1000

        # the exec contract against the REAL SDK: echo roundtrip, typed non-zero exit
        # (e2b raises CommandExitException — must come back as a result, not an exception),
        # and the forwarded timeout
        out = env.exec(["echo", "live from e2b"])
        assert out["exit_code"] == 0 and "live from e2b" in out["stdout"]
        bad = env.exec(shell="exit 7")
        assert bad["exit_code"] == 7 and bad["timed_out"] is False
        slow = env.exec(shell="sleep 30", timeout=2)
        assert slow["timed_out"] is True

        env.click(x=10, y=10)
        env.key("ctrl+s")

        s = env.observe(structured=True)
        assert s["available"] is False  # pixel-only, honestly
        with pytest.raises(UnsupportedProviderOperation):
            env.checkpoint("golden")


# ---------------------------------------------------------------------------- cua


@pytest.mark.skipif(
    os.environ.get("SHINKEN_CUA_LIVE") != "1",
    reason="live cua smoke; set SHINKEN_CUA_LIVE=1 (needs cua-computer + a reachable cua VM)",
)
def test_cua_live_real_computer():
    try:
        import computer  # noqa: F401
    except ImportError:
        pytest.fail(
            "SHINKEN_CUA_LIVE=1 but cua-computer is not installed (pip install cua-computer)"
        )
    # DEFAULT factory: Computer(**_computer_kwargs(spec)) — Linux targets cua's docker
    # provider unless SHINKEN_CUA_PROVIDER / metadata override
    prov = get_backend("cua")
    with prov.session() as env:
        size = env.screen_size()
        assert size["w"] > 0 and size["h"] > 0
        shot = env.screenshot()
        assert shot["png"].startswith(b"\x89PNG") and len(shot["png"]) > 1000

        env.click(x=10, y=10)
        env.type_text("live from cua")
        env.key("ctrl+a")

        if "exec" in env.capabilities.verbs:
            out = env.exec(["echo", "live"])
            assert out["returncode"] == 0 and "live" in out["stdout"]
        # honest either way — and meaningfully asserted in BOTH branches: a served tree
        # must have text; a degrade must be typed with a detail (the real cua RAISES when
        # it can't serve the tree, which the adapter converts — never an unhandled error)
        obs = env.observe(structured=True)
        if obs["available"]:
            assert obs.get("tree_text"), "available=True but tree_text is empty"
        else:
            assert obs.get("detail"), "typed unavailable must carry a detail"
        with pytest.raises(UnsupportedProviderOperation):
            env.checkpoint("golden")


# ---------------------------------------------------------------------------- mcp-computer


@pytest.mark.skipif(
    os.environ.get("SHINKEN_MCP_LIVE") != "1",
    reason=(
        "live MCP computer-use smoke; set SHINKEN_MCP_LIVE=1 (needs `open-computer-use` on "
        "PATH + the OS Accessibility grant, plus Screen Recording for the image part; "
        "target app via SHINKEN_MCP_APP, default Finder)"
    ),
)
def test_mcp_computer_live_real_server():
    if shutil.which("open-computer-use") is None:
        pytest.fail(
            "SHINKEN_MCP_LIVE=1 but `open-computer-use` is not on PATH (npm i -g open-computer-use)"
        )
    app = os.environ.get("SHINKEN_MCP_APP", "Finder")
    prov = get_backend("mcp-computer")  # DEFAULT transport: spawn `open-computer-use mcp`
    with prov.session(SandboxSpec(metadata={"app": app})) as env:
        # the codex-style numbered AX tree -> structured observe + element refs
        obs = env.observe(structured=True)
        assert obs.get("tree_text"), "no AX tree text from the real server"
        refs = [e["ref"] for e in obs.get("elements", []) if e.get("ref")]
        assert refs, "real get_app_state produced no element refs"

        # the image part is OPTIONAL on the real server (absent without the Screen Recording
        # grant / on capture failure) and the adapter degrades to b"" — accept the typed
        # degrade, but validate real content when present
        shot = env.screenshot()
        assert isinstance(shot["png"], bytes)
        if shot["png"]:
            assert shot["png"].startswith(b"\x89PNG\r\n\x1a\n")

        with pytest.raises(UnsupportedProviderOperation):
            env.checkpoint("golden")
