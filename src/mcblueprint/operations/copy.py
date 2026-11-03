"""``copy``: duplicate the current contents of a box to another place."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Self

from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import ExecutionContext, Operation, parse_vec
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.registry import register
from mcblueprint.volume import EMPTY_ATTRIBUTES


@register
class CopyOperation(Operation):
    type = "copy"

    def __init__(
        self, path: str, start: Vec3, end: Vec3, offset: Vec3, comment: str | None = None
    ) -> None:
        super().__init__(path, comment)
        self.box = AABB.of(start, end)
        self.offset = offset

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        start, end = parse_vec(data, "from", path), parse_vec(data, "to", path)
        return cls(path, start, end, parse_vec(data, "offset", path), data.get("comment"))

    def bounds(self) -> AABB:
        shifted = AABB(self.box.min + self.offset, self.box.max + self.offset)
        return self.box.union(shifted)

    def apply(self, ctx: ExecutionContext) -> None:
        # snapshot first so an overlapping destination does not feed back into the source
        snapshot = []
        for local in iter_box(self.box):
            state = ctx.volume.get(ctx.transform.apply(local))
            if state is not None:
                snapshot.append((local, state, ctx.volume.attributes(ctx.transform.apply(local))))
        for local, state, attributes in snapshot:
            world = ctx.transform.apply(local + self.offset)
            incoming = ctx.attributes
            attributes = replace(
                attributes, role=incoming.role or attributes.role,
                element=incoming.element or attributes.element,
                face=(ctx.transform.apply_direction(incoming.face)
                      if incoming.face else attributes.face),
                reserved=incoming.reserved or attributes.reserved,
                owner=incoming.owner or attributes.owner,
                source=incoming.source or attributes.source,
                operation=incoming.operation or attributes.operation,
            )
            if attributes == EMPTY_ATTRIBUTES and ctx.on_conflict == "error":
                ctx.volume.set(world, state)
            else:
                ctx.volume.set(world, state, attributes, on_conflict=ctx.on_conflict)
