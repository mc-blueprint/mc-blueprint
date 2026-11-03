"""``replace``: swap matching blocks inside a box for another block / palette."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext, Operation, parse_vec
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.registry import register

AIR = BlockState.of("air")


def parse_match(data: Mapping[str, Any], path: str) -> tuple[BlockState, ...]:
    raw = data.get("match")
    items: Sequence[Any]
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, list) and raw and all(isinstance(x, str) for x in raw):
        items = raw
    else:
        raise BlueprintError(f"{path}.match: must be a block string or a non-empty array of them")
    try:
        return tuple(BlockState.parse(item) for item in items)
    except BlueprintError as exc:
        raise BlueprintError(f"{path}.match: {exc}") from None


def matches(state: BlockState, pattern: BlockState) -> bool:
    """``pattern`` without properties matches by id; listed properties must be equal."""
    if state.id != pattern.id:
        return False
    return all(state.get(name) == value for name, value in pattern.properties)


@register
class ReplaceOperation(Operation):
    type = "replace"

    def __init__(
        self,
        path: str,
        start: Vec3,
        end: Vec3,
        patterns: tuple[BlockState, ...],
        spec: BlockSpec,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.box = AABB.of(start, end)
        self.patterns = patterns
        self.spec = spec

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        start, end = parse_vec(data, "from", path), parse_vec(data, "to", path)
        return cls(
            path,
            start,
            end,
            parse_match(data, path),
            BlockSpec.from_dict(data, path),
            data.get("comment"),
        )

    def bounds(self) -> AABB:
        return self.box

    def apply(self, ctx: ExecutionContext) -> None:
        # unset cells inside the box count as air so "replace air" fills holes
        box = ctx.clip_box(self.box)
        if box is None:
            return
        for local in iter_box(box):
            world = ctx.transform.apply(local)
            current = ctx.volume.get(world) or AIR
            if any(matches(current, pattern) for pattern in self.patterns):
                ctx.place(local, self.spec)
