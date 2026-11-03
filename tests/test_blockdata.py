import hashlib
import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.model import BlockState

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "src" / "mcblueprint" / "data"
BLOCKDATA_DIR = DATA_DIR / "blockdata"


def test_supported_versions_includes_1_21_11() -> None:
    assert "1.21.11" in blockdata.supported_versions()
    assert blockdata.is_supported("1.21.11")
    assert not blockdata.is_supported("0.0.0")
    assert blockdata.data_version("1.21.11") == 4671


def test_unsupported_version_raises() -> None:
    with pytest.raises(BlueprintError, match="Unsupported Minecraft version"):
        blockdata.load_block_data("0.0.0")
    with pytest.raises(BlueprintError, match="Unsupported Minecraft version"):
        blockdata.data_version("0.0.0")


def test_bundled_data_contents() -> None:
    data = blockdata.load_block_data("1.21.11")
    assert len(data) > 1000
    assert "minecraft:stone_bricks" in data
    assert "minecraft:air" in data
    stairs = data.get("minecraft:oak_stairs")
    assert stairs is not None
    assert stairs.properties["facing"] == ("north", "south", "west", "east")
    assert stairs.default == {
        "facing": "north",
        "half": "bottom",
        "shape": "straight",
        "waterlogged": "false",
    }


def test_load_is_cached() -> None:
    assert blockdata.load_block_data("1.21.11") is blockdata.load_block_data("1.21.11")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("stone_bricks", None),
        ("oak_stairs[facing=south,half=top]", None),
        ("stone_brick", "Unknown block id."),
        ("oak_stairs[facing=up]", "Invalid value 'up' for property 'facing'"),
        ("oak_stairs[color=red]", "Unknown property 'color' for minecraft:oak_stairs"),
        ("stone[facing=north]", "Unknown property 'facing' for minecraft:stone (valid: none)"),
    ],
)
def test_check_state(text: str, expected: str | None) -> None:
    data = blockdata.load_block_data("1.21.11")
    message = data.check_state(BlockState.parse(text))
    if expected is None:
        assert message is None
    else:
        assert message is not None and message.startswith(expected)


def test_complete_fills_defaults() -> None:
    data = blockdata.load_block_data("1.21.11")
    state = data.complete(BlockState.parse("oak_stairs[facing=south]"))
    assert state.to_string() == (
        "minecraft:oak_stairs[facing=south,half=bottom,shape=straight,waterlogged=false]"
    )
    assert data.complete(BlockState.parse("stone")).to_string() == "minecraft:stone"
    with pytest.raises(BlueprintError):
        data.complete(BlockState.parse("not_a_block"))


def test_drawing_attributes_are_resolved_for_state() -> None:
    data = blockdata.load_block_data("1.21.11")
    assert data.drawing(BlockState.parse("air")) == (0, 0, "invisible", "none", None)
    assert data.drawing(BlockState.parse("sea_lantern"))[:2] == (15, 15)
    fixed = data.drawing(BlockState.parse("birch_leaves"))
    assert fixed[3] == "fixed"
    assert fixed[4] == data.tints["fixed"]["minecraft:birch_leaves"]


@pytest.mark.parametrize("version", blockdata.supported_versions())
def test_every_default_is_an_allowed_value(version: str) -> None:
    raw = json.loads(
        (BLOCKDATA_DIR / f"blockdata-{version}.json").read_text(encoding="utf-8")
    )
    for block_id, entry in raw["blocks"].items():
        assert set(entry["default"]) == set(entry["properties"]), block_id
        for name, value in entry["default"].items():
            assert value in entry["properties"][name], (block_id, name)


