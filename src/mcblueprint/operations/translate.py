"""ネストしたOperationを、任意の原点回転の後に平行移動する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import ExecutionContext, Operation, Transform, parse_vec
from mcblueprint.operations.nested import NestedOperation
from mcblueprint.operations.registry import register


@register
class TranslateOperation(NestedOperation):
    type = "translate"

    def __init__(
        self, path: str, offset: Vec3, operations: Sequence[Operation], comment: str | None = None,
        *, rotation: int = 0,
    ) -> None:
        super().__init__(path, operations, comment)
        if type(rotation) is not int or rotation not in (0, 90, 180, 270):
            raise BlueprintError(f"{path}.rotation: 0 / 90 / 180 / 270 が必要です")
        self.offset = offset
        self.rotation = rotation
        self.transform = Transform.rotation(rotation, Vec3(0, 0, 0)).then(
            Transform.translation(offset)
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        offset = parse_vec(data, "offset", path)
        return cls(path, offset, cls.parse_operations(data, path), data.get("comment"),
                   rotation=data.get("rotation", 0))

    def bounds(self) -> AABB:
        return self.transform.bounds(self.inner_bounds())

    def apply(self, ctx: ExecutionContext) -> None:
        self.apply_with(ctx, self.transform)
