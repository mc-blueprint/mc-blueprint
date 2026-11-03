"""Litematica (``.litematic``) exporter — single region, no entities or tile entities."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np
from nbtlib import Compound, File, Int, List, Long, LongArray, String

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters.base import Exporter, indexed_rows, remap_rows, save_atomic
from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.volume import BlockVolume

AIR = BlockState.parse("minecraft:air")
LITEMATIC_VERSION = 6
REGION_NAME = "Main"


def pack_bit_array(values: Sequence[int] | np.ndarray, bits: int) -> np.ndarray:
    """境界をまたぐLitematicaのpackingを行い、符号付き64bit配列を返す。"""
    total_bits = len(values) * bits
    longs = np.zeros(math.ceil(total_bits / 64), dtype=np.uint64)
    for start in range(0, len(values), 65536):
        chunk = np.asarray(values[start:start + 65536], dtype=np.uint64)
        positions = np.arange(start, start + len(chunk), dtype=np.uint64) * bits
        words, offsets = positions // 64, positions % 64
        np.bitwise_or.at(longs, words, chunk << offsets)
        crossing = offsets + bits > 64
        np.bitwise_or.at(longs, words[crossing] + 1, chunk[crossing] >> (64 - offsets[crossing]))
    return longs.view(np.int64)


def unpack_bit_array(longs: list[int], bits: int, count: int) -> list[int]:
    """Inverse of :func:`pack_bit_array` (used by tests and future importers)."""
    words = [int(w) & 0xFFFFFFFFFFFFFFFF for w in longs]
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
        palette = [None, *volume.palette]
        self.export_rows(indexed_rows(volume, bounds, palette), bounds, blueprint, out_path,
                         palette=palette)

    def export_rows(
        self, rows: Iterable[np.ndarray], bounds: AABB,
        blueprint: Blueprint, out_path: Path, *, palette: list[BlockState | None],
    ) -> None:
        """boundsを覆うY/Z/X順の添字行とpaletteを出力する。Noneはair。"""
        size = bounds.size
        blocks = blockdata.load_block_data(blueprint.minecraft_version)

        # palette index 0 is always air (Litematica treats it as the empty state)
        input_palette = palette
        palette: list[BlockState] = [AIR]
        palette_of: dict[BlockState, int] = {AIR: 0}

        def index_for(state: BlockState | None) -> int:
            state = AIR if state is None else state
            index = palette_of.get(state)
            if index is None:
                index = len(palette)
                palette.append(state if blueprint.terrain is not None else blocks.complete(state))
                palette_of[state] = index
            return index

        indices = np.empty(bounds.volume, dtype=np.uint32)
        non_air = 0
        cursor = 0
        for row in remap_rows(rows, input_palette, index_for):
            indices[cursor:cursor + len(row)] = row
            cursor += len(row)
            non_air += int(np.count_nonzero(row))

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
        save_atomic(File(root, gzipped=True, root_name=""), out_path)
