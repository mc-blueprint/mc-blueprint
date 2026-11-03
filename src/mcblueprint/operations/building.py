"""矩形一〜二階建てを、固定配置の room / roof / stairs に展開する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext, parse_int, parse_vec
from mcblueprint.operations.composite import CompositeOperation, is_stairs, parse_block, spec_json
from mcblueprint.operations.registry import build_operation, register
from mcblueprint.operations.room import parse_spec
from mcblueprint.operations.transform import Transform


@dataclass(frozen=True, slots=True)
class BuildingFace:
    """外向きの方角と、床上から最上階の天井までの壁面（開口を含む）。"""

    direction: str
    bounds: AABB


@dataclass(frozen=True, slots=True)
class BuildingLayout:
    """building 自身の position / angle を適用した配置座標での接続情報。

    entrance はドア下段、arrivals は各階に立ったときの足元セル。
    faces は北・東・南・西のローカル壁面順。外側のネスト操作の変換は含まない。
    roof_bounds は軒と棟を含む実際の屋根範囲、roof_ridge は棟の軸。
    """

    entrance: Vec3
    entry_direction: str
    arrivals: tuple[Vec3, ...]
    faces: tuple[BuildingFace, ...]
    roof_bounds: AABB
    roof_ridge: str


class _SharedFaceContext(ExecutionContext):
    def __init__(
        self, ctx: ExecutionContext, transform: Transform, face: AABB,
        excluded: Sequence[AABB] = (),
    ) -> None:
        super().__init__(ctx.volume, ctx.rng, ctx.palettes, transform, ctx.depth)
        self.seed = ctx.seed
        self.attributes = ctx.attributes
        self.operation_id = ctx.operation_id
        self.on_conflict = ctx.on_conflict
        self.sampler = ctx.sampler
        self.face = ctx.transform.bounds(face)
        self.excluded = tuple(ctx.transform.bounds(box) for box in excluded)

    def place(self, pos: Vec3, spec: BlockSpec) -> None:
        world = self.transform.apply(pos)
        if not self.face.contains(world) and not any(box.contains(world) for box in self.excluded):
            super().place(pos, spec)

    def fill(self, box: AABB, spec: BlockSpec) -> None:
        from mcblueprint.operations.box_like import iter_box

        clipped = self.clip_box(box)
        if clipped is not None:
            self.place_all(iter_box(clipped), spec)


@register
class BuildingOperation(CompositeOperation):
    type = "building"

    def __init__(
        self, path: str, position: Vec3, width: int, depth: int, floors: int,
        floor_height: int, angle: int, materials: Mapping[str, BlockState | BlockSpec],
        comment: str | None = None,
    ) -> None:
        super().__init__(path, comment)
        self.width = width
        self.depth = depth
        self.floors = floors
        self.floor_height = floor_height
        self.materials = dict(materials)
        self.transform = Transform.rotation(angle, Vec3(0, 0, 0)).then(
            Transform.translation(position)
        )
        if floors not in (1, 2):
            raise BlueprintError(f"{path}.floors: 1 または 2 が必要です")
        if angle not in (0, 90, 180, 270):
            raise BlueprintError(f"{path}.angle: 0 / 90 / 180 / 270 が必要です")
        if width < 5 or depth < 5 or floor_height < 4:
            raise BlueprintError(f"{path}: 幅・奥行きは5以上、階高は4以上が必要です")
        if floors == 2 and depth < floor_height + 4:
            raise BlueprintError(
                f"{path}.depth: 階段・上階開口・到着床には floorHeight + 4 以上が必要です"
            )
        for role in ("roof", "stairs"):
            if role not in self.materials:
                continue
            state = self.materials[role]
            if isinstance(state, BlockSpec):
                if not is_stairs(state) or any(
                    key in dict(state.state) for key in ("facing", "half", "shape")
                ):
                    raise BlueprintError(f"{path}.{role}: 状態未指定のstairs形状が必要です")
                continue
            if not state.id.endswith("_stairs"):
                raise BlueprintError(f"{path}.{role}: 階段ブロックが必要です")
            if any(state.get(key) is not None for key in ("facing", "half", "shape")):
                raise BlueprintError(f"{path}.{role}: facing / half / shape は自動設定します")
        if not self.materials["door"].id.endswith("_door"):
            raise BlueprintError(f"{path}.door: ドアブロックが必要です")
        self.layout = self._layout()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        floors = parse_int(data, "floors", path, minimum=1, default=1)
        materials = {}
        for role in ("wall", "floor", "roof", "stairs", "window", "door"):
            if role in ("wall", "floor", "roof", "stairs"):
                spec = parse_spec(data, role, path)
                state = spec.block if spec and spec.block else spec
            else:
                state = parse_block(data, role, path)
            if state is None:
                if role == "stairs" and floors == 1:
                    continue
                raise BlueprintError(f"{path}: 必須の材質 {role!r} がありません")
            materials[role] = state
        return cls(
            path, parse_vec(data, "position", path),
            parse_int(data, "width", path, minimum=5),
            parse_int(data, "depth", path, minimum=5),
            floors,
            parse_int(data, "floorHeight", path, minimum=4, default=4),
            parse_int(data, "angle", path, minimum=0, default=0), materials, data.get("comment"),
        )

    @property
    def _door_x(self) -> int:
        return (self.width - 1) // 2

    def _roof(self) -> dict[str, Any]:
        y = self.floors * self.floor_height
        return {
            "type": "roof", "from": [0, y, 0], "to": [self.width - 1, y, self.depth - 1],
            "style": "gable", "ridge": "z", "overhang": 1,
            **spec_json(self.materials["roof"]),
            "gable": self._material("wall"),
        }

    def _material(self, key: str) -> Any:
        value = spec_json(self.materials[key])
        return value.get("block", value)

    def _layout(self) -> BuildingLayout:
        w, d, top = self.width - 1, self.depth - 1, self.floors * self.floor_height
        faces = (
            ("north", AABB(Vec3(0, 1, 0), Vec3(w, top, 0))),
            ("east", AABB(Vec3(w, 1, 0), Vec3(w, top, d))),
            ("south", AABB(Vec3(0, 1, d), Vec3(w, top, d))),
            ("west", AABB(Vec3(0, 1, 0), Vec3(0, top, d))),
        )
        arrivals = [Vec3(self._door_x, 1, 1)]
        if self.floors == 2:
            arrivals.append(Vec3(1, self.floor_height + 1, self.floor_height + 2))
        roof_bounds = build_operation(self._roof(), f"{self.path}<roof>").bounds()
        return BuildingLayout(
            self.transform.apply(Vec3(self._door_x, 1, 0)),
            self.transform.apply_direction("south"),
            tuple(self.transform.apply(point) for point in arrivals),
            tuple(BuildingFace(self.transform.apply_direction(side), self.transform.bounds(box))
                  for side, box in faces),
            self.transform.bounds(roof_bounds), self.transform.apply_axis("z"),
        )

    def expand(self) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for level in range(self.floors):
            y = level * self.floor_height
            windows = [
                {"side": "east", "sill": 2, "height": 2,
                 "block": self.materials["window"].to_string()},
                {"side": "south", "sill": 2, "height": 2,
                 "block": self.materials["window"].to_string()},
            ]
            if level > 0:
                windows.append({"side": "north", "sill": 2, "height": 2,
                                "block": self.materials["window"].to_string()})
            ops.append({
                "type": "room", "from": [0, y, 0],
                "to": [self.width - 1, y + self.floor_height, self.depth - 1],
                "wall": self._material("wall"),
                "floor": self._material("floor"),
                "ceiling": self._material("floor"),
                "windows": windows,
                "doors": [{"side": "north", "offset": self._door_x,
                           "door": self.materials["door"].to_string()}] if level == 0 else [],
            })
        if self.floors == 2:
            ops.append({
                "type": "stairs", "start": [1, 1, 2], "direction": "south",
                "height": self.floor_height, "headroom": 3,
                **spec_json(self.materials["stairs"]),
                "base": self._material("floor"),
            })
        ops.append(self._roof())
        return ops

    @property
    def stairs_bounds(self) -> AABB | None:
        """固定階段の土台・頭上・到着を含む接続予約域。配置座標で、外側変換は含まない。"""
        if self.floors == 1:
            return None
        return self.transform.bounds(AABB(
            Vec3(1, 1, 2), Vec3(1, self.floor_height + 3, self.floor_height + 2),
        ))

    def apply_without_face(
        self, ctx: ExecutionContext, face: AABB, *, include_roof: bool = True,
        excluded: Sequence[AABB] = (),
    ) -> None:
        """join_buildings が所有する共有面への配置を行わず、他の部分だけを生成する。

        face は building 自身の position / angle 適用後、外側変換前の配置座標。
        床・窓・屋根も含め、この範囲の全セルの生成責務を接続操作へ移譲する。
        include_roof=False は屋根全体の生成も接続操作へ移譲する。
        excluded は同じ配置座標で、接合内部の後段屋根へ最終配置を移譲する矩形。
        """
        child = _SharedFaceContext(ctx, self.transform.then(ctx.transform), face, excluded)
        operations = self.expanded if include_roof else self.expanded[:-1]
        for operation in operations:
            child.execute(operation)

    def bounds(self) -> AABB:
        return self.transform.bounds(super().bounds())

    def apply(self, ctx: ExecutionContext) -> None:
        child = ctx.child(self.transform, nesting=False)
        super().apply(child)
