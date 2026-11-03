"""``fill``: solid cuboid."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Self

from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext
from mcblueprint.operations.box_like import CornerOperation, iter_box
from mcblueprint.operations.registry import register


@register
class FillOperation(CornerOperation):
    type = "fill"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        start, end = cls.parse_corners(data, path)
        return cls(path, start, end, BlockSpec.from_dict(data, path), data.get("comment"))

    def cells(self) -> Iterable[Vec3]:
        return iter_box(self.box)

    def apply(self, ctx: ExecutionContext) -> None:
        ctx.fill(self.box, self.spec)
