from pathlib import Path
from typing import Any

import nbtlib
import pytest

from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS, LitematicExporter
from mcblueprint.exporters.litematic import bits_for, pack_bit_array, unpack_bit_array
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
    out = tmp_path / "out.litematic"
    LitematicExporter().export(volume if volume is not None else generate(bp), bp, out)
    return nbtlib.load(out)


def palette_names(region: Any) -> list[str]:
    names = []
    for entry in region["BlockStatePalette"]:
        props = entry.get("Properties")
        suffix = "[" + ",".join(f"{k}={v}" for k, v in sorted(props.items())) + "]" if props else ""
        names.append(str(entry["Name"]) + suffix)
    return names


class TestBitArray:
    @pytest.mark.parametrize("bits", [2, 3, 5, 7, 11, 13])
    def test_roundtrip(self, bits: int) -> None:
        values = [(i * 7919) % (1 << bits) for i in range(200)]
        longs = pack_bit_array(values, bits)
        assert all(-(1 << 63) <= w < (1 << 63) for w in longs)
        assert len(longs) == (200 * bits + 63) // 64
        assert unpack_bit_array(longs, bits, 200) == values

    def test_entries_straddle_words(self) -> None:
        # 22 entries of 3 bits = 66 bits: the last entry crosses the first long boundary
        values = [7] * 22
        longs = pack_bit_array(values, 3)
        assert len(longs) == 2
        assert longs[0] == -1  # all 64 bits set
        assert unpack_bit_array(longs, 3, 22) == values

    @pytest.mark.parametrize("bits", [3, 7, 13, 31])
    def test_packing_chunk_boundary(self, bits: int) -> None:
        values = [(i * 7919) % (1 << bits) for i in range(65551)]
        assert unpack_bit_array(pack_bit_array(values, bits), bits, len(values)) == values

    def test_bits_for(self) -> None:
        assert bits_for(1) == 2
        assert bits_for(4) == 2
        assert bits_for(5) == 3
        assert bits_for(16) == 4
        assert bits_for(17) == 5


class TestLitematic:
    def test_registry(self) -> None:
        assert EXPORTERS["litematic"] is LitematicExporter
        assert LitematicExporter.extension == ".litematic"

    def test_structure(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "fill", "from": [-1, 0, 2], "to": [1, 1, 3], "block": "stone"},
            {"type": "set", "position": [1, 1, 3], "block": "oak_stairs[facing=east]"},
            origin=[0, 0, 0],
            description="desc",
            author="me",
        )
        nbt = export(tmp_path, bp)
        assert nbt.root_name == ""
        assert nbt.gzipped
        assert nbt["Version"] == 6
        assert nbt["MinecraftDataVersion"] == 4671
        meta = nbt["Metadata"]
        assert str(meta["Name"]) == "t" and str(meta["Author"]) == "me"
        assert str(meta["Description"]) == "desc"
        assert meta["RegionCount"] == 1
        assert meta["TotalVolume"] == 12 and meta["TotalBlocks"] == 12
        assert dict(meta["EnclosingSize"]) == {"x": 3, "y": 2, "z": 2}
        region = nbt["Regions"]["Main"]
        assert dict(region["Position"]) == {"x": -1, "y": 0, "z": 2}
        assert dict(region["Size"]) == {"x": 3, "y": 2, "z": 2}
        names = palette_names(region)
        assert names[0] == "minecraft:air"
        assert set(names) == {
            "minecraft:air",
            "minecraft:stone",
            "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]",
        }
        for key in ("TileEntities", "Entities", "PendingBlockTicks", "PendingFluidTicks"):
            assert len(region[key]) == 0

        indices = unpack_bit_array(list(region["BlockStates"]), bits_for(len(names)), 12)
        stairs = names.index(
            "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]"
        )
        # index = (y * sizeZ + z) * sizeX + x relative to the min corner (-1, 0, 2)
        assert indices[(1 * 2 + 1) * 3 + 2] == stairs
        assert all(i == names.index("minecraft:stone") for n, i in enumerate(indices) if n != 11)

    def test_unset_cells_are_air_index_zero(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "set", "position": [0, 0, 0], "block": "stone"},
            {"type": "set", "position": [2, 0, 0], "block": "stone"},
        )
        nbt = export(tmp_path, bp)
        region = nbt["Regions"]["Main"]
        indices = unpack_bit_array(list(region["BlockStates"]), 2, 3)
        assert indices == [1, 0, 1]
        assert nbt["Metadata"]["TotalBlocks"] == 2

    def test_origin_shifts_region_position(self, tmp_path: Path) -> None:
        bp = blueprint(
            {"type": "fill", "from": [10, 5, 10], "to": [12, 5, 12], "block": "stone"},
            origin=[11, 5, 11],
        )
        nbt = export(tmp_path, bp)
        assert dict(nbt["Regions"]["Main"]["Position"]) == {"x": -1, "y": 0, "z": -1}

    def test_large_palette_bits(self, tmp_path: Path) -> None:
        from mcblueprint import blockdata

        data = blockdata.load_block_data("1.21.11")
        ids = [
            block_id
            for block_id in sorted(data._blocks)
            if not data.get(block_id).properties and block_id != "minecraft:air"
        ][:40]
        volume = BlockVolume()
        for i, block_id in enumerate(ids):
            volume.set(Vec3(i, 0, 0), BlockState.parse(block_id))
        bp = blueprint({"type": "set", "position": [0, 0, 0], "block": "stone"})
        region = export(tmp_path, bp, volume)["Regions"]["Main"]
        names = palette_names(region)
        assert len(names) == 41  # air + 40
        indices = unpack_bit_array(list(region["BlockStates"]), bits_for(41), 40)
        assert [names[i] for i in indices] == ids

    def test_empty_volume_rejected(self, tmp_path: Path) -> None:
        bp = blueprint({"type": "set", "position": [0, 0, 0], "block": "stone"})
        with pytest.raises(BlueprintError, match="no blocks"):
            LitematicExporter().export(BlockVolume(), bp, tmp_path / "x.litematic")
