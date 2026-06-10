"""Pluggable shinkend injector: method registry + user-chosen method, clear error if the
chosen method can't reach the sandbox (no silent fallback). Mock-tested (no real docker/ssh)."""

from __future__ import annotations

import pytest

from shinken import inject
from shinken.inject import InjectionError, InjectionTarget, inject_shinkend


@pytest.fixture
def fake_binary(tmp_path):
    p = tmp_path / "shinkend"
    p.write_bytes(b"\x7fELF-fake-binary")
    return str(p)


@pytest.fixture
def captured_run(monkeypatch):
    calls: list[list[str]] = []

    def fake(cmd, *, timeout=60.0):
        calls.append(list(cmd))
        return ""

    monkeypatch.setattr(inject, "_run", fake)
    return calls


def test_three_methods_registered():
    assert set(inject.available()) == {"docker", "ssh", "osworld-exec"}


def test_unknown_method_raises_listing_available(fake_binary):
    with pytest.raises(InjectionError) as ei:
        inject_shinkend(InjectionTarget(container="c"), fake_binary, method="nope")
    msg = str(ei.value)
    assert "unknown injection method" in msg and "docker" in msg and "ssh" in msg


def test_missing_binary_raises():
    with pytest.raises(InjectionError, match="not found"):
        inject_shinkend(InjectionTarget(container="c"), "/no/such/shinkend", method="docker")


def test_docker_injection_builds_cp_exec_and_reachable_addr(fake_binary, captured_run):
    target = InjectionTarget(container="abc123", port=8765, reachable_addr="127.0.0.1:9999")
    # readiness_timeout=0 skips the TCP reachability poll (no real shinkend in this test).
    res = inject_shinkend(target, fake_binary, method="docker", readiness_timeout=0)
    assert res.addr == "127.0.0.1:9999"
    assert res.token and res.token.startswith("shk_")
    joined = [" ".join(c) for c in captured_run]
    assert any("cp" in j and "abc123:/usr/local/bin/shinkend" in j for j in joined)
    assert any("exec -d abc123" in j for j in joined)
    start = next(j for j in joined if "SHINKEND_ADDR" in j)
    assert "0.0.0.0:8765" in start and "SHINKEND_TOKEN" in start


def test_docker_missing_container_raises(fake_binary, captured_run):
    with pytest.raises(InjectionError, match="container"):
        inject_shinkend(InjectionTarget(), fake_binary, method="docker")


def test_ssh_injection_builds_scp_and_ssh(fake_binary, captured_run):
    target = InjectionTarget(ssh_host="vm.local", ssh_user="root", ssh_port=2222, port=8765)
    res = inject_shinkend(target, fake_binary, method="ssh", readiness_timeout=0)
    joined = [" ".join(c) for c in captured_run]
    assert any(
        j.startswith("scp ") and "root@vm.local:/usr/local/bin/shinkend" in j for j in joined
    )
    assert any(j.startswith("ssh ") and "SHINKEND_ADDR" in j for j in joined)
    assert res.addr == "vm.local:8765"


def test_ssh_missing_host_raises(fake_binary, captured_run):
    with pytest.raises(InjectionError, match="ssh_host"):
        inject_shinkend(InjectionTarget(), fake_binary, method="ssh")


def test_osworld_exec_injection(fake_binary, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        inject, "_controller_exec", lambda url, cmd, **k: (calls.append(cmd), "")[1]
    )
    target = InjectionTarget(controller_url="http://sbx:5000", host="sbx", port=8765)
    res = inject_shinkend(target, fake_binary, method="osworld-exec", readiness_timeout=0)
    assert any("base64 -d" in c for c in calls)
    assert any("SHINKEND_ADDR" in c for c in calls)
    assert res.addr == "sbx:8765"


def test_loopback_bind_when_no_token(fake_binary, captured_run):
    res = inject_shinkend(
        InjectionTarget(container="c"),
        fake_binary,
        method="docker",
        require_token=False,
        readiness_timeout=0,
    )
    assert res.token is None
    start = next(" ".join(c) for c in captured_run if "SHINKEND_ADDR" in " ".join(c))
    assert "127.0.0.1:" in start and "SHINKEND_TOKEN" not in start


