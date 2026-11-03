"""``pillar``: vertical column with optional base and cap blocks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import BlockSpec, parse_int, parse_vec
from mcblueprint.operations.composite import CompositeOperation, parse_block, spec_json
from mcblueprint.operations.registry import register


@register
class PillarOperation(CompositeOperation):
    type = "pillar"

    def __init__(
        self,
        path: str,
        position: Vec3,
        height: int,
        spec: BlockSpec,
        base: BlockState | BlockSpec | None = None,
        cap: BlockState | BlockSpec | None = None,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.position = position
        self.height = height
        self.spec = spec
        self.base = base
        self.cap = cap

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        return cls(
            path,
            parse_vec(data, "position", path),
            parse_int(data, "height", path, minimum=1),
            BlockSpec.from_dict(data, path),
            parse_block(data, "base", path),
            parse_block(data, "cap", path),
            data.get("comment"),
        )

    def expand(self) -> list[dict[str, Any]]:
        top = self.position + Vec3(0, self.height - 1, 0)
        ops: list[dict[str, Any]] = [
            {
                "type": "fill",
                "from": self.position.to_list(),
                "to": top.to_list(),
                **spec_json(self.spec),
            }
        ]
        if self.base is not None:
            ops.append(
                {"type": "set", "position": self.position.to_list(), **spec_json(self.base)}
            )
        if self.cap is not None and self.height > 1:
            ops.append({"type": "set", "position": top.to_list(), **spec_json(self.cap)})
        return ops
