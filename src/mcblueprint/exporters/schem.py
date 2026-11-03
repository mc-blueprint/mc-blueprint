"""Sponge Schematic v2 (``.schem``) exporter — see docs/ARCHITECTURE.md section 9."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np
from nbtlib import ByteArray, Compound, File, Int, IntArray, Short

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters.base import Exporter, indexed_rows, remap_rows, save_atomic
from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB
from mcblueprint.volume import BlockVolume

AIR = BlockState.parse("minecraft:air")
MAX_SHORT = 32767


def encode_varint(value: int) -> Iterable[int]:
    """7 bits per byte, low bits first, high bit set while more bytes follow."""
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            yield byte | 0x80
        else:
            yield byte
            return


def to_signed_byte(byte: int) -> int:
    return byte - 256 if byte > 127 else byte


def _encode_varints(values: np.ndarray) -> bytes:
    """添字の1行を、セル順を保つ可変長byte列へ変換する。"""
    maximum = int(values.max())
    if maximum < 128:
        return values.astype(np.uint8).tobytes()
    width = (maximum.bit_length() + 6) // 7
    lengths = np.ones(len(values), dtype=np.intp)
    for part in range(1, width):
        lengths += values >= (1 << (7 * part))
    starts = np.cumsum(lengths) - lengths
    packed = np.empty(int(lengths.sum()), dtype=np.uint8)
    for part in range(width):
        mask = lengths > part
        packed[starts[mask] + part] = (
            ((values[mask] >> (7 * part)) & 0x7F)
            | np.where(lengths[mask] > part + 1, 0x80, 0)
        )
    return packed.tobytes()


class SchemExporter(Exporter):
    format = "schem"
    extension = ".schem"

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
        if max(size.x, size.y, size.z) > MAX_SHORT:
            raise BlueprintError(f"Structure too large for .schem: {size.x} x {size.y} x {size.z}")

        blocks = blockdata.load_block_data(blueprint.minecraft_version)
        palette_index: dict[str, int] = {}
        palette_of: dict[BlockState, int] = {}

        def index_for(state: BlockState | None) -> int:
            state = AIR if state is None else state
            index = palette_of.get(state)
            if index is None:
                exported = state if blueprint.terrain is not None else blocks.complete(state)
                key = exported.to_string()
                index = palette_index.setdefault(key, len(palette_index))
                palette_of[state] = index
            return index

        data = bytearray()
        for row in remap_rows(rows, palette, index_for):
            data.extend(_encode_varints(row))

        offset = bounds.min - blueprint.origin
        root = Compound(
            {
                "Version": Int(2),
                "DataVersion": Int(blockdata.data_version(blueprint.minecraft_version)),
                "Width": Short(size.x),
                "Height": Short(size.y),
                "Length": Short(size.z),
                "Offset": IntArray([offset.x, offset.y, offset.z]),
                "Palette": Compound({key: Int(i) for key, i in palette_index.items()}),
                "PaletteMax": Int(len(palette_index)),
                "BlockData": ByteArray(np.frombuffer(data, dtype=np.int8)),
                "Metadata": Compound(
                    {
                        "WEOffsetX": Int(offset.x),
                        "WEOffsetY": Int(offset.y),
                        "WEOffsetZ": Int(offset.z),
                    }
                ),
            }
        )
        save_atomic(File(root, gzipped=True, root_name="Schematic"), out_path)
