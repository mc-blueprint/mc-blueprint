"""``spiral_stairs``: staircase winding around a vertical axis, one block up per step."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import BlockSpec, parse_choice, parse_int, parse_vec
from mcblueprint.operations.composite import (
    CompositeOperation,
    is_stairs,
    oriented,
    parse_block,
    spec_json,
)
from mcblueprint.operations.registry import register
from mcblueprint.operations.shapes import disc_cells

DEFAULT_HEADROOM = 3
TURNS = ("clockwise", "counterclockwise")


def ring_path(radius: int, clockwise: bool) -> list[tuple[int, int]]:
    """Cells of the radius-``radius`` ring ordered by angle, starting east (+x).

    Clockwise (seen from above) goes east -> south -> west -> north.
    """
    cells = sorted(disc_cells(radius, "hollow"), key=_angle)
    if not clockwise:
        cells = [cells[0]] + cells[:0:-1]
    return cells


def tread_cells(radius: int, clockwise: bool) -> list[list[tuple[int, int]]]:
    """For each ring step, the wedge of cells it occupies: the ring cell plus every
    inner cell (excluding the axis) whose angle is closest to that ring cell.

    Each (x, z) belongs to exactly one step per turn, so consecutive steps never
    stack on top of each other.
    """
    ring = ring_path(radius, clockwise)
    ring_angles = [_angle(c) for c in ring]
    treads: list[list[tuple[int, int]]] = [[c] for c in ring]
    inner = [c for c in disc_cells(radius, "solid") if c != (0, 0) and c not in set(ring)]
    for cell in sorted(inner, key=lambda c: (-(c[0] ** 2 + c[1] ** 2), _angle(c))):
        angle = _angle(cell)
        best = min(range(len(ring)), key=lambda i: _angular_distance(angle, ring_angles[i]))
        treads[best].append(cell)
    return treads


def _angle(cell: tuple[int, int]) -> float:
    return math.atan2(cell[1], cell[0]) % (2 * math.pi)


def _angular_distance(a: float, b: float) -> float:
    d = abs(a - b) % (2 * math.pi)
    return min(d, 2 * math.pi - d)


def facing_for(offset: tuple[int, int], next_offset: tuple[int, int]) -> str:
    """Horizontal direction of travel from one ring cell to the next (major axis)."""
    dx, dz = next_offset[0] - offset[0], next_offset[1] - offset[1]
    if abs(dx) >= abs(dz):
        return "east" if dx > 0 else "west"
    return "south" if dz > 0 else "north"


@register
class SpiralStairsOperation(CompositeOperation):
    type = "spiral_stairs"

    def __init__(
        self,
        path: str,
        center: Vec3,
        radius: int,
        height: int,
        spec: BlockSpec,
        turn: str = "clockwise",
        headroom: int = DEFAULT_HEADROOM,
        column: BlockState | BlockSpec | None = None,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.center = center
        self.radius = radius
        self.height = height
        self.spec = spec
        self.turn = turn
        self.headroom = headroom
        self.column = column

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        radius = parse_int(data, "radius", path, minimum=1)
        if radius > 8:
            raise BlueprintError(f"{path}.radius: must be <= 8")
        return cls(
            path,
            parse_vec(data, "center", path),
            radius,
            parse_int(data, "height", path, minimum=1),
            BlockSpec.from_dict(data, path),
            parse_choice(data, "turn", path, TURNS, "clockwise"),
            parse_int(data, "headroom", path, minimum=2, default=DEFAULT_HEADROOM),
            parse_block(data, "column", path),
            data.get("comment"),
        )

    def expand(self) -> list[dict[str, Any]]:
        ring = ring_path(self.radius, self.turn == "clockwise")
        stairs = is_stairs(self.spec)
        ops: list[dict[str, Any]] = []
        if self.column is not None:
            ops.append(
                {
                    "type": "fill",
                    "from": self.center.to_list(),
                    "to": (self.center + Vec3(0, self.height - 1, 0)).to_list(),
                    **spec_json(self.column),
                }
            )
        treads = tread_cells(self.radius, self.turn == "clockwise")
        for i in range(self.height):
            index = i % len(ring)
            offset = ring[index]
            following = ring[(i + 1) % len(ring)]
            block = (
                oriented(self.spec, facing=facing_for(offset, following), half="bottom")
                if stairs
                else spec_json(self.spec)
            )
            for ox, oz in treads[index]:
                cell = self.center + Vec3(ox, i, oz)
                ops.append({"type": "set", "position": cell.to_list(), **block})
                ops.append(
                    {
                        "type": "fill",
                        "from": (cell + Vec3(0, 1, 0)).to_list(),
                        "to": (cell + Vec3(0, self.headroom, 0)).to_list(),
                        "block": "minecraft:air",
                    }
                )
        return ops
