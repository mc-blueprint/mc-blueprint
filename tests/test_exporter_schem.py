import gzip
from pathlib import Path
from typing import Any

import nbtlib
import numpy as np
import pytest

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS, SchemExporter
from mcblueprint.exporters.schem import _encode_varints, encode_varint
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model import BlockState, Vec3
from mcblueprint.volume import BlockVolume


def blueprint(*operations: dict[str, Any], **extra: Any) -> Any:
    return load_blueprint_dict(
        {
            "formatVersion": 1,
            "minecraftVersion": "1.21.11",
            "name": "t",
            "operations": list(operations),
            **extra,
        }
    )


def export(tmp_path: Path, bp: Any, volume: BlockVolume | None = None) -> nbtlib.File:
    out = tmp_path / "out.schem"
    SchemExporter().export(volume if volume is not None else generate(bp), bp, out)
    return nbtlib.load(out)


def decode_varints(data: list[int]) -> list[int]:
    values, value, shift = [], 0, 0
    for byte in data:
        byte &= 0xFF
        value |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
        else:
            values.append(value)
            value, shift = 0, 0
    return values


class TestVarint:
    def test_array_encoding_preserves_scalar_bytes(self) -> None:
        values = np.array([
            0, 127, 128, 16383, 16384, (1 << 21) - 1, 1 << 21,
            (1 << 28) - 1, 1 << 28, (1 << 32) - 1, 1, 0,
        ], dtype=np.uint32)
        expected = bytes(byte for value in values for byte in encode_varint(int(value)))
        assert _encode_varints(values) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, [0]),
            (1, [1]),
            (127, [127]),
            (128, [0x80, 0x01]),
            (300, [0xAC, 0x02]),
            (16384, [0x80, 0x80, 0x01]),
        ],
    )
    def test_encode(self, value: int, expected: list[int]) -> None:
        assert list(encode_varint(value)) == expected


class TestSchem:
    def test_registry(self) -> None:
        assert EXPORTERS["schem"] is SchemExporter
        assert SchemExporter.extension == ".schem"

    def test_header_and_layout(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "fill", "from": [-1, 0, 2], "to": [1, 1, 3], "block": "stone"},
            {"type": "set", "position": [1, 1, 3], "block": "oak_stairs[facing=east]"},
        )
        nbt = export(tmp_path, bp)
        assert nbt.root_name == "Schematic"
        assert nbt.gzipped
        assert nbt["Version"] == 2
        assert nbt["DataVersion"] == 4671
        assert (nbt["Width"], nbt["Height"], nbt["Length"]) == (3, 2, 2)
        assert list(nbt["Offset"]) == [-1, 0, 2]
        assert int(nbt["Metadata"]["WEOffsetX"]) == -1
        palette = {str(k): int(v) for k, v in nbt["Palette"].items()}
        assert set(palette) == {
            "minecraft:stone",
            "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]",
        }
        assert nbt["PaletteMax"] == 2
        assert sorted(palette.values()) == [0, 1]

        indices = decode_varints(list(nbt["BlockData"]))
        assert len(indices) == 3 * 2 * 2
        stairs = palette[
            "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]"
        ]
        # index = x + z*Width + y*Width*Length, relative to min corner (-1, 0, 2)
        assert indices[(1 - -1) + (3 - 2) * 3 + (1 - 0) * 3 * 2] == stairs
        assert all(i == palette["minecraft:stone"] for n, i in enumerate(indices) if n != 11)

    def test_unset_cells_are_air(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "set", "position": [0, 0, 0], "block": "stone"},
            {"type": "set", "position": [2, 0, 0], "block": "stone"},
        )
        nbt = export(tmp_path, bp)
        palette = {str(k): int(v) for k, v in nbt["Palette"].items()}
        indices = decode_varints(list(nbt["BlockData"]))
        assert indices == [
            palette["minecraft:stone"],
            palette["minecraft:air"],
            palette["minecraft:stone"],
        ]

    def test_explicit_air_shares_palette_entry(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "fill", "from": [0, 0, 0], "to": [2, 0, 0], "block": "stone"},
            {"type": "set", "position": [1, 0, 0], "block": "air"},
        )
        nbt = export(tmp_path, bp)
        assert nbt["PaletteMax"] == 2

    def test_origin_shifts_offset(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "fill", "from": [10, 5, 10], "to": [12, 5, 12], "block": "stone"},
            origin=[11, 5, 11],
        )
        nbt = export(tmp_path, bp)
        assert list(nbt["Offset"]) == [-1, 0, -1]
        assert [int(nbt["Metadata"][k]) for k in ("WEOffsetX", "WEOffsetY", "WEOffsetZ")] == [
            -1,
            0,
            -1,
        ]

    def test_large_palette_uses_multibyte_varints(self, tmp_path: Path) -> None:
        data = blockdata.load_block_data("1.21.11")
        ids = [
            block_id
            for block_id in sorted(data._blocks)
            if not data.get(block_id).properties and block_id != "minecraft:air"
        ][:150]
        assert len(ids) == 150
        volume = BlockVolume()
        for i, block_id in enumerate(ids):
            volume.set(Vec3(i, 0, 0), BlockState.parse(block_id))
        bp = blueprint({"type": "set", "position": [0, 0, 0], "block": "stone"})
        nbt = export(tmp_path, bp, volume)
        assert nbt["PaletteMax"] == 150
        raw = list(nbt["BlockData"])
        assert len(raw) == 128 + 2 * 22  # indices >= 128 take two bytes
        palette = {str(k): int(v) for k, v in nbt["Palette"].items()}
        assert decode_varints(raw) == [palette[block_id] for block_id in ids]
        assert min(b & 0xFF for b in raw[:128]) >= 0

    def test_empty_volume_rejected(self, tmp_path: Path) -> None:
        bp = blueprint({"type": "set", "position": [0, 0, 0], "block": "stone"})
        with pytest.raises(BlueprintError, match="no blocks"):
            SchemExporter().export(BlockVolume(), bp, tmp_path / "x.schem")

    def test_creates_parent_directory_and_is_gzip(self, tmp_path: Path) -> None:
        bp = blueprint({"type": "set", "position": [0, 0, 0], "block": "stone"})
        out = tmp_path / "nested" / "dir" / "x.schem"
        SchemExporter().export(generate(bp), bp, out)
        assert out.exists()
        with gzip.open(out, "rb") as fh:
            assert fh.read(3) == b"\n\x00\t"  # TAG_Compound with a 9-char name
