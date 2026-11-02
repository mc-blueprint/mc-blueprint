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


def test_scene_requires_air_first_and_fixed_tint_color() -> None:
    schema = json.loads((ROOT / "schema" / "scene.schema.json").read_text(encoding="utf-8"))
    scene: dict[str, Any] = json.loads(
        (ROOT / "tests" / "data" / "scene-v1.json").read_text(encoding="utf-8")
    )
    assert scene["palette"][0]["state"] == "minecraft:air"
    scene["palette"][0]["state"] = "minecraft:stone"
    assert list(Draft202012Validator(schema).iter_errors(scene))
    scene["palette"][0]["state"] = "minecraft:air"
    del scene["palette"][2]["tintColor"]
    assert list(Draft202012Validator(schema).iter_errors(scene))
    scene["palette"][2]["tint"] = "foliage"
    scene["palette"][2]["tintColor"] = [0.65, -0.08, 0.12]
    assert list(Draft202012Validator(schema).iter_errors(scene))


def test_scene_palette_zero_requires_palette_entry_fields() -> None:
    schema = json.loads((ROOT / "schema" / "scene.schema.json").read_text(encoding="utf-8"))
    scene: dict[str, Any] = json.loads(
        (ROOT / "tests" / "data" / "scene-v1.json").read_text(encoding="utf-8")
    )
    del scene["palette"][0]["emission"]
    assert list(Draft202012Validator(schema).iter_errors(scene))


def test_edits_delete_requires_exactly_one_of_at_or_region() -> None:
    schema = json.loads((ROOT / "schema" / "edits.schema.json").read_text(encoding="utf-8"))
    edits: dict[str, Any] = json.loads(
        (ROOT / "tests" / "data" / "edits-v1.json").read_text(encoding="utf-8")
    )
    delete = edits["edits"][1]
    del delete["region"]
    delete["at"] = [2, 3, 4]
    Draft202012Validator(schema).validate(edits)
    delete["region"] = [[2, 3, 4], [4, 5, 6]]
    assert list(Draft202012Validator(schema).iter_errors(edits))
    del delete["at"]
    del delete["region"]
    assert list(Draft202012Validator(schema).iter_errors(edits))


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
