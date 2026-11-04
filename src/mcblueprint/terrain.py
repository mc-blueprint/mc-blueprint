"""取得範囲と元座標を保持する、不変のオフライン地形スナップショット。"""

from __future__ import annotations

import tempfile
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from types import MappingProxyType

import numpy as np

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3

_AIR = BlockState.parse("minecraft:air")


@dataclass(frozen=True, slots=True)
class TerrainSnapshot:
    """宣言領域と通常 air 以外の元状態を不変に保持する。

    bounds は最小座標が [0, 0, 0] のローカル領域。offset はファイルの
    貼り付け基準に対する最小コーナーであり、絶対ワールド座標とは限らない。
    Minecraft 版は DataVersion が同梱版と完全一致するときだけ設定する。
    input_sha256 は圧縮を含む入力ファイル全体の SHA-256。
    """

    bounds: AABB
    offset: Vec3
    data_version: int | None
    minecraft_version: str | None
    input_sha256: str
    blocks: Mapping[Vec3, BlockState]

    def __post_init__(self) -> None:
        if self.bounds.min != Vec3(0, 0, 0):
            raise ValueError("地形のローカル領域は [0, 0, 0] から始まる必要があります")
        if any(not self.bounds.contains(pos) for pos in self.blocks):
            raise ValueError("地形のセルが宣言領域外にあります")
        object.__setattr__(self, "blocks", MappingProxyType(dict(self.blocks)))

    def get(self, pos: Vec3) -> BlockState | None:
        """ローカル位置の元状態を返す。取得済み air は状態、範囲外は None。"""
        if not self.bounds.contains(pos):
            return None
        return self.blocks.get(pos, _AIR)

    def to_source(self, pos: Vec3) -> Vec3:
        """領域内外を問わず、ローカル位置をファイルの貼り付け基準へ変換する。"""
        return pos + self.offset

    def to_local(self, pos: Vec3) -> Vec3:
        """ファイルの貼り付け基準の位置をローカル位置へ逆変換する。"""
        return pos - self.offset

    @property
    def source_bounds(self) -> AABB:
        """ファイルの貼り付け基準で表した宣言領域（両端を含む）。"""
        return AABB(self.to_source(self.bounds.min), self.to_source(self.bounds.max))


@dataclass(frozen=True, slots=True)
class TerrainPlacement:
    """貼付基準の座標で指定した施工矩形と、不変の元地形。"""

    snapshot: TerrainSnapshot | RegionTerrainSnapshot
    region: AABB
    protected: tuple[AABB, ...] = ()

    def __post_init__(self) -> None:
        source = self.snapshot.source_bounds
        if not source.contains(self.region.min) or not source.contains(self.region.max):
            raise ValueError("terrain.region が地形の取得範囲外です")
        for box in self.protected:
            if all(self.region.min[i] <= box.max[i] and box.min[i] <= self.region.max[i]
                   for i in range(3)):
                raise ValueError("terrain.region が保護範囲と交差しています")


