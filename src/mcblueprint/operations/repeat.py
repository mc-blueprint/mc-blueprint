"""``repeat``: run nested operations ``count`` times, shifting by ``offset`` each time."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import ExecutionContext, Operation, Transform, parse_int, parse_vec
from mcblueprint.operations.nested import NestedOperation
from mcblueprint.operations.registry import register

MAX_COUNT = 512


@register
class RepeatOperation(NestedOperation):
    type = "repeat"

    def __init__(
        self,
        path: str,
        count: int,
        offset: Vec3,
        operations: Sequence[Operation],
        comment: str | None = None,
    ) -> None:
        super().__init__(path, operations, comment)
        self.count = count
        self.offset = offset

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        count = parse_int(data, "count", path, minimum=1)
        if count > MAX_COUNT:
            raise BlueprintError(f"{path}.count: must be <= {MAX_COUNT}")
        offset = parse_vec(data, "offset", path)
        return cls(path, count, offset, cls.parse_operations(data, path), data.get("comment"))

    def bounds(self) -> AABB:
        inner = self.inner_bounds()
        last = Transform.translation(self.offset * (self.count - 1)).bounds(inner)
        return inner.union(last)

    def apply(self, ctx: ExecutionContext) -> None:
        for i in range(self.count):
            self.apply_with(ctx, Transform.translation(self.offset * i), instance=f"repeat#{i}")
