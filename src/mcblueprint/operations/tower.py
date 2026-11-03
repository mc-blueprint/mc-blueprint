"""``tower``: round or square tower shell with storey floors, a spiral staircase, a
roof platform with battlements, windows on the cardinal sides and a door.

``position`` is the centre of the ground floor layer; the walls rise ``height`` rows
above it and the roof platform sits one row above the walls (overhanging by one).
Expands into ``cylinder`` / ``circle`` (round) or ``wall`` / ``floor`` (square),
``fill``, ``spiral_stairs``, ``window`` and ``doorway``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.arch import parse_arch_spec
from mcblueprint.operations.base import BlockSpec, parse_choice, parse_int, parse_vec
from mcblueprint.operations.composite import (
    HORIZONTAL,
    CompositeOperation,
    parse_block,
    parse_direction,
    spec_json,
)
from mcblueprint.operations.registry import register
from mcblueprint.operations.room import parse_spec
from mcblueprint.operations.shapes import disc_cells
from mcblueprint.operations.spiral_stairs import TURNS

SHAPES = ("round", "square")
DEFAULT_STAIRS_RADIUS = 2
DEFAULT_MERLON_SPACING = 1
DEFAULT_SILL = 2


def _angle(cell: tuple[int, int]) -> float:
    return math.atan2(cell[1], cell[0]) % (2 * math.pi)


def square_ring(half: int) -> set[tuple[int, int]]:
    """Perimeter cells of the square ``-half..half``."""
    return {
        (x, z)
        for x in range(-half, half + 1)
        for z in range(-half, half + 1)
        if abs(x) == half or abs(z) == half
    }


def merlon_cells(ring: set[tuple[int, int]], spacing: int) -> list[tuple[int, int]]:
    """Every ``spacing + 1``-th ring cell going round from east (the merlons)."""
    ordered = sorted(ring, key=_angle)
    return ordered[:: spacing + 1]


def _sub(data: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any] | None:
    value = data.get(key)
    if value is None or value is False:
        return None
    if value is True:
        return {}
    if not isinstance(value, Mapping):
        raise BlueprintError(f"{path}.{key}: must be an object (or true / false)")
    return value


@register
class TowerOperation(CompositeOperation):
    type = "tower"

    def __init__(
        self,
        path: str,
        position: Vec3,
        shape: str,
        radius: int,
        height: int,
        wall: BlockSpec,
        floor: BlockSpec | None = None,
        floors: int | None = None,
        stairs: Mapping[str, Any] | None = None,
        battlement: Mapping[str, Any] | None = None,
        windows: Mapping[str, Any] | None = None,
        door: Mapping[str, Any] | None = None,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.position = position
        self.shape = shape
        self.radius = radius  # outer radius (round) or half width (square)
        self.height = height
        self.wall = wall
        self.floor = floor or wall
        self.floors = floors
        self._parse_stairs(stairs, path)
        self._parse_battlement(battlement, path)
        self._parse_windows(windows, path)
        self._parse_door(door, path)

    # --- sub-objects ------------------------------------------------------------

    def _parse_stairs(self, data: Mapping[str, Any] | None, path: str) -> None:
        self.stairs = data
        if data is None:
            return
        sub = f"{path}.stairs"
        limit = self.radius - 2
        if limit < 1:
            raise BlueprintError(
                f"{path}: a tower with stairs needs a radius of at least 3 (got {self.radius}); "
                "set 'stairs': false to omit them"
            )
        self.stairs_radius = parse_int(
            data, "radius", sub, minimum=1, default=min(DEFAULT_STAIRS_RADIUS, limit)
        )
        if self.stairs_radius > limit:
            raise BlueprintError(
                f"{sub}.radius: must be at most {limit} (radius - 2) to leave a walkway"
            )
        self.stairs_spec = parse_spec(data, "block", sub) or self.floor
        self.stairs_turn = parse_choice(data, "turn", sub, TURNS, "clockwise")
        self.stairs_column = parse_block(data, "column", sub)

    def _parse_battlement(self, data: Mapping[str, Any] | None, path: str) -> None:
        self.battlement = data
        if data is None:
            return
        sub = f"{path}.battlement"
        self.battlement_spec = parse_spec(data, "block", sub) or self.wall
        self.merlon_spacing = parse_int(
            data, "spacing", sub, minimum=0, default=DEFAULT_MERLON_SPACING
        )

    def _parse_windows(self, data: Mapping[str, Any] | None, path: str) -> None:
        self.windows = data
        if data is None:
            return
        sub = f"{path}.windows"
        self.window_sill = parse_int(data, "sill", sub, minimum=1, default=DEFAULT_SILL)
        self.window_width = parse_int(data, "width", sub, minimum=1, default=1)
        self.window_height = parse_int(data, "height", sub, minimum=1, default=1)
        self.window_block = parse_block(data, "block", sub)
        self.window_arch = parse_arch_spec(data, sub)
        sides = data.get("sides", list(HORIZONTAL))
        if not isinstance(sides, list) or any(s not in HORIZONTAL for s in sides):
            raise BlueprintError(f"{sub}.sides: must be a list of north / south / east / west")
        self.window_sides = sides
        storey = self.floors if self.floors is not None else self.height
        rise = self.window_arch.rise(self.window_width) if self.window_arch else 0
        self.window_top = (
            self.window_sill + self.window_height - 1 + rise + (1 if self.window_arch else 0)
        )
        if self.window_top > storey - 1:
            raise BlueprintError(
                f"{sub}: the window (with its arch) needs {self.window_top} rows above a floor "
                f"but a storey has only {storey - 1}"
            )

    def _parse_door(self, data: Mapping[str, Any] | None, path: str) -> None:
        self.door = data
        if data is None:
            return
        sub = f"{path}.door"
        self.door_side = parse_direction(data, "side", sub, "north")
        self.door_width = parse_int(data, "width", sub, minimum=1, default=1)
        self.door_height = parse_int(data, "height", sub, minimum=2, default=2)
        self.door_block = parse_block(data, "block", sub)
        if self.door_block is not None and not self.door_block.id.endswith("_door"):
            raise BlueprintError(f"{sub}.block: must be a door block (e.g. oak_door)")
        if self.door_block is not None and self.door_width > 2:
            raise BlueprintError(f"{sub}: a door needs width 1 or 2")
        self.door_arch = parse_arch_spec(data, sub)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        shape = parse_choice(data, "shape", path, SHAPES, "round")
        if shape == "round":
            if "size" in data:
                raise BlueprintError(f"{path}: 'size' is only for square towers; use 'radius'")
            radius = parse_int(data, "radius", path, minimum=2)
        else:
            if "radius" in data:
                raise BlueprintError(f"{path}: 'radius' is only for round towers; use 'size'")
            size = parse_int(data, "size", path, minimum=5)
            if size % 2 == 0:
                raise BlueprintError(f"{path}.size: must be odd so the centre is a block")
            radius = (size - 1) // 2
        wall = parse_spec(data, "wall", path)
        if wall is None:
            raise BlueprintError(f"{path}: missing required 'wall'")
        stairs = _sub(data, "stairs", path) if "stairs" in data else {}
        return cls(
            path,
            parse_vec(data, "position", path),
            shape,
            radius,
            parse_int(data, "height", path, minimum=3),
            wall,
            parse_spec(data, "floor", path),
            parse_int(data, "floors", path, minimum=3) if "floors" in data else None,
            stairs,
            _sub(data, "battlement", path),
            _sub(data, "windows", path),
            _sub(data, "door", path),
            data.get("comment"),
        )

    # --- geometry -------------------------------------------------------------

    @property
    def storey_floors(self) -> list[int]:
        """y of every floor layer: the ground floor and each storey floor that leaves
        at least two rows below the roof platform."""
        base = self.position.y
        ys = [base]
        if self.floors is not None:
            y = base + self.floors
            while y <= base + self.height - 2:
                ys.append(y)
                y += self.floors
        return ys

    @property
    def platform_y(self) -> int:
        return self.position.y + self.height + 1

    def _disc(self, y: int, radius: int, mode: str, spec: BlockSpec) -> dict[str, Any]:
        c = self.position
        if self.shape == "round":
            return {
                "type": "circle",
                "center": [c.x, y, c.z],
                "radius": radius,
                "mode": mode,
                **spec_json(spec),
            }
        corner_a, corner_b = [c.x - radius, y, c.z - radius], [c.x + radius, y, c.z + radius]
        if mode == "solid":
            return {"type": "floor", "from": corner_a, "to": corner_b, **spec_json(spec)}
        return {"type": "wall", "from": corner_a, "to": corner_b, "height": 1, **spec_json(spec)}

    def _shell(self, y: int, height: int, mode: str, block: dict[str, str]) -> dict[str, Any]:
        c, r = self.position, self.radius
        if self.shape == "round":
            return {
                "type": "cylinder",
                "center": [c.x, y, c.z],
                "radius": r,
                "height": height,
                "mode": mode,
                **block,
            }
        corner_a, corner_b = [c.x - r, y, c.z - r], [c.x + r, y, c.z + r]
        if mode == "solid":
            return {
                "type": "fill",
                "from": corner_a,
                "to": [c.x + r, y + height - 1, c.z + r],
                **block,
            }
        return {"type": "wall", "from": corner_a, "to": corner_b, "height": height, **block}

    def _ring(self, radius: int) -> set[tuple[int, int]]:
        if self.shape == "round":
            return disc_cells(radius, "hollow")
        return square_ring(radius)

    def _side_cell(self, side: str, width: int) -> tuple[Vec3, str, str]:
        """(position, axis, inward facing) of a ``width`` opening centred on ``side``."""
        c, r = self.position, self.radius
        half = (width - 1) // 2
        if side == "north":
            return Vec3(c.x - half, 0, c.z - r), "x", "south"
        if side == "south":
            return Vec3(c.x - half, 0, c.z + r), "x", "north"
        if side == "east":
            return Vec3(c.x + r, 0, c.z - half), "z", "west"
        return Vec3(c.x - r, 0, c.z - half), "z", "east"

    def expand(self) -> list[dict[str, Any]]:
        c, r, h = self.position, self.radius, self.height
        air = {"block": "minecraft:air"}
        ops: list[dict[str, Any]] = [self._shell(c.y + 1, h, "solid", air)]
        for y in self.storey_floors:
            ops.append(self._disc(y, r, "solid", self.floor))
        ops.append(self._shell(c.y + 1, h, "hollow", spec_json(self.wall)))
        ops.append(self._disc(self.platform_y, r + 1, "solid", self.wall))
        if self.battlement is not None:
            ops.append(self._disc(self.platform_y + 1, r + 1, "hollow", self.battlement_spec))
            for x, z in merlon_cells(self._ring(r + 1), self.merlon_spacing):
                ops.append(
                    {
                        "type": "set",
                        "position": [c.x + x, self.platform_y + 2, c.z + z],
                        **spec_json(self.battlement_spec),
                    }
                )
        if self.stairs is not None:
            stairs: dict[str, Any] = {
                "type": "spiral_stairs",
                "center": [c.x, c.y + 1, c.z],
                "radius": self.stairs_radius,
                "height": h + 1,  # the last step lands on the roof platform
                "turn": self.stairs_turn,
                **spec_json(self.stairs_spec),
            }
            if self.stairs_column is not None:
                stairs["column"] = spec_json(self.stairs_column)["block"]
            ops.append(stairs)
        if self.windows is not None:
            floors = self.storey_floors
            for index, y in enumerate(floors):
                # rows between this floor and the next floor / the roof platform
                above = (floors[index + 1] if index + 1 < len(floors) else self.platform_y) - y - 1
                if self.window_top > above:
                    continue  # a short top storey gets no window
                for side in self.window_sides:
                    if self.door is not None and y == c.y and side == self.door_side:
                        continue  # the door takes this side of the ground floor
                    pos, axis, _ = self._side_cell(side, self.window_width)
                    window: dict[str, Any] = {
                        "type": "window",
                        "position": [pos.x, y + self.window_sill, pos.z],
                        "axis": axis,
                        "width": self.window_width,
                        "height": self.window_height,
                    }
                    if self.window_block is not None:
                        window["block"] = self.window_block.to_string()
                    if self.window_arch is not None:
                        window["arch"] = self.window_arch.to_json()
                    ops.append(window)
        if self.door is not None:
            pos, _, facing = self._side_cell(self.door_side, self.door_width)
            door: dict[str, Any] = {
                "type": "doorway",
                "position": [pos.x, c.y + 1, pos.z],
                "facing": facing,
                "width": self.door_width,
                "height": self.door_height,
            }
            if self.door_block is not None:
                door["door"] = self.door_block.to_string()
            if self.door_arch is not None:
                door["arch"] = self.door_arch.to_json()
            ops.append(door)
        return ops
