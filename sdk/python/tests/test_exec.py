"""Typed in-guest exec channel (G1): the SDK's ``exec``/``exec_stream`` against the
mock runtime — wire shape, typed results, streaming order, capability gating with
the argv/shell audit detail, and the pre-exec-runtime fallback contract."""

from __future__ import annotations

import asyncio
import json
import os

import jsonschema
import pytest

import shinken
from shinken.gateway import CAPABILITY_EVENT_SCHEMA, CapabilityDenied


def _recorded_execs(env) -> list[dict]:
    """The exec requests the mock runtime actually received (observed effects)."""
    return env.query("state")["execs"]


# ---------------------------------------------------------------- buffered form


def test_exec_argv_returns_typed_result(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        res = env.exec(["echo", "hello", "exec"])
        assert res["exit_code"] == 0
        assert res["stdout"] == "hello exec\n"
        assert res["stderr"] == ""
        assert res["timed_out"] is False
        assert res["stdout_truncated"] is False and res["stderr_truncated"] is False
        assert res["duration_ms"] > 0
        # the wire carried the argv form, no shell
        [sent] = _recorded_execs(env)
        assert sent["argv"] == ["echo", "hello", "exec"]
        assert "shell" not in sent


def test_exec_shell_cwd_env_stdin_timeout_travel_the_wire(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        res = env.exec(
            shell="cat | wc -l",
            cwd="/tmp",
            env={"A": "b"},
            stdin="line\n",
            timeout=5.0,
        )
        # the mock echoes the received parameters back as stdout JSON
        echoed = json.loads(res["stdout"])
        assert echoed["shell"] == "cat | wc -l"
        assert echoed["cwd"] == "/tmp"
        assert echoed["env"] == {"A": "b"}
        assert echoed["stdin"] == "line\n"
        [sent] = _recorded_execs(env)
        assert sent["timeout_ms"] == 5000


def test_exec_nonzero_exit_is_returned_not_raised(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        res = env.exec(["false"])
        assert res["exit_code"] == 1  # the COMMAND failed; the ACTION succeeded


def test_exec_timeout_is_reported_honestly(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        res = env.exec(["sleepy"])  # the mock's simulated guest-side timeout kill
        assert res["timed_out"] is True
        assert res["exit_code"] is None
        assert res["signal"] == 9


def test_exec_argument_validation_is_typed_and_local(mock_shinkend):
    with shinken.connect(mock_shinkend) as env:
        with pytest.raises(ValueError, match="exactly one"):
            env.exec(["ls"], shell="ls")
        with pytest.raises(ValueError, match="exactly one"):
            env.exec()
        with pytest.raises(ValueError, match="non-empty"):
            env.exec([])
        assert _recorded_execs(env) == [], "validation failures must never hit the wire"


# ---------------------------------------------------------------- streamed form


def _reassemble(chunks: list[dict]) -> tuple[bytes, bytes, dict]:
    """Split a finished exec_stream into (stdout bytes, stderr bytes, exit dict),
    asserting seq is strictly increasing across both channels."""
    exit_item = chunks[-1]
    assert exit_item["channel"] == "exit", "the stream must terminate with the exit item"
    seqs = [c["seq"] for c in chunks[:-1]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), f"seq not monotonic: {seqs}"
    out = b"".join(c["data"] for c in chunks[:-1] if c["channel"] == "stdout")
    err = b"".join(c["data"] for c in chunks[:-1] if c["channel"] == "stderr")
    return out, err, exit_item


def test_exec_stream_orders_chunks_and_terminates_with_exit(mock_shinkend):
    # default connect negotiates binary frames → exec_output rides raw-byte frames
    with shinken.connect(mock_shinkend) as env:
        chunks = list(env.exec_stream(["echo", "stream", "me"]))
        out, err, exit_item = _reassemble(chunks)
        assert out == b"stream me\n"
        assert exit_item["exit_code"] == 0
        assert exit_item["timed_out"] is False
        assert exit_item["truncated"] is False


def test_exec_stream_text_session_uses_base64_events(mock_shinkend_text_only):
    # a pre-binary runtime: exec_output arrives as JSON text with data_b64
    with shinken.connect(mock_shinkend_text_only) as env:
        chunks = list(env.exec_stream(shell="probe"))
        out, err, exit_item = _reassemble(chunks)
        assert json.loads(out.decode())["shell"] == "probe"
        assert err == b"err-probe\n"
        assert exit_item["exit_code"] == 0


def test_exec_stream_async_iteration(mock_shinkend):
    async def run():
        env = await shinken.aconnect(mock_shinkend)
        try:
            chunks = [c async for c in env.exec_stream(["echo", "async"])]
        finally:
            await env.close()
        return chunks

    chunks = asyncio.run(run())
    out, _err, exit_item = _reassemble(chunks)
    assert out == b"async\n"
    assert exit_item["exit_code"] == 0


# ------------------------------------------------- capability gate + audit detail


def test_exec_denied_capability_never_reaches_the_wire(mock_shinkend):
    with shinken.connect(
        mock_shinkend, sandbox_capabilities={"exec": False}, enforce_capabilities=True
    ) as env:
        with pytest.raises(CapabilityDenied, match="exec"):
            env.exec(["rm", "-rf", "/tmp/x"])
        assert _recorded_execs(env) == []
        [event] = [e for e in env.capability_events if e["subject"] == "exec"]
        assert event["decision"] == "deny" and event["granted"] is False
        # the envelope records WHAT was asked, not just that exec fired
        assert event["detail"] == {"argv": ["rm", "-rf", "/tmp/x"]}
        jsonschema.validate(event, CAPABILITY_EVENT_SCHEMA)


def test_exec_ask_tier_honors_the_approval_handler(mock_shinkend):
    asked: list[tuple] = []

    def approve(verb, cap, reason):
        asked.append((verb, cap))
        return True

    with shinken.connect(
        mock_shinkend,
        sandbox_capabilities={"exec": "ask"},
        enforce_capabilities=True,
        on_ask=approve,
    ) as env:
        res = env.exec(shell="ls | wc -l")
        assert res["exit_code"] == 0
        assert asked == [("exec", "exec")]
        [event] = [e for e in env.capability_events if e["subject"] == "exec"]
        assert event["decision"] == "ask" and event["granted"] is True
        assert event["detail"] == {"shell": "ls | wc -l"}
        jsonschema.validate(event, CAPABILITY_EVENT_SCHEMA)

    # ...and an unhandled ask denies by default, recording the denial
    with shinken.connect(
        mock_shinkend, sandbox_capabilities={"exec": "ask"}, enforce_capabilities=True
    ) as env:
        with pytest.raises(CapabilityDenied):
            env.exec(["id"])
        assert _recorded_execs(env) == []


def test_exec_allowed_by_default_envelope(mock_shinkend):
    # the default envelope grants exec (eval setup/verify is the primary consumer)
    with shinken.connect(mock_shinkend, enforce_capabilities=True) as env:
        assert env.exec(["echo", "ok"])["exit_code"] == 0
        [event] = [e for e in env.capability_events if e["subject"] == "exec"]
        assert event["decision"] == "allow"


# ------------------------------------------------------ pre-exec runtime fallback


def test_exec_against_a_pre_exec_runtime_is_typed_before_the_wire(mock_shinkend_no_exec):
    with shinken.connect(mock_shinkend_no_exec) as env:
        assert "exec" not in env.capabilities.verbs
        with pytest.raises(RuntimeError, match="not advertised"):
            env.exec(["echo", "hi"])
        with pytest.raises(RuntimeError, match="not advertised"):
            list(env.exec_stream(["echo", "hi"]))
        assert _recorded_execs(env) == [], "nothing may reach a runtime without the verb"


# ------------------------------------------------------------------- live (Docker)

requires_docker = pytest.mark.skipif(
    os.environ.get("SHINKEN_DOCKER_TESTS") != "1",
    reason="live Docker test: set SHINKEN_DOCKER_TESTS=1 (needs the shinken/sandbox-linux image)",
)


@requires_docker
def test_live_docker_exec_end_to_end():
    """The typed exec channel against a real container: echo/env/cwd/exit codes,
    truncation, timeout group-kill, and the streamed form — all in-band over the
    sandbox's WebSocket, no `docker exec` involved."""
    import time

    from shinken.providers.docker import DockerLocalProvider

    provider = DockerLocalProvider(
        image=os.environ.get("SHINKEN_IMAGE", "shinken/sandbox-linux"), startup_timeout=120.0
    )
    handle = provider.create()
    try:
        env = provider.connect(handle)
        try:
            r = env.exec(["echo", "hello", "docker"])
            assert (r["exit_code"], r["stdout"]) == (0, "hello docker\n")
            r = env.exec(
                shell='cat; pwd; printf "%s" "$SHK"; exit 4',
                cwd="/tmp",
                env={"SHK": "wired"},
                stdin="in\n",
            )
            assert r["exit_code"] == 4
            assert r["stdout"].startswith("in\n") and r["stdout"].endswith("wired")
            # big output → honest truncation at the per-channel cap
            r = env.exec(
                shell="i=0; while [ $i -lt 16384 ]; do printf '%064d' $i; i=$((i+1)); done"
            )
            assert r["stdout_truncated"] is True and len(r["stdout"]) == 256 * 1024
            # timeout kills the whole process group promptly
            t0 = time.time()
            r = env.exec(shell="sleep 30 & wait", timeout=0.5)
            assert r["timed_out"] is True and r["exit_code"] is None
            assert time.time() - t0 < 10
            # streamed form: ordered chunks + terminal exit
            chunks = list(env.exec_stream(shell="echo a; echo b 1>&2; exit 7"))
            assert chunks[-1]["channel"] == "exit" and chunks[-1]["exit_code"] == 7
            out = b"".join(c["data"] for c in chunks[:-1] if c["channel"] == "stdout")
            assert out == b"a\n"
        finally:
            env.close()
    finally:
        provider.destroy(handle)
