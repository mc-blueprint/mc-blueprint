"""``component``: place a reusable part loaded from ``components/<name>.json``."""

from __future__ import annotations

from collections.abc import Mapping
from functools import reduce
from typing import Any, Self

from mcblueprint import components
from mcblueprint.errors import BlueprintError
from mcblueprint.model.palette import Palette
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import ExecutionContext, Operation, Transform, parse_vec
from mcblueprint.operations.registry import register

ANGLES = (0, 90, 180, 270)


@register
class ComponentOperation(Operation):
    type = "component"

    def __init__(
        self,
        path: str,
        name: str,
        position: Vec3,
        rotation: int,
        operations: list[Operation],
        palettes: dict[str, Palette],
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.name = name
        self.position = position
        self.rotation = rotation
        self.operations = operations
        self.palettes = palettes
        self.transform = Transform.rotation(rotation, Vec3(0, 0, 0)).then(
            Transform.translation(position)
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        name = data.get("name")
        if not isinstance(name, str):
            raise BlueprintError(f"{path}: missing required 'name'")
        position = parse_vec(data, "position", path)
        rotation = data.get("rotation", 0)
        if not isinstance(rotation, int) or isinstance(rotation, bool) or rotation not in ANGLES:
            raise BlueprintError(f"{path}.rotation: must be one of 0, 90, 180, 270")

        # imported here to avoid a circular import (loader -> registry -> component -> loader)
        from mcblueprint.loader import build_palettes
        from mcblueprint.operations.registry import build_operations

        try:
            raw = components.load_component(name)
            with components.loading(name):
                operations = build_operations(raw["operations"], f"{path}<{name}>.operations")
                palettes = build_palettes(raw.get("palettes", {}))
        except BlueprintError as exc:
            message = str(exc)
            prefix = f"{path}: "
            raise BlueprintError(
                message if message.startswith(path) else prefix + message
            ) from None
        return cls(path, name, position, rotation, operations, palettes, data.get("comment"))

    def bounds(self) -> AABB:
        inner = reduce(AABB.union, (op.bounds() for op in self.operations))
        return self.transform.bounds(inner)

    def apply(self, ctx: ExecutionContext) -> None:
        child = ctx.child(self.transform, palettes=self.palettes)
        for op in self.operations:
            child.execute(op)