def test_blockdata_lock_matches_bundled_files() -> None:
    lock = json.loads((DATA_DIR / "lock.json").read_text(encoding="utf-8"))["blockdata"]
    assert lock["repository"] == "https://github.com/mc-blueprint/mc-blueprint-blockdata"
    assert lock["version"] == "1.0.0"
    assert lock["commit"] == "88ac6cb2fa90a99c65edacd1713904649c17d1a6"
    files = {p.name: p for p in BLOCKDATA_DIR.iterdir() if p.is_file()}
    assert set(lock["sha256"]) == set(files)
    for name, path in files.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == lock["sha256"][name], name
    versions = blockdata.supported_versions()
    assert {f"blockdata-{version}.json" for version in versions} == (
        set(files) - {"blockdata.schema.json"}
    )
    keys = [tuple(int(part) for part in v.split(".")) for v in versions]
    assert keys == sorted(keys)
    data_versions = [blockdata.data_version(version) for version in versions]
    assert data_versions == sorted(data_versions) and len(set(data_versions)) == len(data_versions)


@pytest.mark.parametrize(
    ("version", "blocks", "states"),
    [
        ("1.21.11", 1166, 29671),
        ("26.1.2", 1168, 29873),
        ("26.2", 1196, 32366),
        ("26.3", 1286, 35723),
    ],
)
def test_blockdata_v1_contract(version: str, blocks: int, states: int) -> None:
    schema = json.loads((BLOCKDATA_DIR / "blockdata.schema.json").read_text(encoding="utf-8"))
    raw = json.loads((BLOCKDATA_DIR / f"blockdata-{version}.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(raw)
    lock = json.loads((DATA_DIR / "lock.json").read_text(encoding="utf-8"))["blockdata"]
    assert raw["minecraftVersion"] == version
    assert raw["dataVersion"] == blockdata.data_version(version)
    assert raw["generator"] == {
        "repository": "mc-blueprint/mc-blueprint-blockdata",
        "version": lock["version"],
        "commit": lock["commit"],
    }
    assert len(raw["blocks"]) == blocks
    assert sum(
        math.prod(len(values) for values in entry["properties"].values())
        for entry in raw["blocks"].values()
    ) == states
    loaded = blockdata.load_block_data(version)
    for block_id, entry in raw["blocks"].items():
        info = loaded.get(block_id)
        assert info is not None
        assert info.properties == {
            name: tuple(values) for name, values in entry["properties"].items()
        }
        assert info.default == entry["default"]


@pytest.mark.parametrize("version", blockdata.supported_versions())
def test_every_bundled_version_loads(version: str) -> None:
    data = blockdata.load_block_data(version)
    assert len(data) > 1000
    for block_id in ("minecraft:air", "minecraft:stone_bricks", "minecraft:oak_stairs"):
        assert block_id in data
    assert data.check_state(BlockState.parse("oak_stairs[facing=east,half=top]")) is None


def test_newer_versions_keep_the_older_blocks() -> None:
    versions = blockdata.supported_versions()
    for older, newer in zip(versions, versions[1:], strict=False):
        a, b = blockdata.load_block_data(older), blockdata.load_block_data(newer)
        missing = [block_id for block_id in a._blocks if block_id not in b]
        assert missing == [], f"{older} -> {newer} removed {missing}"


def test_version_for_data_version_and_nearest() -> None:
    assert blockdata.version_for_data_version(4671) == "1.21.11"
    assert blockdata.version_for_data_version(5023) == "26.3"
    assert blockdata.version_for_data_version(4700) is None
    assert blockdata.nearest_version(4700) == "1.21.11"
    assert blockdata.nearest_version(4790) == "26.1.2"
    assert blockdata.nearest_version(1) == "1.21.11"
    assert blockdata.nearest_version(10**6) == blockdata.supported_versions()[-1]


def test_26_3_additions() -> None:
    data = blockdata.load_block_data("26.3")
    assert "minecraft:poplar_planks" in data
    assert "minecraft:red_wool_stairs" in data
    assert "minecraft:cinnabar_bricks" in data  # since 26.2
    assert "minecraft:poplar_planks" not in blockdata.load_block_data("1.21.11")
