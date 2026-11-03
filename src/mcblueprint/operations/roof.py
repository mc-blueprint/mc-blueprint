"""``roof``: gable or hip roof made of stair blocks over a rectangular footprint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import BlockSpec, parse_choice, parse_int, parse_vec
from mcblueprint.operations.composite import (
    CompositeOperation,
    is_stairs,
    oriented,
    slab_for,
    spec_json,
)
from mcblueprint.operations.registry import register
from mcblueprint.operations.room import parse_spec

STYLES = ("gable", "hip")
RIDGES = ("x", "z")


@register
class RoofOperation(CompositeOperation):
    type = "roof"

    def __init__(
        self,
        path: str,
        start: Vec3,
        end: Vec3,
        spec: BlockSpec,
        style: str = "gable",
        ridge: str | None = None,
        overhang: int = 1,
        gable: BlockState | BlockSpec | None = None,
        ridge_block: BlockState | BlockSpec | None = None,
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        if start.y != end.y:
            raise BlueprintError(f"{path}: 'from' and 'to' must have the same y coordinate")
        self.footprint = AABB.of(start, end)
        self.spec = spec
        self.style = style
        self.overhang = overhang
        self.gable = gable
        size = self.footprint.size
        self.ridge = ridge or ("x" if size.x >= size.z else "z")
        if ridge_block is not None:
            self.ridge_block = ridge_block
        elif spec.block is not None:
            self.ridge_block = slab_for(spec.block)
        elif spec.role is not None:
            self.ridge_block = (
                BlockSpec(role=spec.role, shape="slab", state=(("type", "bottom"),))
                if spec.shape == "stairs" else spec
            )
        else:
            self.ridge_block = None  # palette roofs use the palette for the ridge

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        return cls(
            path,
            parse_vec(data, "from", path),
            parse_vec(data, "to", path),
            BlockSpec.from_dict(data, path),
            parse_choice(data, "style", path, STYLES, "gable"),
            parse_choice(data, "ridge", path, RIDGES, "x") if "ridge" in data else None,
            parse_int(data, "overhang", path, minimum=0, default=1),
            cls._material(data, "gable", path),
            cls._material(data, "ridgeBlock", path),
            data.get("comment"),
        )

    @staticmethod
    def _material(data: Mapping[str, Any], key: str, path: str) -> BlockState | BlockSpec | None:
        spec = parse_spec(data, key, path)
        return spec.block if spec and spec.block else spec

    def _slope(self, facing: str, shape: str | None = None) -> dict[str, Any]:
        if is_stairs(self.spec):
            extra = {"shape": shape} if shape else {}
            return oriented(self.spec, facing=facing, half="bottom", **extra)
        return spec_json(self.spec)

    def _corners(self, xa: int, xb: int, za: int, zb: int, level: int) -> list[dict[str, Any]]:
        """The four hip corners as outer-corner stairs, matching what Minecraft
        computes from the neighbours (a corner faces along z; the stair in front of
        it is the x edge, whose facing decides left / right)."""
        return [
            {"type": "set", "position": [xa, level, za], **self._slope("south", "outer_left")},
            {"type": "set", "position": [xb, level, za], **self._slope("south", "outer_right")},
            {"type": "set", "position": [xa, level, zb], **self._slope("north", "outer_right")},
            {"type": "set", "position": [xb, level, zb], **self._slope("north", "outer_left")},
        ]

    def _ridge(self) -> dict[str, Any]:
        if self.ridge_block is not None:
            if isinstance(self.ridge_block, BlockSpec):
                return spec_json(self.ridge_block)
            return {"block": self.ridge_block.to_string()}
        return spec_json(self.spec)

    def expand(self) -> list[dict[str, Any]]:
        fp = self.footprint
        o = self.overhang
        x0, x1 = fp.min.x - o, fp.max.x + o
        z0, z1 = fp.min.z - o, fp.max.z + o
        y = fp.min.y
        ops: list[dict[str, Any]] = []
        layer = 0
        while True:
            xa, xb = x0 + layer, x1 - layer
            za, zb = z0 + layer, z1 - layer
            shrink_x = self.style == "hip" or self.ridge == "z"
            shrink_z = self.style == "hip" or self.ridge == "x"
            if not shrink_x:
                xa, xb = x0, x1
            if not shrink_z:
                za, zb = z0, z1
            if xa > xb or za > zb:
                break
            level = y + layer
            width_x, width_z = xb - xa + 1, zb - za + 1
            if (shrink_z and width_z == 1) or (shrink_x and width_x == 1):
                ops.append(
                    {
                        "type": "fill",
                        "from": [xa, level, za],
                        "to": [xb, level, zb],
                        **self._ridge(),
                    }
                )
                break
            inner_za, inner_zb = (za + 1, zb - 1) if shrink_z else (za, zb)
            # a hip layer has stairs on all four sides, so its corners are outer corners
            corners = shrink_x and shrink_z and inner_za <= inner_zb and is_stairs(self.spec)
            if shrink_z:
                edge_xa, edge_xb = (xa + 1, xb - 1) if corners else (xa, xb)
                if edge_xa <= edge_xb:
                    ops.append(
                        {
                            "type": "fill",
                            "from": [edge_xa, level, za],
                            "to": [edge_xb, level, za],
                            **self._slope("south"),
                        }
                    )
                    ops.append(
                        {
                            "type": "fill",
                            "from": [edge_xa, level, zb],
                            "to": [edge_xb, level, zb],
                            **self._slope("north"),
                        }
                    )
                if corners:
                    ops.extend(self._corners(xa, xb, za, zb, level))
            if shrink_x:
                if inner_za <= inner_zb:
                    ops.append(
                        {
                            "type": "fill",
                            "from": [xa, level, inner_za],
                            "to": [xa, level, inner_zb],
                            **self._slope("east"),
                        }
                    )
                    ops.append(
                        {
                            "type": "fill",
                            "from": [xb, level, inner_za],
                            "to": [xb, level, inner_zb],
                            **self._slope("west"),
                        }
                    )
            if (shrink_z and width_z == 2) or (shrink_x and width_x == 2):
                break
            layer += 1
        if self.style == "gable" and self.gable is not None:
            ops.extend(self._gable_walls(x0, x1, z0, z1, y))
        return ops

    def _gable_walls(
        self, x0: int, x1: int, z0: int, z1: int, y: int,
        materials: tuple[BlockState | BlockSpec, BlockState | BlockSpec] | None = None,
    ) -> list[dict[str, Any]]:
        """棟軸の最小端・最大端を閉じる妻壁。materials省略時は両端にgable材を使う。"""
        fp = self.footprint
        if materials is None:
            assert self.gable is not None
            materials = (self.gable, self.gable)
        ops: list[dict[str, Any]] = []
        layer = 0
        while True:
            if self.ridge == "x":
                a, b = z0 + layer + 1, z1 - layer - 1
                if a > b:
                    break
                for x, material in zip((fp.min.x, fp.max.x), materials, strict=True):
                    ops.append(
                        {
                            "type": "fill",
                            "from": [x, y + layer, a],
                            "to": [x, y + layer, b],
                            **spec_json(material),
                        }
                    )
            else:
                a, b = x0 + layer + 1, x1 - layer - 1
                if a > b:
                    break
                for z, material in zip((fp.min.z, fp.max.z), materials, strict=True):
                    ops.append(
                        {
                            "type": "fill",
                            "from": [a, y + layer, z],
                            "to": [b, y + layer, z],
                            **spec_json(material),
                        }
                    )
            layer += 1
        return ops
