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
    res = inject_shinkend(target, fake_binary, method="docker")
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
    res = inject_shinkend(target, fake_binary, method="ssh")
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
    res = inject_shinkend(target, fake_binary, method="osworld-exec")
    assert any("base64 -d" in c for c in calls)
    assert any("SHINKEND_ADDR" in c for c in calls)
    assert res.addr == "sbx:8765"


def test_loopback_bind_when_no_token(fake_binary, captured_run):
    res = inject_shinkend(
        InjectionTarget(container="c"), fake_binary, method="docker", require_token=False
    )
    assert res.token is None
    start = next(" ".join(c) for c in captured_run if "SHINKEND_ADDR" in " ".join(c))
    assert "127.0.0.1:" in start and "SHINKEND_TOKEN" not in start


def test_run_wraps_subprocess_failure_as_injection_error():
    with pytest.raises(InjectionError):
        inject._run(["false"])  # real subprocess: non-zero exit -> InjectionError
