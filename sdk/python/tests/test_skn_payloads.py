"""Per-kind .skn event payload contracts (#150)."""

from __future__ import annotations

import jsonschema
import pytest

from shinken.skn import skn_schema


def _ev_ref() -> dict:
    return {"$defs": skn_schema()["$defs"], "$ref": "#/$defs/Event"}


def _event(kind: str, payload: dict) -> dict:
    return {"seq": 0, "dt": 0.0, "kind": kind, "src": "x", "payload": payload}


def _ok(kind: str, payload: dict) -> None:
    jsonschema.validate(_event(kind, payload), _ev_ref())


def _bad(kind: str, payload: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_event(kind, payload), _ev_ref())


def test_file_transfer_payload_contract():
    _ok("file_transfer", {"direction": "put", "sha256": "a", "size": 3, "scope": "session"})
    _ok(
        "file_transfer",
        {"direction": "get", "path": "/p", "sha256": "a", "size": 0, "scope": "s", "stored": True},
    )
    _bad("file_transfer", {"sha256": "a", "size": 3, "scope": "s"})  # missing direction
    _bad("file_transfer", {"direction": "put", "size": 3, "scope": "s"})  # missing sha256
    _bad(
        "file_transfer",
        {"direction": "put", "sha256": "a", "size": 3, "scope": "s", "bogus": 1},  # extra field
    )


def test_verifier_receipt_payload_contract():
    _ok("verifier_receipt", {"passed": True, "checks": [{"name": "x", "ok": True, "evidence": 1}]})
    _bad("verifier_receipt", {"checks": []})  # missing passed
    _bad("verifier_receipt", {"passed": True})  # missing checks
    _bad("verifier_receipt", {"passed": True, "checks": [{"ok": True}]})  # check missing name


def test_action_payload_requires_verb():
    _ok("action", {"verb": "click", "target": {"kind": "point_px", "x": 1, "y": 2}})
    _bad("action", {})  # missing verb


def test_open_kinds_accept_generic_payloads():
    for kind in ("observation", "permission", "meta", "marker", "decision"):
        _ok(kind, {"anything": 1})
        _ok(kind, {})
