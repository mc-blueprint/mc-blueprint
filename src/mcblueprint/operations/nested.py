"""Shared base for operations that contain other operations (``mirror``, ``repeat``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import reduce
from typing import Any

from mcblueprint.errors import BlueprintError
from mcblueprint.model.vec import AABB
from mcblueprint.operations.base import ExecutionContext, Operation, Transform
from mcblueprint.operations.registry import build_operations


class NestedOperation(Operation):
    def __init__(self, path: str, operations: Sequence[Operation], comment: str | None = None):
        super().__init__(path, comment)
        self.operations = list(operations)

    @classmethod
    def parse_operations(cls, data: Mapping[str, Any], path: str) -> list[Operation]:
        items = data.get("operations")
        if not isinstance(items, list) or not items:
            raise BlueprintError(f"{path}.operations: must be a non-empty array")
        return build_operations(items, f"{path}.operations")

    def inner_bounds(self) -> AABB:
        return reduce(AABB.union, (op.bounds() for op in self.operations))

    def apply_with(
        self, ctx: ExecutionContext, transform: Transform, *, instance: str = "",
    ) -> None:
        child = ctx.child(transform, instance=instance)
        for op in self.operations:
            child.execute(op)
