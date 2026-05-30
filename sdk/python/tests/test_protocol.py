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
                "verbs": ["click"],
                "targets": ["element_ref"],
                "observation_types": ["a11y"],
            },
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
    ],
)
def test_invalid_messages_fail_schema(message):
    with pytest.raises(jsonschema.ValidationError):
        protocol.validate(message)


def test_schema_version_constant():
    assert protocol.SCHEMA_VERSION == 0
