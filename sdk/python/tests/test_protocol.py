"""ACI v0 messages validate against schema/aci.schema.json (proves schema round-trip)."""

from __future__ import annotations

import jsonschema
import pytest

from shinken import protocol


@pytest.mark.parametrize(
    "message",
    [
        {"type": "hello", "v": 0, "client": {"name": "shinken-py", "version": "0.0.0"}},
        {"type": "ping", "t": 1.0},
        {"type": "query", "call_id": "c1", "q": "platform"},
        {"type": "result", "call_id": "c1", "ok": True, "value": "linux"},
        {
            "type": "action",
            "call_id": "c2",
            "action": {"verb": "click", "target": {"kind": "element_ref", "ref": "e1"}},
        },
        {
            "type": "welcome",
            "v": 0,
            "server": {"name": "shinkend", "version": "0.0.0", "platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click", "start_screencast", "stop_screencast"],
                "targets": ["element_ref"],
                "observation_types": ["a11y", "screencast"],
            },
        },
        # screencast wire vocabulary (must match shinkend/SDK; see #56)
        {
            "type": "action",
            "call_id": "sc1",
            "action": {"verb": "start_screencast", "fps": 10, "max_long_edge": 640},
        },
        {"type": "action", "call_id": "sc2", "action": {"verb": "stop_screencast"}},
        # fps at the runtime clamp boundaries (0.1 .. 30) is valid (#70)
        {
            "type": "action",
            "call_id": "sc3",
            "action": {"verb": "start_screencast", "fps": 0.1, "max_long_edge": 1},
        },
        {
            "type": "action",
            "call_id": "sc4",
            "action": {"verb": "start_screencast", "fps": 30, "max_long_edge": 2576},
        },
        {
            "type": "action",
            "call_id": "c",
            "action": {"verb": "screenshot", "scope": "active_window"},
        },
        {
            "type": "action",
            "call_id": "c",
            "action": {"verb": "screenshot", "scope": "window:0x1a"},
        },
        # a server-pushed screencast frame carries stream + seq + a scoped image
        {
            "type": "observation",
            "obs_id": "sc1-0",
            "stream": "sc1",
            "seq": 0,
            "image": {"ref": "abc", "w": 640, "h": 400, "scope": "screen"},
        },
    ],
)
def test_valid_messages_pass_schema(message):
    protocol.validate(message)


@pytest.mark.parametrize(
    "message",
    [
        {"type": "hello"},  # missing v + client
        {"type": "action", "call_id": "c", "action": {"verb": "teleport"}},  # bad verb
        {"type": "nonsense"},  # unknown discriminator
        {  # bad window id
            "type": "action",
            "call_id": "c",
            "action": {"verb": "screenshot", "scope": "window:nope"},
        },
        {  # unsupported scope (region not implemented)
            "type": "action",
            "call_id": "c",
            "action": {"verb": "screenshot", "scope": "region"},
        },
        {  # fps below the runtime lower clamp bound (#70)
            "type": "action",
            "call_id": "sc",
            "action": {"verb": "start_screencast", "fps": 0},
        },
        {  # fps above the runtime upper clamp bound (#70)
            "type": "action",
            "call_id": "sc",
            "action": {"verb": "start_screencast", "fps": 60},
        },
        {  # invalid long-edge cap
            "type": "action",
            "call_id": "sc",
            "action": {"verb": "start_screencast", "max_long_edge": 0},
        },
        {  # a stream frame must include seq (#70)
            "type": "observation",
            "obs_id": "sc-0",
            "stream": "sc",
            "image": {"ref": "abc", "w": 640, "h": 400, "scope": "screen"},
        },
        {  # seq implies a stream id (#70)
            "type": "observation",
            "obs_id": "sc-0",
            "seq": 0,
            "image": {"ref": "abc", "w": 640, "h": 400, "scope": "screen"},
        },
        {  # stream frames carry image payloads (#70)
            "type": "observation",
            "obs_id": "sc-0",
            "stream": "sc",
            "seq": 0,
        },
    ],
)
def test_invalid_messages_fail_schema(message):
    with pytest.raises(jsonschema.ValidationError):
        protocol.validate(message)


def test_schema_version_constant():
    assert protocol.SCHEMA_VERSION == 0


def test_parse_welcome_ok():
    from shinken.client import _parse_welcome

    caps, platform = _parse_welcome(
        {
            "type": "welcome",
            "v": 0,
            "server": {"platform": "linux"},
            "capabilities": {
                "schema_version": 0,
                "verbs": ["click"],
                "targets": ["point_px"],
                "observation_types": ["screenshot"],
            },
        }
    )
    assert platform == "linux"
    assert caps.schema_version == 0 and "click" in caps.verbs


@pytest.mark.parametrize(
    "welcome",
    [
        {"type": "oops", "v": 0, "capabilities": {"schema_version": 0}},
        {"type": "welcome", "v": 1, "capabilities": {"schema_version": 0}},
        {"type": "welcome", "v": 0, "capabilities": {"schema_version": 1}},
    ],
)
def test_parse_welcome_rejects_mismatch(welcome):
    from shinken.client import _parse_welcome

    with pytest.raises(RuntimeError):
        _parse_welcome(welcome)
