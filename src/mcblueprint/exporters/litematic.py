"""Litematica (``.litematic``) exporter — single region, no entities or tile entities."""

from __future__ import annotations

import math
import time
from pathlib import Path

from nbtlib import Compound, File, Int, List, Long, LongArray, String

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters.base import Exporter
from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import Vec3
from mcblueprint.volume import BlockVolume

AIR = BlockState.parse("minecraft:air")
LITEMATIC_VERSION = 6
REGION_NAME = "Main"


def pack_bit_array(values: list[int], bits: int) -> list[int]:
    """Litematica bit packing: entries straddle long boundaries; returns signed longs."""
    total_bits = len(values) * bits
    longs = [0] * math.ceil(total_bits / 64)
    for index, value in enumerate(values):
        start = index * bits
        word, offset = divmod(start, 64)
        longs[word] |= (value << offset) & 0xFFFFFFFFFFFFFFFF
        end_word = (start + bits - 1) // 64
        if end_word != word:
            longs[end_word] |= value >> (64 - offset)
    return [w - (1 << 64) if w >= (1 << 63) else w for w in longs]


def unpack_bit_array(longs: list[int], bits: int, count: int) -> list[int]:
    """Inverse of :func:`pack_bit_array` (used by tests and future importers)."""
    words = [w & 0xFFFFFFFFFFFFFFFF for w in longs]
    mask = (1 << bits) - 1
    values = []
    for index in range(count):
        start = index * bits
        word, offset = divmod(start, 64)
        value = words[word] >> offset
        end_word = (start + bits - 1) // 64
        if end_word != word:
            value |= words[end_word] << (64 - offset)
        values.append(value & mask)
    return values


def bits_for(palette_size: int) -> int:
    return max(2, (palette_size - 1).bit_length())


def _vec_compound(v: Vec3) -> Compound:
    return Compound({"x": Int(v.x), "y": Int(v.y), "z": Int(v.z)})


def _state_compound(state: BlockState) -> Compound:
    entry = Compound({"Name": String(state.id)})
    if state.properties:
        entry["Properties"] = Compound({k: String(v) for k, v in state.properties})
    return entry


class LitematicExporter(Exporter):
    format = "litematic"
    extension = ".litematic"

    def export(self, volume: BlockVolume, blueprint: Blueprint, out_path: Path) -> None:
        bounds = volume.bounds()
        if bounds is None:
            raise BlueprintError("Nothing to export: no blocks were generated")
        size = bounds.size
        blocks = blockdata.load_block_data(blueprint.minecraft_version)

        # palette index 0 is always air (Litematica treats it as the empty state)
        palette: list[BlockState] = [AIR]
        palette_of: dict[BlockState, int] = {AIR: 0}

        def index_for(state: BlockState) -> int:
            index = palette_of.get(state)
            if index is None:
                index = len(palette)
                palette.append(state if blueprint.terrain is not None else blocks.complete(state))
                palette_of[state] = index
            return index

        indices: list[int] = []
        non_air = 0
        for y in range(bounds.min.y, bounds.max.y + 1):
            for z in range(bounds.min.z, bounds.max.z + 1):
                for x in range(bounds.min.x, bounds.max.x + 1):
                    state = volume.get(Vec3(x, y, z))
                    if state is None:
                        indices.append(0)
                        continue
                    index = index_for(state)
                    indices.append(index)
                    if index != 0:
                        non_air += 1

        bits = bits_for(len(palette))
        now = Long(int(time.time() * 1000))
        region = Compound(
            {
                "Position": _vec_compound(bounds.min - blueprint.origin),
                "Size": _vec_compound(size),
                "BlockStatePalette": List[Compound]([_state_compound(s) for s in palette]),
                "BlockStates": LongArray(pack_bit_array(indices, bits)),
                "TileEntities": List[Compound]([]),
                "Entities": List[Compound]([]),
                "PendingBlockTicks": List[Compound]([]),
                "PendingFluidTicks": List[Compound]([]),
            }
        )
        root = Compound(
            {
                "MinecraftDataVersion": Int(blockdata.data_version(blueprint.minecraft_version)),
                "Version": Int(LITEMATIC_VERSION),
                "Metadata": Compound(
                    {
                        "Name": String(blueprint.name),
                        "Author": String(blueprint.author or "mc-blueprint"),
                        "Description": String(blueprint.description or ""),
                        "RegionCount": Int(1),
                        "TotalVolume": Int(size.x * size.y * size.z),
                        "TotalBlocks": Int(non_air),
                        "EnclosingSize": _vec_compound(size),
                        "TimeCreated": now,
                        "TimeModified": now,
                    }
                ),
                "Regions": Compound({REGION_NAME: region}),
            }
        )
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        File(root, gzipped=True, root_name="").save(str(out_path))
