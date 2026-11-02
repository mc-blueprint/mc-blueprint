import base64
import json
import struct
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["scene", "edits"])
def test_viewer_schema_copies_match(name: str) -> None:
    root = json.loads((ROOT / "schema" / f"{name}.schema.json").read_text(encoding="utf-8"))
    package = json.loads(
        (ROOT / "src" / "mcblueprint" / "schema" / f"{name}.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert root == package
    Draft202012Validator.check_schema(root)


@pytest.mark.parametrize("name", ["scene", "edits"])
def test_viewer_sample_is_valid(name: str) -> None:
    schema = json.loads((ROOT / "schema" / f"{name}.schema.json").read_text(encoding="utf-8"))
    sample = json.loads((ROOT / "tests" / "data" / f"{name}-v1.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(sample)


def test_scene_rejects_unknown_render_and_out_of_range_emission() -> None:
    schema = json.loads((ROOT / "schema" / "scene.schema.json").read_text(encoding="utf-8"))
    scene: dict[str, Any] = json.loads(
        (ROOT / "tests" / "data" / "scene-v1.json").read_text(encoding="utf-8")
    )
    scene["palette"][0]["render"] = "unknown"
    assert list(Draft202012Validator(schema).iter_errors(scene))
    scene["palette"][0]["render"] = "model"
    scene["palette"][0]["emission"] = 16
    assert list(Draft202012Validator(schema).iter_errors(scene))


def test_scene_sample_chunk_binary_contract() -> None:
    scene: dict[str, Any] = json.loads(
        (ROOT / "tests" / "data" / "scene-v1.json").read_text(encoding="utf-8")
    )
    palette_ids = set(range(len(scene["palette"])))
    source_ids = {entry["id"] for entry in scene["sources"]}
    index_width = scene["indexWidth"] // 8

    for chunk in scene["chunks"]:
        dimensions = [
            min(16, size - at) for size, at in zip(scene["size"], chunk["at"], strict=True)
        ]
        element_count = dimensions[0] * dimensions[1] * dimensions[2]
        blocks = base64.b64decode(chunk["blocks"], validate=True)
        sources = base64.b64decode(chunk["sources"], validate=True)
        assert len(blocks) == element_count * index_width
        assert len(sources) == element_count * 4

        block_values = struct.unpack(f"<{element_count}{'H' if index_width == 2 else 'I'}", blocks)
        source_values = struct.unpack(f"<{element_count}I", sources)
        assert set(block_values) <= palette_ids
        assert set(source_values) <= source_ids | {0}