class RegionTerrainSnapshot:
    """密なuint32添字の一時spool。座標辞書や全体volumeを作らず行窓を読む。"""

    to_source = TerrainSnapshot.to_source
    to_local = TerrainSnapshot.to_local
    source_bounds = TerrainSnapshot.source_bounds

    def __init__(self, decoded, digest: str, max_memory_mb: float):
        from mcblueprint.importers import version_for_data_version

        size = decoded.size
        if any(n <= 0 for n in size):
            raise BlueprintError("地形の宣言寸法は正の整数が必要です")
        if not decoded.palette or any(n < 0 for n in decoded.palette):
            raise BlueprintError("地形のpaletteが不正です")
        self.bounds = AABB(Vec3(0, 0, 0), size - Vec3(1, 1, 1))
        self.offset = decoded.offset
        self.data_version = decoded.data_version
        self.minecraft_version = version_for_data_version(decoded.data_version)
        self.input_sha256 = digest
        self.palette = tuple(decoded.palette.values())
        remap = {old: new for new, old in enumerate(decoded.palette)}
        self._cells = tempfile.TemporaryFile()
        self._rows = OrderedDict()
        self.generation_memory_bytes = size.x * 4 * 8 + len(self.palette) * 2048
        self.plan_budget_bytes = int(max_memory_mb * 1024 * 1024 / 4)
        if self.generation_memory_bytes > max_memory_mb * 1024 * 1024 / 4:
            raise BlueprintError("地形の行窓・パレットが limits.max_memory_mb に収まりません")
        indices = iter(decoded.indices)
        self.surface_y = self.source_bounds.min.y
        for row_index in range(size.y * size.z):
            row = np.fromiter(islice(indices, size.x), dtype=np.int64)
            if len(row) != size.x:
                raise BlueprintError("地形のセル数が宣言領域と一致しません")
            unique, inverse = np.unique(row, return_inverse=True)
            if any(index not in remap for index in unique):
                raise BlueprintError("Block index is not in the palette")
            mapped = np.array([remap[index] for index in unique], dtype=np.uint32)
            if any(decoded.palette[index] != _AIR for index in unique):
                self.surface_y = self.offset.y + row_index // size.z
            self._cells.write(mapped[inverse].tobytes())
        if next(indices, None) is not None:
            raise BlueprintError("地形のセル数が宣言領域と一致しません")
        self._cells.flush()

    def row(self, y: int, z: int) -> np.ndarray:
        key = y, z
        if key not in self._rows:
            width = self.bounds.size.x
            self._cells.seek((y * self.bounds.size.z + z) * width * 4)
            self._rows[key] = np.frombuffer(self._cells.read(width * 4), dtype=np.uint32)
            if len(self._rows) > 8:
                self._rows.popitem(last=False)
        self._rows.move_to_end(key)
        return self._rows[key]

    def get(self, pos: Vec3) -> BlockState | None:
        if not self.bounds.contains(pos):
            return None
        return self.palette[int(self.row(pos.y, pos.z)[pos.x])]

    def states_in(self, area: AABB):
        """施工窓で使われる状態を、最初の元座標とともに一度ずつ返す。"""
        lo, hi = self.to_local(area.min), self.to_local(area.max)
        seen = set()
        for y in range(lo.y, hi.y + 1):
            for z in range(lo.z, hi.z + 1):
                row = self.row(y, z)[lo.x:hi.x + 1]
                for index in np.unique(row):
                    if int(index) not in seen:
                        seen.add(int(index))
                        x = int(np.flatnonzero(row == index)[0]) + lo.x
                        yield self.to_source(Vec3(x, y, z)), self.palette[int(index)]

    def ground_heights(self, area: AABB) -> dict[tuple[int, int], int]:
        """領域窓だけの元地表Y。通常air以外の最上面を使い、全air列は取得下端。"""
        heights = {}
        lo, hi = self.to_local(area.min), self.to_local(area.max)
        solid = np.array([state != _AIR for state in self.palette])
        for z in range(lo.z, hi.z + 1):
            row_heights = np.full(hi.x - lo.x + 1, self.offset.y, dtype=np.int32)
            for y in range(self.bounds.size.y):
                present = solid[self.row(y, z)[lo.x:hi.x + 1]]
                row_heights[present] = y + self.offset.y
            for x, height in enumerate(row_heights, area.min.x):
                heights[x, z + self.offset.z] = int(height)
        return heights


class ColumnWrites:
    """施工変更を列内の同材区間で保持する。反復時だけセルへ展開する。"""

    def __init__(self, budget: int):
        self.columns = {}
        self.count = 0
        self.generation_memory_bytes = 0
        self.budget = budget

    def append(self, item):
        pos, state = item
        runs = self.columns.setdefault((pos.x, pos.z), [])
        if runs and runs[-1][1] + 1 == pos.y and runs[-1][2] == state:
            start, _, _ = runs[-1]
            runs[-1] = start, pos.y, state
        else:
            runs.append((pos.y, pos.y, state))
            self.generation_memory_bytes += 256
            if self.generation_memory_bytes > self.budget:
                raise BlueprintError("造成列計画が limits.max_memory_mb に収まりません")
        self.count += 1

    def __len__(self):
        return self.count

    def __iter__(self):
        return self.iter_region(None)

    def iter_region(self, area):
        for (x, z), runs in self.columns.items():
            if area is not None and not (area.min.x <= x <= area.max.x
                                         and area.min.z <= z <= area.max.z):
                continue
            for low, high, state in runs:
                if area is not None:
                    low, high = max(low, area.min.y), min(high, area.max.y)
                for y in range(low, high + 1):
                    yield Vec3(x, y, z), state

    def get(self, pos):
        for low, high, state in self.columns.get((pos.x, pos.z), ()):
            if low <= pos.y <= high:
                return state
        return None
