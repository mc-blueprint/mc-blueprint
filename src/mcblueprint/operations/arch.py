"""``arch``: round, pointed or flat arch: a 1-thick ring of blocks around an opening.

The arch is described in a 2D grid ``(u, v)``: ``u`` runs along ``axis`` from the
left edge of the opening, ``v`` upwards from ``position.y``. The opening is a
rectangle (``width`` x ``height``) whose top ``rise + 1`` rows are narrowed to the
arch curve; the ring is every cell within ``thickness`` steps (4-neighbours) of the
opening, above the floor. Optional ``trim`` stairs replace the ring cells at its
steps: normal stairs on the outer convex corners, upside-down stairs on the inner
corners over the opening, so the curve reads as rounded (both only show with a
ring of thickness 2 or more; a 1-thick ring keeps the inner ones).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import BlockSpec, parse_choice, parse_int, parse_vec
from mcblueprint.operations.composite import CompositeOperation, parse_block, spec_json
from mcblueprint.operations.registry import register

STYLES = ("round", "pointed", "flat")
AXES_H = ("x", "z")

Cell = tuple[int, int]


def curve_rows(width: int, style: str) -> list[tuple[int, int]]:
    """``(u_min, u_max)`` of each row of the curved part, bottom (springing row,
    always the full width) first. ``len(rows) - 1`` is the rise of the arch."""
    if style == "flat":
        return [(0, width - 1)]
    rows: list[tuple[int, int]] = []
    k = 0
    while True:
        if style == "round":
            # disc of diameter ``width`` centred on the opening (WorldEdit-style test)
            cells = [
                u for u in range(width) if (2 * u - (width - 1)) ** 2 + (2 * k) ** 2 < width**2
            ]
        else:
            # two arcs of radius ``width`` centred just outside the springers
            limit = (2 * width + 1) ** 2
            cells = [
                u
                for u in range(width)
                if (2 * (u + 1)) ** 2 + (2 * k) ** 2 < limit
                and (2 * (width - u)) ** 2 + (2 * k) ** 2 < limit
            ]
        if not cells:
            return rows
        rows.append((cells[0], cells[-1]))
        k += 1


def arch_rise(width: int, style: str) -> int:
    return len(curve_rows(width, style)) - 1


def opening_cells(width: int, height: int, style: str) -> set[Cell]:
    rows = curve_rows(width, style)
    springing = height - len(rows)
    if springing < 0:
        raise BlueprintError(
            f"height must be at least {len(rows)} for a {style} arch of width {width}"
        )
    cells = {(u, v) for v in range(springing) for u in range(width)}
    for k, (a, b) in enumerate(rows):
        cells.update((u, springing + k) for u in range(a, b + 1))
    return cells


NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def ring_cells(opening: set[Cell], thickness: int = 1) -> set[Cell]:
    """Cells within ``thickness`` 4-neighbour steps of the opening (not the opening
    itself, nothing below the floor)."""
    ring: set[Cell] = set()
    frontier = set(opening)
    for _ in range(thickness):
        grown: set[Cell] = set()
        for u, v in frontier:
            for du, dv in NEIGHBOURS:
                cell = (u + du, v + dv)
                if cell not in opening and cell not in ring and cell[1] >= 0:
                    grown.add(cell)
        ring |= grown
        frontier = grown
    return ring


def ring_steps(opening: set[Cell], ring: set[Cell], width: int) -> list[tuple[Cell, str, int]]:
    """Ring cells that form a step of the curve, as ``(cell, kind, side)``.

    ``kind`` is ``"outer"`` for a convex corner on the outside of the ring (nothing
    above it and nothing further out, and it hangs over the opening or over a wider
    ring row rather than standing on a straight jamb) or ``"inner"`` for the end of a
    ring row over the opening (opening below, but not below the next cell outwards).
    ``side`` is ``-1`` left of the centre, ``+1`` right of it; cells on the centre
    line are never steps. A cell that qualifies for both is reported as ``"inner"``.

    The top of a curved arch stays plain blocks: no steps on the ring's top row and
    no inner steps in the ceiling directly above the opening's top row (the curve ends
    in a flat crown, not in a pair of stairs). An opening whose top row is still the
    full width (width 3 ``round``, ``flat``) has no curve, so its crown keeps the
    upside-down stairs at the ends."""
    solid = opening | ring
    centre = (width - 1) / 2
    apex = max(v for _, v in opening)
    top = max(v for _, v in ring)
    flat_crown = sum(1 for _, v in opening if v == apex) < width
    steps = []
    for u, v in sorted(ring, key=lambda c: (c[1], c[0])):
        side = -1 if u < centre else 1 if u > centre else 0
        if side == 0 or (flat_crown and v == top):
            continue
        outward = (u + side, v)
        if (u, v - 1) in opening and (u + side, v - 1) not in opening:
            if not (flat_crown and v == apex + 1):
                steps.append(((u, v), "inner", side))
        elif (
            (u, v + 1) not in solid
            and outward not in solid
            and ((u, v - 1) not in ring or (u + side, v - 1) in ring)
        ):
            steps.append(((u, v), "outer", side))
    return steps


def runs(cells: Iterable[Cell]) -> list[tuple[int, int, int]]:
    """``(v, u_min, u_max)`` of maximal horizontal runs, bottom row first."""
    ordered = sorted(cells, key=lambda c: (c[1], c[0]))
    result: list[tuple[int, int, int]] = []
    for u, v in ordered:
        if result and result[-1][0] == v and result[-1][2] == u - 1:
            result[-1] = (v, result[-1][1], u)
        else:
            result.append((v, u, u))
    return result


@register
class ArchOperation(CompositeOperation):
    type = "arch"

    def __init__(
        self,
        path: str,
        position: Vec3,
        axis: str,
        width: int,
        height: int,
        spec: BlockSpec,
        style: str = "round",
        depth: int = 1,
        trim: BlockState | BlockSpec | None = None,
        fill: BlockState | BlockSpec | None = None,
        hollow: bool = True,
        thickness: int = 1,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.position = position
        self.axis = axis
        self.width = width
        self.height = height
        self.spec = spec
        self.style = style
        self.depth = depth
        self.trim = trim
        self.hollow = hollow
        self.thickness = thickness
        self.fill = fill if fill is not None else (BlockState.of("air") if hollow else None)
        try:
            self.opening = opening_cells(width, height, style)
        except BlueprintError as exc:
            raise BlueprintError(f"{path}: {exc}") from None
        self.ring = ring_cells(self.opening, thickness)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        hollow = data.get("hollow", True)
        if not isinstance(hollow, bool):
            raise BlueprintError(f"{path}.hollow: must be a boolean")
        return cls(
            path,
            parse_vec(data, "position", path),
            parse_choice(data, "axis", path, AXES_H, "x"),
            parse_int(data, "width", path, minimum=1),
            parse_int(data, "height", path, minimum=1),
            BlockSpec.from_dict(data, path),
            parse_choice(data, "style", path, STYLES, "round"),
            parse_int(data, "depth", path, minimum=1, default=1),
            parse_block(data, "trim", path),
            parse_block(data, "fill", path),
            hollow,
            parse_int(data, "thickness", path, minimum=1, default=1),
            data.get("comment"),
        )

    # --- geometry -------------------------------------------------------------

    def _to_world(self, u: int, v: int, d: int) -> Vec3:
        local = Vec3(u, v, d) if self.axis == "x" else Vec3(d, v, u)
        return self.position + local

    def _fill_op(self, v: int, u_min: int, u_max: int, block: dict[str, str]) -> dict[str, Any]:
        a, b = self._to_world(u_min, v, 0), self._to_world(u_max, v, self.depth - 1)
        if a == b:
            return {"type": "set", "position": a.to_list(), **block}
        return {"type": "fill", "from": a.to_list(), "to": b.to_list(), **block}

    def _trim_state(self, kind: str, side: int) -> BlockState | dict[str, Any]:
        """Trim block for a step: stairs get ``facing`` (full side towards the centre
        for outer steps, away from it for inner ones) and ``half``; slabs get ``type``."""
        assert self.trim is not None
        state = self.trim
        towards_centre = -side  # +1 = +u
        direction = towards_centre if kind == "outer" else -towards_centre
        if isinstance(state, BlockSpec):
            from mcblueprint.operations.composite import oriented

            if state.shape == "stairs":
                facing = ("east" if direction > 0 else "west") if self.axis == "x" else (
                    "south" if direction > 0 else "north")
                return oriented(state, facing=facing, half="bottom" if kind == "outer" else "top")
            if state.shape == "slab":
                return oriented(state, type="bottom" if kind == "outer" else "top")
            return spec_json(state)
        if state.id.endswith("_stairs"):
            if self.axis == "x":
                facing = "east" if direction > 0 else "west"
            else:
                facing = "south" if direction > 0 else "north"
            if state.get("facing") is None:
                state = state.with_property("facing", facing)
            if state.get("half") is None:
                state = state.with_property("half", "bottom" if kind == "outer" else "top")
        elif state.id.endswith("_slab") and state.get("type") is None:
            state = state.with_property("type", "bottom" if kind == "outer" else "top")
        return state

    def expand(self) -> list[dict[str, Any]]:
        ops = [self._fill_op(v, a, b, spec_json(self.spec)) for v, a, b in runs(self.ring)]
        if self.fill is not None:
            block = spec_json(self.fill)
            ops.extend(self._fill_op(v, a, b, block) for v, a, b in runs(self.opening))
        if self.trim is not None:
            for (u, v), kind, side in ring_steps(self.opening, self.ring, self.width):
                state = self._trim_state(kind, side)
                block = state if isinstance(state, dict) else spec_json(state)
                ops.append(self._fill_op(v, u, u, block))
        return ops


@dataclass(frozen=True)
class ArchSpec:
    """``arch`` object of ``doorway`` / ``window`` (and the openings of ``room`` /
    ``tower``): ``{style, block | palette, trim, thickness}``."""

    style: str
    spec: BlockSpec
    trim: BlockState | BlockSpec | None = None
    thickness: int = 1

    def rise(self, width: int) -> int:
        return arch_rise(width, self.style)

    def operation(
        self,
        path: str,
        position: Vec3,
        axis: str,
        width: int,
        height: int,
        *,
        depth: int = 1,
        fill: BlockState | None = None,
    ) -> ArchOperation:
        return ArchOperation(
            path,
            position,
            axis,
            width,
            height,
            self.spec,
            self.style,
            depth=depth,
            trim=self.trim,
            fill=fill,
            thickness=self.thickness,
        )

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"style": self.style, **spec_json(self.spec)}
        if self.trim is not None:
            result["trim"] = spec_json(self.trim)["block"]
        if self.thickness != 1:
            result["thickness"] = self.thickness
        return result


def parse_arch_spec(data: Mapping[str, Any], path: str) -> ArchSpec | None:
    value = data.get("arch")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise BlueprintError(f"{path}.arch: must be an object with 'style' and 'block'")
    sub = f"{path}.arch"
    return ArchSpec(
        parse_choice(value, "style", sub, STYLES, "round"),
        BlockSpec.from_dict(value, sub),
        parse_block(value, "trim", sub),
        parse_int(value, "thickness", sub, minimum=1, default=1),
    )
