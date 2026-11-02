"""``path``: a walkway along a polyline of waypoints: axis-aligned segments of a
given width, height changes spread along a segment as single steps (stairs when a
``stairs`` block is given), optional kerb blocks along both sides and lights
(a component or a block) at a fixed spacing.

``points`` are the centre-line cells of the walking surface layer. Expands into
``fill`` / ``set`` / ``component``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Self

from mcblueprint.components import COMPONENT_NAME_PATTERN
from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import BlockSpec, parse_int
from mcblueprint.operations.composite import CompositeOperation, oriented, parse_block, spec_json
from mcblueprint.operations.registry import register
from mcblueprint.operations.tower import _sub

DEFAULT_WIDTH = 2
DEFAULT_CLEARANCE = 2
FACING = {(1, 0): "east", (-1, 0): "west", (0, 1): "south", (0, -1): "north"}


def parse_points(data: Mapping[str, Any], path: str) -> list[Vec3]:
    value = data.get("points")
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) < 2:
        raise BlueprintError(f"{path}.points: must be a list of at least 2 positions")
    points = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, Sequence)
            or len(item) != 3
            or any(not isinstance(v, int) or isinstance(v, bool) for v in item)
        ):
            raise BlueprintError(f"{path}.points[{index}]: must be [x, y, z] integers")
        points.append(Vec3(*item))
    for index, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        if a.x != b.x and a.z != b.z:
            raise BlueprintError(
                f"{path}.points[{index + 1}]: segments must run along x or z "
                f"(from {a.to_list()} to {b.to_list()} both change)"
            )
        if a.x == b.x and a.z == b.z:
            raise BlueprintError(f"{path}.points[{index + 1}]: same x and z as the previous point")
        length = abs(b.x - a.x) + abs(b.z - a.z)
        if abs(b.y - a.y) > length:
            raise BlueprintError(
                f"{path}.points[{index + 1}]: rises {abs(b.y - a.y)} over {length} blocks; "
                "a path can climb at most one block per block"
            )
    return points


def _step(a: Vec3, b: Vec3) -> Vec3:
    return Vec3((b.x > a.x) - (b.x < a.x), 0, (b.z > a.z) - (b.z < a.z))


def segment_cells(a: Vec3, b: Vec3) -> list[Vec3]:
    """Centre-line cells from ``a`` to ``b`` inclusive, y spread evenly."""
    length = abs(b.x - a.x) + abs(b.z - a.z)
    step = _step(a, b)
    rise = abs(b.y - a.y)
    sign = 1 if b.y >= a.y else -1
    cells = []
    for i in range(length + 1):
        climbed = (2 * i * rise + length) // (2 * length)  # round half up, evenly spread
        cells.append(Vec3(a.x + step.x * i, a.y + sign * climbed, a.z + step.z * i))
    return cells


@register
class PathOperation(CompositeOperation):
    type = "path"

    def __init__(
        self,
        path: str,
        points: Sequence[Vec3],
        spec: BlockSpec,
        width: int = DEFAULT_WIDTH,
        edge: BlockState | None = None,
        stairs: BlockState | None = None,
        clearance: int = DEFAULT_CLEARANCE,
        lights: Mapping[str, Any] | None = None,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.points = list(points)
        self.spec = spec
        self.width = width
        self.edge = edge
        self.stairs = stairs
        self.clearance = clearance
        self.lights = lights
        if lights is not None:
            sub = f"{path}.lights"
            self.light_spacing = parse_int(lights, "spacing", sub, minimum=1)
            component = lights.get("component")
            self.light_block = parse_block(lights, "block", sub)
            if (component is None) == (self.light_block is None):
                raise BlueprintError(f"{sub}: give exactly one of 'component' or 'block'")
            if component is not None and (
                not isinstance(component, str) or not COMPONENT_NAME_PATTERN.match(component)
            ):
                raise BlueprintError(f"{sub}.component: must be a component name")
            self.light_component = component

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        return cls(
            path,
            parse_points(data, path),
            BlockSpec.from_dict(data, path),
            parse_int(data, "width", path, minimum=1, default=DEFAULT_WIDTH),
            parse_block(data, "edge", path),
            parse_block(data, "stairs", path),
            parse_int(data, "clearance", path, minimum=0, default=DEFAULT_CLEARANCE),
            _sub(data, "lights", path),
            data.get("comment"),
        )

    # --- geometry -------------------------------------------------------------

    @property
    def across_range(self) -> range:
        return range(-((self.width - 1) // 2), self.width // 2 + 1)

    def centre_line(self) -> list[tuple[Vec3, Vec3]]:
        """``(cell, direction of travel)`` along the whole path, waypoints once."""
        cells: list[tuple[Vec3, Vec3]] = []
        for a, b in zip(self.points, self.points[1:], strict=False):
            step = _step(a, b)
            segment = segment_cells(a, b)
            if cells:
                segment = segment[1:]
            cells.extend((cell, step) for cell in segment)
        return cells

    def _surface(self, index: int, line: list[tuple[Vec3, Vec3]]) -> dict[str, str]:
        cell, step = line[index]
        before = line[index - 1][0] if index > 0 else None
        after = line[index + 1][0] if index + 1 < len(line) else None
        if self.stairs is None:
            return spec_json(self.spec)
        facing: str | None = None
        if before is not None and cell.y == before.y + 1:
            facing = FACING[(step.x, step.z)]  # climbing forwards: full side ahead
        elif after is not None and cell.y == after.y + 1:
            back = line[index + 1][1]
            facing = FACING[(-back.x, -back.z)]  # dropping ahead: full side behind
        if facing is None:
            return spec_json(self.spec)
        return oriented(BlockSpec(block=self.stairs), facing=facing, half="bottom")

    def expand(self) -> list[dict[str, Any]]:
        line = self.centre_line()
        air = {"block": "minecraft:air"}
        ops: list[dict[str, Any]] = []
        placed: set[tuple[int, int]] = set()
        for index, (cell, step) in enumerate(line):
            across = Vec3(step.z, 0, step.x)  # perpendicular in the xz plane
            surface = self._surface(index, line)
            for k in self.across_range:
                p = cell + across * k
                placed.add((p.x, p.z))
                ops.append({"type": "set", "position": p.to_list(), **surface})
                if self.clearance:
                    ops.append(
                        {
                            "type": "fill",
                            "from": (p + Vec3(0, 1, 0)).to_list(),
                            "to": (p + Vec3(0, self.clearance, 0)).to_list(),
                            **air,
                        }
                    )
        # corners: the square spanned by the widths of the incoming and outgoing legs
        for index, point in enumerate(self.points[1:-1], start=1):
            s_in = _step(self.points[index - 1], point)
            s_out = _step(point, self.points[index + 1])
            across_in, across_out = Vec3(s_in.z, 0, s_in.x), Vec3(s_out.z, 0, s_out.x)
            for k1 in self.across_range:
                for k2 in self.across_range:
                    p = point + across_in * k1 + across_out * k2
                    if (p.x, p.z) not in placed:
                        placed.add((p.x, p.z))
                        ops.append({"type": "set", "position": p.to_list(), **spec_json(self.spec)})
                        if self.clearance:
                            ops.append(
                                {
                                    "type": "fill",
                                    "from": (p + Vec3(0, 1, 0)).to_list(),
                                    "to": (p + Vec3(0, self.clearance, 0)).to_list(),
                                    **air,
                                }
                            )
        if self.edge is not None:
            r = self.across_range
            for cell, step in line:
                across = Vec3(step.z, 0, step.x)
                for k in (r.start - 1, r.stop):
                    p = cell + across * k
                    if (p.x, p.z) not in placed:
                        ops.append(
                            {"type": "set", "position": p.to_list(), "block": self.edge.to_string()}
                        )
        if self.lights is not None:
            r = self.across_range
            for index, (cell, step) in enumerate(line):
                if index % self.light_spacing != 0:
                    continue
                across = Vec3(step.z, 0, step.x)
                p = cell + across * (r.stop + (1 if self.edge is not None else 0))
                if (p.x, p.z) in placed:
                    continue
                if self.light_component is not None:
                    ops.append(
                        {
                            "type": "component",
                            "name": self.light_component,
                            "position": (p + Vec3(0, 1, 0)).to_list(),
                        }
                    )
                else:
                    assert self.light_block is not None
                    ops.append(
                        {
                            "type": "set",
                            "position": (p + Vec3(0, 1, 0)).to_list(),
                            "block": self.light_block.to_string(),
                        }
                    )
        return ops
