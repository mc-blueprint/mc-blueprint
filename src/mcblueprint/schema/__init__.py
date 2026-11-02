"""Access to the bundled Blueprint JSON Schema.

The canonical schema lives in this package so it is always available at runtime.
``schema/blueprint.schema.json`` at the repository root is a copy for editors and
external tools; ``tests/test_schema.py`` checks that both files are identical.
分割出力の region-manifest.schema.json もこのパッケージを正本とし、
ルート schema/ に同一コピーを置く。
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

SCHEMA_FILENAME = "blueprint.schema.json"


@cache
def load_schema() -> dict[str, Any]:
    """Return the bundled Blueprint JSON Schema as a dict (cached)."""
    text = resources.files(__name__).joinpath(SCHEMA_FILENAME).read_text(encoding="utf-8")
    return json.loads(text)


COMPONENT_EXCLUDED_KEYS = ("minecraftVersion", "origin", "size", "seed")


@cache
def load_component_schema() -> dict[str, Any]:
    """Schema for ``components/*.json``: a Blueprint without version / placement keys."""
    schema = json.loads(json.dumps(load_schema()))
    schema["title"] = "mc-blueprint Component"
    schema["required"] = ["formatVersion", "name", "operations"]
    for key in COMPONENT_EXCLUDED_KEYS:
        schema["properties"].pop(key, None)
    schema["properties"]["parameters"] = {"type": "object"}
    schema["properties"]["anchors"] = {
        "type": "object",
        "additionalProperties": {"$ref": "#/$defs/position"},
    }
    schema["properties"]["name"]["pattern"] = "^(?:[a-z0-9_]+|[a-z0-9_]+\\.[a-z0-9_]+)$"
    return schema
