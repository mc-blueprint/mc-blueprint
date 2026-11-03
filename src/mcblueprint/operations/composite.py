"""Base for high-level building operations that expand into primitive operations."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from functools import reduce
from typing import Any

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext, Operation, parse_choice
from mcblueprint.operations.transform import DIRECTIONS

HORIZONTAL = ("north", "south", "east", "west")
RIGHT_OF = {"north": "east", "east": "south", "south": "west", "west": "north"}


def direction_vec(name: str) -> Vec3:
    return DIRECTIONS[name]


def right_of(name: str) -> str:
    """Direction 90 degrees clockwise (seen from above) of a horizontal direction."""
    return RIGHT_OF[name]


def opposite(name: str) -> str:
    return {"north": "south", "south": "north", "east": "west", "west": "east"}[name]


def parse_direction(
    data: Mapping[str, Any], key: str, path: str, default: str | None = None
) -> str:
    if key not in data:
        if default is None:
            raise BlueprintError(f"{path}: missing required '{key}'")
        return default
    return parse_choice(data, key, path, HORIZONTAL, default or "north")


def parse_block(data: Mapping[str, Any], key: str, path: str) -> BlockState | BlockSpec | None:
    """任意の単一ブロック。trim/fill/base/cap/columnはMaterialize前の役割参照も受ける。"""
    value = data.get(key)
    if value is None:
        return None
    if key in ("trim", "fill", "base", "cap", "column") and (
        isinstance(value, Mapping) and "role" in value
    ):
        return BlockSpec.from_dict({"block": value}, f"{path}.{key}")
    if not isinstance(value, str):
        raise BlueprintError(f"{path}.{key}: must be a block string")
    try:
        return BlockState.parse(value)
    except BlueprintError as exc:
        raise BlueprintError(f"{path}.{key}: {exc}") from None


def spec_json(spec: BlockSpec | BlockState) -> dict[str, Any]:
    """``block`` / ``palette`` entry for an expanded primitive operation."""
    if isinstance(spec, BlockState):
        return {"block": spec.to_string()}
    if spec.role is not None:
        return {"block": {"role": spec.role, "shape": spec.shape, "state": dict(spec.state)}}
    if spec.block is not None:
        return {"block": spec.block.to_string()}
    assert spec.palette is not None
    return {"palette": spec.palette}


def oriented(spec: BlockSpec, **defaults: str) -> dict[str, Any]:
    """Like :func:`spec_json` but fills ``defaults`` (e.g. ``facing``) into a
    stairs / slab block that does not set them itself; palettes are left as is."""
    if spec.role is not None:
        return {"block": {"role": spec.role, "shape": spec.shape,
                          "state": defaults | dict(spec.state)}}
    if spec.block is None:
        return spec_json(spec)
    state = spec.block
    for name, value in defaults.items():
        if state.get(name) is None:
            state = state.with_property(name, value)
    return {"block": state.to_string()}


def is_stairs(spec: BlockSpec) -> bool:
    return spec.shape == "stairs" if spec.role else (
        spec.block is not None and spec.block.id.endswith("_stairs")
    )


def slab_for(state: BlockState) -> BlockState:
    """``*_stairs`` -> ``*_slab[type=bottom]``; other blocks unchanged."""
    if state.id.endswith("_stairs"):
        return BlockState(state.id[: -len("_stairs")] + "_slab", (("type", "bottom"),))
    return state


class CompositeOperation(Operation):
    """Operation defined by the primitive operations it expands to.

    Subclasses implement :meth:`expand` returning JSON-like dicts of primitive
    operations (in local coordinates); they are built through the registry so
    they get the same parsing and validation as hand-written ones.
    """

    def __init__(self, path: str, comment: str | None = None) -> None:
        super().__init__(path, comment)
        self._expanded: list[Operation] | None = None

    @abstractmethod
    def expand(self) -> list[dict[str, Any]]:
        """Primitive operations (as dicts) in application order."""

    @property
    def expanded(self) -> list[Operation]:
        if self._expanded is None:
            from mcblueprint.operations.registry import build_operations

            self._expanded = build_operations(self.expand(), f"{self.path}<{self.type}>")
        return self._expanded

    def bounds(self) -> AABB:
        ops: Sequence[Operation] = self.expanded
        if not ops:
            raise BlueprintError(f"{self.path}: operation expands to nothing")
        return reduce(AABB.union, (op.bounds() for op in ops))

    def apply(self, ctx: ExecutionContext) -> None:
        for op in self.expanded:
            ctx.execute(op)
