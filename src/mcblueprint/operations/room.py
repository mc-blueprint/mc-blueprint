"""``room``: floor, perimeter walls, optional ceiling / corner posts, and the door and
window openings of one storey, expanded into ``floor`` / ``wall`` / ``doorway`` / ``window``.

``from.y`` is the floor layer, ``from.y + 1`` the first row of the interior (where a
door's lower half goes), ``to.y`` the ceiling layer (or the top row of the walls when
there is no ceiling).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.arch import ArchSpec, parse_arch_spec
from mcblueprint.operations.base import BlockSpec, parse_int, parse_vec
from mcblueprint.operations.composite import (
    CompositeOperation,
    parse_block,
    parse_direction,
    spec_json,
)
from mcblueprint.operations.registry import register

DEFAULT_SILL = 2
DEFAULT_SPACING = 2


def parse_spec(data: Mapping[str, Any], key: str, path: str) -> BlockSpec | None:
    """``"block"`` string or ``{"block": ...}`` / ``{"palette": ...}`` object."""
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return BlockSpec(block=BlockState.parse(value))
        except BlueprintError as exc:
            raise BlueprintError(f"{path}.{key}: {exc}") from None
    if isinstance(value, Mapping):
        if "role" in value:
            return BlockSpec.from_dict({"block": value}, f"{path}.{key}")
        return BlockSpec.from_dict(value, f"{path}.{key}")
    raise BlueprintError(f"{path}.{key}: must be a block string or an object with 'palette'")


@dataclass(frozen=True)
class Opening:
    """A door or window on one side of the room."""

    path: str
    side: str
    offset: int | None
    width: int
    height: int
    sill: int  # rows above the floor layer (1 for doors)
    count: int
    spacing: int
    block: BlockState | None
    arch: ArchSpec | None

    @property
    def rise(self) -> int:
        return self.arch.rise(self.width) if self.arch is not None else 0

    @property
    def top(self) -> int:
        """Highest row (above the floor layer) of the opening including the arch curve."""
        return self.sill + self.height - 1 + self.rise


def _parse_openings(data: Mapping[str, Any], key: str, path: str, *, door: bool) -> list[Opening]:
    value = data.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise BlueprintError(f"{path}.{key}: must be a list of objects")
    openings = []
    for index, item in enumerate(value):
        sub = f"{path}.{key}[{index}]"
        if not isinstance(item, Mapping):
            raise BlueprintError(f"{sub}: must be an object")
        block = parse_block(item, "door" if door else "block", sub)
        if door and block is not None and not block.id.endswith("_door"):
            raise BlueprintError(f"{sub}.door: must be a door block (e.g. oak_door)")
        width = parse_int(item, "width", sub, minimum=1, default=1)
        if door and block is not None and width > 2:
            raise BlueprintError(f"{sub}: a door needs width 1 or 2")
        openings.append(
            Opening(
                sub,
                parse_direction(item, "side", sub),
                parse_int(item, "offset", sub, minimum=0) if "offset" in item else None,
                width,
                parse_int(item, "height", sub, minimum=2 if door else 1, default=2 if door else 1),
                1 if door else parse_int(item, "sill", sub, minimum=1, default=DEFAULT_SILL),
                1 if door else parse_int(item, "count", sub, minimum=1, default=1),
                1 if door else parse_int(item, "spacing", sub, minimum=0, default=DEFAULT_SPACING),
                block,
                parse_arch_spec(item, sub),
            )
        )
    return openings


@register
class RoomOperation(CompositeOperation):
    type = "room"

    def __init__(
        self,
        path: str,
        start: Vec3,
        end: Vec3,
        wall: BlockSpec,
        floor: BlockSpec | None = None,
        ceiling: BlockSpec | None = None,
        corners: BlockSpec | None = None,
        thickness: int = 1,
        interior: bool = True,
        doors: Sequence[Opening] = (),
        windows: Sequence[Opening] = (),
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.box = AABB.of(start, end)
        self.wall = wall
        self.floor = floor
        self.ceiling = ceiling
        self.corners = corners
        self.thickness = thickness
        self.interior = interior
        self.doors = list(doors)
        self.windows = list(windows)
        size = self.box.size
        if size.x < 2 * thickness + 1 or size.z < 2 * thickness + 1:
            raise BlueprintError(
                f"{path}: the room must be at least {2 * thickness + 1} blocks wide "
                f"(walls of thickness {thickness} on both sides plus an interior)"
            )
        if size.y < 3:
            raise BlueprintError(f"{path}: 'to.y' must be at least 'from.y' + 2 (floor + one row)")
        for opening in self.doors + self.windows:
            self._check_fit(opening)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        wall = parse_spec(data, "wall", path)
        if wall is None:
            raise BlueprintError(f"{path}: missing required 'wall'")
        interior = data.get("interior", True)
        if not isinstance(interior, bool):
            raise BlueprintError(f"{path}.interior: must be a boolean")
        return cls(
            path,
            parse_vec(data, "from", path),
            parse_vec(data, "to", path),
            wall,
            parse_spec(data, "floor", path),
            parse_spec(data, "ceiling", path),
            parse_spec(data, "corners", path),
            parse_int(data, "thickness", path, minimum=1, default=1),
            interior,
            _parse_openings(data, "doors", path, door=True),
            _parse_openings(data, "windows", path, door=False),
            data.get("comment"),
        )

    # --- layout ---------------------------------------------------------------

    @property
    def wall_height(self) -> int:
        return self.box.max.y - self.box.min.y

    @property
    def interior_box(self) -> AABB:
        t = self.thickness
        lo, hi = self.box.min, self.box.max
        top = hi.y - 1 if self.ceiling is not None else hi.y
        return AABB(Vec3(lo.x + t, lo.y + 1, lo.z + t), Vec3(hi.x - t, top, hi.z - t))

    def _span(self, side: str) -> tuple[int, int]:
        """Interior extent along the wall of ``side`` (the cells an opening may use)."""
        inner = self.interior_box
        if side in ("north", "south"):
            return inner.min.x, inner.max.x
        return inner.min.z, inner.max.z

    def _starts(self, opening: Opening) -> list[int]:
        """Along-the-wall coordinate of the first cell of each window / the door."""
        lo, hi = self._span(opening.side)
        total = opening.count * opening.width + (opening.count - 1) * opening.spacing
        if opening.offset is not None:
            first = self.box.min.x if opening.side in ("north", "south") else self.box.min.z
            first += opening.offset
        else:
            first = lo + (hi - lo + 1 - total) // 2
        return [first + i * (opening.width + opening.spacing) for i in range(opening.count)]

    def _check_fit(self, opening: Opening) -> None:
        lo, hi = self._span(opening.side)
        starts = self._starts(opening)
        if starts[0] < lo or starts[-1] + opening.width - 1 > hi:
            raise BlueprintError(
                f"{opening.path}: the opening does not fit between the corners of the "
                f"{opening.side} wall (usable range {lo}..{hi})"
            )
        limit = self.interior_box.max.y - self.box.min.y
        if opening.arch is not None:
            limit = min(limit, self.wall_height - 1)  # the ring needs a row above the opening
        if opening.top > limit:
            raise BlueprintError(
                f"{opening.path}: the opening (with its arch) reaches row {opening.top} above "
                f"the floor but only {limit} rows are available"
            )

    def _opening_ops(self, opening: Opening, *, door: bool) -> list[dict[str, Any]]:
        lo, hi = self.box.min, self.box.max
        y = lo.y + opening.sill
        ops: list[dict[str, Any]] = []
        for start in self._starts(opening):
            if opening.side == "north":
                pos, facing, axis = Vec3(start, y, lo.z), "south", "x"
            elif opening.side == "south":
                pos, facing, axis = (
                    Vec3(start, y, hi.z if door else hi.z - self.thickness + 1),
                    "north",
                    "x",
                )
            elif opening.side == "west":
                pos, facing, axis = Vec3(lo.x, y, start), "east", "z"
            else:
                pos, facing, axis = (
                    Vec3(hi.x if door else hi.x - self.thickness + 1, y, start),
                    "west",
                    "z",
                )
            op: dict[str, Any] = {
                "type": "doorway" if door else "window",
                "position": pos.to_list(),
                "width": opening.width,
                "height": opening.height,
                "depth": self.thickness,
            }
            if door:
                op["facing"] = facing
                if opening.block is not None:
                    op["door"] = opening.block.to_string()
            else:
                op["axis"] = axis
                if opening.block is not None:
                    op["block"] = opening.block.to_string()
            if opening.arch is not None:
                op["arch"] = opening.arch.to_json()
            ops.append(op)
        return ops

    def expand(self) -> list[dict[str, Any]]:
        lo, hi = self.box.min, self.box.max
        ops: list[dict[str, Any]] = []
        if self.floor is not None:
            ops.append(
                {
                    "type": "floor",
                    "from": [lo.x, lo.y, lo.z],
                    "to": [hi.x, lo.y, hi.z],
                    **spec_json(self.floor),
                }
            )
        ops.append(
            {
                "type": "wall",
                "from": [lo.x, lo.y + 1, lo.z],
                "to": [hi.x, lo.y + 1, hi.z],
                "height": self.wall_height,
                "thickness": self.thickness,
                **spec_json(self.wall),
            }
        )
        if self.corners is not None:
            for x, z in ((lo.x, lo.z), (hi.x, lo.z), (lo.x, hi.z), (hi.x, hi.z)):
                ops.append(
                    {
                        "type": "wall",
                        "from": [x, lo.y + 1, z],
                        "to": [x, lo.y + 1, z],
                        "height": self.wall_height,
                        **spec_json(self.corners),
                    }
                )
        inner = self.interior_box
        if self.ceiling is not None:
            ops.append(
                {
                    "type": "floor",
                    "from": [inner.min.x, hi.y, inner.min.z],
                    "to": [inner.max.x, hi.y, inner.max.z],
                    **spec_json(self.ceiling),
                }
            )
        if self.interior:
            ops.append(
                {
                    "type": "fill",
                    "from": inner.min.to_list(),
                    "to": inner.max.to_list(),
                    "block": "minecraft:air",
                }
            )
        for opening in self.windows:
            ops.extend(self._opening_ops(opening, door=False))
        for opening in self.doors:
            ops.extend(self._opening_ops(opening, door=True))
        return ops
