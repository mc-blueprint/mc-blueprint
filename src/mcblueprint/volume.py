"""Sparse block container produced by the generator and consumed by exporters."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, replace

import numpy as np

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3

MAX_VOLUME = 100_000_000


@dataclass(frozen=True, slots=True)
class CellAttributes:
    """最終配置の属性。owner は予約を書ける責務、source は入力の出典。"""

    role: str | None = None
    element: str | None = None
    face: str | None = None
    reserved: str | None = None
    owner: str | None = None
    source: str | None = None
    operation: str | None = None


EMPTY_ATTRIBUTES = CellAttributes()


def _placement_attributes(
    previous: CellAttributes, incoming: CellAttributes, on_conflict: str,
) -> CellAttributes | None:
    if previous.reserved and (previous.owner is None or previous.owner != incoming.owner):
        if on_conflict == "skip" and incoming.reserved is None:
            return None
        raise BlueprintError(
            f"予約セルへ配置できません: {previous.owner} <- {incoming.operation}"
        )
    if previous.reserved and incoming.reserved is None:
        return replace(incoming, reserved=previous.reserved, owner=previous.owner)
    return incoming


class BlockVolume:
    """Maps positions to block states; later ``set`` calls overwrite earlier ones.

    States are interned into ``palette`` so each cell stores only an index.
    Positions never written are absent (exporters treat them as air).
    write_bounds 指定時は矩形外へのsetをBlueprintErrorとして拒否する。
    """

    def __init__(self, *, write_bounds: AABB | None = None) -> None:
        self._write_bounds = write_bounds
        self._cells: dict[Vec3, int] = {}
        self._palette: list[BlockState] = []
        self._index: dict[BlockState, int] = {}
        self._attributes: dict[Vec3, CellAttributes] = {}

    @property
    def palette(self) -> list[BlockState]:
        return list(self._palette)

    def set(
        self, pos: Vec3, state: BlockState, attributes: CellAttributes = EMPTY_ATTRIBUTES,
        *, on_conflict: str = "error",
    ) -> None:
        if self._write_bounds is not None and not self._write_bounds.contains(pos):
            raise BlueprintError(f"施工範囲外へ配置できません: {pos}")
        attributes = _placement_attributes(self.attributes(pos), attributes, on_conflict)
        if attributes is None:
            return
        index = self._index.get(state)
        if index is None:
            index = len(self._palette)
            self._palette.append(state)
            self._index[state] = index
        self._cells[pos] = index
        if attributes != EMPTY_ATTRIBUTES:
            self._attributes[pos] = attributes
        else:
            self._attributes.pop(pos, None)

    def attributes(self, pos: Vec3) -> CellAttributes:
        """未配置セルは空属性を返す。air の属性も保持する。"""
        return self._attributes.get(pos, EMPTY_ATTRIBUTES)

    def get(self, pos: Vec3) -> BlockState | None:
        index = self._cells.get(pos)
        return None if index is None else self._palette[index]

    def __contains__(self, pos: Vec3) -> bool:
        return pos in self._cells

    def __len__(self) -> int:
        return len(self._cells)

    def __iter__(self) -> Iterator[tuple[Vec3, BlockState]]:
        for pos, index in self._cells.items():
            yield pos, self._palette[index]

    def positions(self) -> Iterator[Vec3]:
        return iter(self._cells)

    def bounds(self) -> AABB | None:
        return AABB.from_points(self._cells)

    def count_by_state(self) -> dict[str, int]:
        """Block state string -> number of cells, most frequent first."""
        counts = Counter(self._cells.values())
        ordered = sorted(counts.items(), key=lambda item: (-item[1], self._palette[item[0]].id))
        return {self._palette[index].to_string(): count for index, count in ordered}


class RegionVolume:
    """限定矩形の配置と属性をuint32配列で保持する。未配置は添字0。

    bounds 外の書込みは拒否する。属性とブロックは同時に更新し、予約の
    owner が異なる書込みは on_conflict に従って拒否または無視する。
    """

    def __init__(self, bounds: AABB) -> None:
        self.region = bounds
        size = bounds.size
        self._blocks = np.zeros((size.y, size.z, size.x), dtype=np.uint32)
        self._attrs = np.zeros_like(self._blocks)
        self._palette: list[BlockState | None] = [None]
        self._index: dict[BlockState, int] = {}
        self._attribute_palette = [EMPTY_ATTRIBUTES]
        self._attribute_index = {EMPTY_ATTRIBUTES: 0}
        self._has_reservations = False

    def _block_index(self, state: BlockState) -> int:
        if state not in self._index:
            self._index[state] = len(self._palette)
            self._palette.append(state)
        return self._index[state]

    def _attribute_id(self, attributes: CellAttributes) -> int:
        if attributes not in self._attribute_index:
            self._attribute_index[attributes] = len(self._attribute_palette)
            self._attribute_palette.append(attributes)
        self._has_reservations |= attributes.reserved is not None
        return self._attribute_index[attributes]

    def _offset(self, pos: Vec3) -> tuple[int, int, int]:
        p = pos - self.region.min
        return p.y, p.z, p.x

    @property
    def nbytes(self) -> int:
        """セル配列のバイト数（パレット・Pythonオブジェクトは含まない）。"""
        return self._blocks.nbytes + self._attrs.nbytes

    @property
    def palette(self) -> list[BlockState]:
        return self._palette[1:]

    def set(
        self, pos: Vec3, state: BlockState, attributes: CellAttributes = EMPTY_ATTRIBUTES,
        *, on_conflict: str = "error",
    ) -> None:
        if not self.region.contains(pos):
            raise BlueprintError(f"領域外へ配置できません: {pos}")
        offset = self._offset(pos)
        attributes = _placement_attributes(self.attributes(pos), attributes, on_conflict)
        if attributes is None:
            return
        self._blocks[offset] = self._block_index(state)
        self._attrs[offset] = self._attribute_id(attributes)

    def set_box(
        self, box: AABB, states: tuple[BlockState, ...], choices: np.ndarray | None,
        attributes: CellAttributes, *, on_conflict: str = "error",
    ) -> None:
        """矩形へ一括配置する。choicesは(y,z,x)順のstates内添字、省略時は単一材。

        予約の判定はsetと共通。skip対象は配置・属性とも保ち、予約種別も継承する。
        """
        if not self.region.contains(box.min) or not self.region.contains(box.max):
            raise BlueprintError(f"領域外へ配置できません: {box}")
        lo, hi = box.min - self.region.min, box.max - self.region.min
        slices = (slice(lo.y, hi.y + 1), slice(lo.z, hi.z + 1), slice(lo.x, hi.x + 1))
        blocks, attrs = self._blocks[slices], self._attrs[slices]
        indices = np.array([self._block_index(state) for state in states], dtype=np.uint32)
        values = indices[0] if choices is None else indices[choices]
        if not self._has_reservations:
            blocks[...] = values
            attrs[...] = self._attribute_id(attributes)
            return
        previous = attrs.copy()
        writes = []
        for old in np.unique(previous):
            incoming = _placement_attributes(self._attribute_palette[old], attributes, on_conflict)
            if incoming is not None:
                writes.append((old, self._attribute_id(incoming)))
        for old, incoming in writes:
            mask = previous == old
            blocks[mask] = values if choices is None else values[mask]
            attrs[mask] = incoming

    def get(self, pos: Vec3) -> BlockState | None:
        if not self.region.contains(pos):
            return None
        return self._palette[int(self._blocks[self._offset(pos)])]

    def attributes(self, pos: Vec3) -> CellAttributes:
        if not self.region.contains(pos):
            return EMPTY_ATTRIBUTES
        return self._attribute_palette[int(self._attrs[self._offset(pos)])]

    def __len__(self) -> int:
        return int(np.count_nonzero(self._blocks))

    def __contains__(self, pos: Vec3) -> bool:
        return self.get(pos) is not None

    def __iter__(self) -> Iterator[tuple[Vec3, BlockState]]:
        # 行単位に列挙し、全領域の座標配列を複製しない。
        for y in range(self._blocks.shape[0]):
            for z in range(self._blocks.shape[1]):
                for x in np.flatnonzero(self._blocks[y, z]):
                    pos = self.region.min + Vec3(int(x), y, z)
                    yield pos, self._palette[int(self._blocks[y, z, x])]

    def positions(self) -> Iterator[Vec3]:
        return (pos for pos, _ in self)

    def bounds(self) -> AABB | None:
        return AABB.from_points(self.positions())
