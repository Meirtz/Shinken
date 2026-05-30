"""ACI v0 protocol helpers + schema validation.

The wire schema source of truth is ``schema/aci.schema.json`` at the repo root.
:func:`validate` checks a message against it (requires the optional ``jsonschema``
dependency, installed via ``pip install shinken[dev]``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 0


@lru_cache(maxsize=1)
def aci_schema() -> dict:
    """Load the ACI v0 JSON Schema from the repo (monorepo / editable install)."""
    # this file: sdk/python/src/shinken/protocol.py -> parents[4] == repo root
    root = Path(__file__).resolve().parents[4]
    return json.loads((root / "schema" / "aci.schema.json").read_text())


def validate(message: dict) -> None:
    """Validate one ACI message against the v0 schema. Raises on invalid input."""
    try:
        import jsonschema
    except ImportError as exc:  # pragma: no cover - exercised only without the dev extra
        raise RuntimeError(
            "validate() requires the 'jsonschema' package — pip install 'shinken[dev]'"
        ) from exc
    jsonschema.validate(message, aci_schema())
