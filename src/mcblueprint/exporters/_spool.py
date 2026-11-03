"""完成coreを退避する内部用の行指向spool。保存形式の互換性は保証しない。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

import numpy as np

from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB


class RegionSpool:
    def __init__(self, path: Path, bounds: AABB) -> None:
        self.path = path
        self.bounds = bounds
        self.palette: list[BlockState | None] = [None]
        self.index: dict[BlockState | None, int] = {None: 0}

    def write(self, regions: Iterable[GeneratedRegion]) -> None:
        boxes: list[AABB] = []
        with self.path.open("w+b") as stream:
            stream.truncate(self.bounds.volume * 4)
            iterator = iter(regions)
            while True:
                try:
                    region = next(iterator)
                except StopIteration:
                    break
                core = region.core
                if not self.bounds.contains(core.min) or not self.bounds.contains(core.max):
                    raise BlueprintError("出力範囲外のcoreです")
                if any(all(core.min[i] <= box.max[i] and box.min[i] <= core.max[i]
                           for i in range(3)) for box in boxes):
                    raise BlueprintError("出力coreが重複しています")
                if not region.volume.region.contains(core.min) or not region.volume.region.contains(
                    core.max
                ):
                    raise BlueprintError("coreが生成volumeの範囲外です")
                mapping = []
                for state in [None, *region.volume.palette]:
                    index = self.index.get(state)
                    if index is None:
                        index = len(self.palette)
                        self.index[state] = index
                        self.palette.append(state)
                    mapping.append(index)
                lookup = np.asarray(mapping, dtype="<u4")
                low = core.min - region.volume.region.min
                size = core.size
                # RegionVolume内部の(y,z,x)添字を直接使い、haloは読み出さない。
                for y in range(size.y):
                    for z in range(size.z):
                        row = region.volume._blocks[
                            low.y + y, low.z + z, low.x:low.x + size.x,
                        ]
                        values = lookup[row]
                        self._seek(stream, core.min.y + y, core.min.z + z, core.min.x)
                        stream.write(values.tobytes())
                del row
                boxes.append(core)
                del region
        if sum(box.volume for box in boxes) != self.bounds.volume:
            raise BlueprintError("出力coreが全体範囲を被覆していません")

    def _seek(self, stream: BinaryIO, y: int, z: int, x: int) -> None:
        size, low = self.bounds.size, self.bounds.min
        stream.seek((((y - low.y) * size.z + z - low.z) * size.x + x - low.x) * 4)

    def rows(self) -> Iterator[np.ndarray]:
        with self.path.open("rb") as stream:
            width = self.bounds.size.x
            for _ in range(self.bounds.size.y * self.bounds.size.z):
                data = stream.read(width * 4)
                if len(data) != width * 4:
                    raise BlueprintError("出力spoolの行が欠落しています")
                yield np.frombuffer(data, dtype="<u4")