def test_run_wraps_subprocess_failure_as_injection_error():
    with pytest.raises(InjectionError):
        inject._run(["false"])  # real subprocess: non-zero exit -> InjectionError


def test_osworld_exec_chunks_large_binary_and_honors_remote_bin(tmp_path, monkeypatch):
    # A multi-MB binary must upload in chunks (one command would overrun ARG_MAX) and land at
    # the chosen remote_bin (a non-root controller can't write /usr/local/bin).
    big = tmp_path / "shinkend"
    big.write_bytes(b"\x7fELF" + b"A" * 200_000)  # base64 > one CHUNK → multiple appends
    calls: list[str] = []
    monkeypatch.setattr(inject, "_controller_exec", lambda url, cmd, **k: calls.append(cmd))
    t = InjectionTarget(
        controller_url="http://sbx:5000", host="sbx", port=8765, remote_bin="/tmp/shinkend"
    )
    inject_shinkend(t, str(big), method="osworld-exec", readiness_timeout=0)
    assert len([c for c in calls if ">> /tmp/shinkend.b64" in c]) >= 2  # chunked
    assert any("base64 -d /tmp/shinkend.b64 > /tmp/shinkend" in c for c in calls)  # → remote_bin
    assert any(
        c.startswith("nohup sh -c ") and "SHINKEND_ADDR" in c for c in calls
    )  # shell-wrapped


def test_controller_exec_raises_on_nonzero_returncode(monkeypatch):
    # The controller answers 200 even when the command fails; a non-zero exit must surface.
    import json
    import urllib.request

    class _Resp:
        def read(self):
            return json.dumps({"returncode": 1, "error": "permission denied"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=60: _Resp())
    with pytest.raises(InjectionError, match="permission denied"):
        inject._controller_exec("http://sbx:5000", "touch /usr/local/bin/x")


def test_readiness_poll_failure_raises_with_log_hint(fake_binary, captured_run, monkeypatch):
    # The injectors run detached, so a binary that never binds its port must be caught by
    # the readiness poll (not left for the SDK to fail on connect).
    monkeypatch.setattr(inject, "_wait_reachable", lambda addr, timeout: False)
    with pytest.raises(InjectionError, match="did not become reachable"):
        inject_shinkend(InjectionTarget(container="c"), fake_binary, method="docker")


def test_ssh_host_with_leading_dash_is_rejected(fake_binary, captured_run):
    # an ssh_host that looks like an option is argument-injection into the ssh client
    with pytest.raises(InjectionError, match="leading"):
        inject_shinkend(
            InjectionTarget(ssh_host="-oProxyCommand=evil"),
            fake_binary,
            method="ssh",
            readiness_timeout=0,
        )


def test_pin_x11_display_forces_x11_backend_and_display():
    from shinken.inject import pin_x11_display

    t = pin_x11_display(InjectionTarget(container="c"))
    assert t.env["DISPLAY"] == ":0"
    assert t.env["SHINKEND_EXECUTOR"] == "x11_xtest"
    # does not clobber a caller-set display
    t2 = pin_x11_display(InjectionTarget(container="c", env={"DISPLAY": ":99"}))
    assert t2.env["DISPLAY"] == ":99" and t2.env["SHINKEND_EXECUTOR"] == "x11_xtest"


def test_injected_start_command_exports_env(fake_binary, captured_run):
    # target.env (DISPLAY, SHINKEND_EXECUTOR) must reach the started shinkend so a missing
    # display fails loud (x11_xtest) instead of silently binding the virtual backend.
    from shinken.inject import pin_x11_display

    target = pin_x11_display(InjectionTarget(container="abc"))
    inject_shinkend(target, fake_binary, method="docker", readiness_timeout=0)
    start = next(" ".join(c) for c in captured_run if "SHINKEND_ADDR" in " ".join(c))
    assert "SHINKEND_EXECUTOR=x11_xtest" in start and "DISPLAY=:0" in start
