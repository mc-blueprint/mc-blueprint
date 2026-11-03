"""``stairs``: straight staircase with guaranteed headroom and a landing opening."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import BlockSpec, parse_int, parse_vec
from mcblueprint.operations.composite import (
    CompositeOperation,
    direction_vec,
    is_stairs,
    oriented,
    parse_block,
    parse_direction,
    right_of,
    spec_json,
)
from mcblueprint.operations.registry import register
from mcblueprint.operations.room import parse_spec

DEFAULT_HEADROOM = 3


@register
class StairsOperation(CompositeOperation):
    type = "stairs"

    def __init__(
        self,
        path: str,
        start: Vec3,
        direction: str,
        height: int,
        spec: BlockSpec,
        width: int = 1,
        headroom: int = DEFAULT_HEADROOM,
        base: BlockState | BlockSpec | None = None,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.start = start
        self.direction = direction
        self.height = height
        self.spec = spec
        self.width = width
        self.headroom = headroom
        self.base = base

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        return cls(
            path,
            parse_vec(data, "start", path),
            parse_direction(data, "direction", path),
            parse_int(data, "height", path, minimum=1),
            BlockSpec.from_dict(data, path),
            parse_int(data, "width", path, minimum=1, default=1),
            parse_int(data, "headroom", path, minimum=2, default=DEFAULT_HEADROOM),
            (parse_spec(data, "base", path) if isinstance(data.get("base"), Mapping)
             else parse_block(data, "base", path)),
            data.get("comment"),
        )

    def expand(self) -> list[dict[str, Any]]:
        forward = direction_vec(self.direction)
        side = direction_vec(right_of(self.direction)) * (self.width - 1)
        step_block = (
            oriented(self.spec, facing=self.direction, half="bottom")
            if is_stairs(self.spec)
            else spec_json(self.spec)
        )
        ops: list[dict[str, Any]] = []
        for i in range(self.height):
            step = self.start + forward * i + Vec3(0, i, 0)
            far = step + side
            if self.base is not None and i > 0:
                below = Vec3(step.x, self.start.y, step.z)
                ops.append(
                    {
                        "type": "fill",
                        "from": below.to_list(),
                        "to": (far - Vec3(0, 1, 0)).to_list(),
                        **(spec_json(self.base) if isinstance(self.base, BlockSpec)
                           else {"block": self.base.to_string()}),
                    }
                )
            ops.append({"type": "fill", "from": step.to_list(), "to": far.to_list(), **step_block})
            ops.append(
                {
                    "type": "fill",
                    "from": (step + Vec3(0, 1, 0)).to_list(),
                    "to": (far + Vec3(0, self.headroom, 0)).to_list(),
                    "block": "minecraft:air",
                }
            )
        # landing: keep headroom clear above the cell the player steps onto after the last step
        landing = self.start + forward * self.height + Vec3(0, self.height, 0)
        ops.append(
            {
                "type": "fill",
                "from": landing.to_list(),
                "to": (landing + side + Vec3(0, self.headroom - 1, 0)).to_list(),
                "block": "minecraft:air",
            }
        )
        return ops
