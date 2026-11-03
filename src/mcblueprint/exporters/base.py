"""Exporter interface."""

from __future__ import annotations

import gzip
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

import numpy as np
from nbtlib import File

from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.volume import BlockVolume


def indexed_rows(
    volume: BlockVolume, bounds: AABB, palette: list[BlockState | None],
) -> Iterator[np.ndarray]:
    """完成volumeをY/Z/X順のパレット添字の行として読む。"""
    indices = {state: i for i, state in enumerate(palette)}
    for y in range(bounds.min.y, bounds.max.y + 1):
        for z in range(bounds.min.z, bounds.max.z + 1):
            yield np.fromiter(
                (indices[volume.get(Vec3(x, y, z))]
                 for x in range(bounds.min.x, bounds.max.x + 1)),
                dtype=np.uint32, count=bounds.size.x,
            )


def remap_rows(
    rows: Iterable[np.ndarray], palette: list[BlockState | None],
    index_for: Callable[[BlockState | None], int],
) -> Iterator[np.ndarray]:
    """セルを状態へ戻さず、初出順を保って出力パレットへ写す。"""
    mapping = np.full(len(palette), -1, dtype=np.int64)
    for row in rows:
        mapped = mapping[row]
        if np.any(mapped < 0):
            keys, first = np.unique(row, return_index=True)
            for key in keys[np.argsort(first)]:
                if mapping[key] < 0:
                    mapping[key] = index_for(palette[key])
            mapped = mapping[row]
        yield mapped


def save_atomic(nbt: File, out_path: Path) -> None:
    """圧縮・書込みが成功してから完成品を置換する。例外時は既存品を保つ。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f".{out_path.name}-", dir=out_path.parent) as tmp:
        staged = Path(tmp) / "output"
        with gzip.open(staged, "wb", compresslevel=1) as stream:
            nbt.write(stream, byteorder=nbt.byteorder)
        staged.replace(out_path)


class Exporter(ABC):
    format: ClassVar[str]
    extension: ClassVar[str]

    @abstractmethod
    def export(self, volume: BlockVolume, blueprint: Blueprint, out_path: Path) -> None:
        """Write ``volume`` to ``out_path`` in this exporter's format."""
