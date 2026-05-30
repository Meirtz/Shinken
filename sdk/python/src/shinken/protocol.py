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
    """Load the ACI v0 JSON Schema. Works from a wheel via packaged data, with a
    repo-root fallback for unusual layouts."""
    try:
        from importlib.resources import files

        text = files("shinken").joinpath("schemas", "aci.schema.json").read_text(encoding="utf-8")
        return json.loads(text)
    except (FileNotFoundError, ModuleNotFoundError, NotADirectoryError):
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
