"""明示した矩形二棟の共有壁と内部開口を、一つの操作で所有して生成する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial, reduce
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext, Operation, parse_int
from mcblueprint.operations.building import BuildingFace, BuildingLayout, BuildingOperation
from mcblueprint.operations.composite import direction_vec, opposite, parse_block
from mcblueprint.operations.registry import build_operation, register
from mcblueprint.operations.roof import RoofOperation


@dataclass(frozen=True, slots=True)
class JoinLayout:
    """二棟それぞれの変換適用後、外側ネスト変換前の接続情報。

    shared_face は先頭棟の外向きで、床上から共通最上天井までの実共有面。
    floor_y は両棟の基礎床層、openings は空気として生成する内部開口の範囲。
    """

    buildings: tuple[BuildingLayout, BuildingLayout]
    shared_face: BuildingFace
    floor_y: int
    openings: tuple[AABB, ...]


def _intersects(a: AABB, b: AABB) -> bool:
    return all(a.min[i] <= b.max[i] and b.min[i] <= a.max[i] for i in range(3))


@register
class JoinBuildingsOperation(Operation):
    type = "join_buildings"

    def __init__(
        self, path: str, buildings: tuple[BuildingOperation, BuildingOperation],
        faces: tuple[str, str], wall: BlockState, openings: Sequence[Mapping[str, Any]],
        comment: str | None = None, *, join_roof: bool = False,
    ) -> None:
        super().__init__(path, comment)
        self.buildings = buildings
        self.join_roof = join_roof
        self.wall = BlockSpec(block=wall)
        a, b = buildings
        if opposite(faces[0]) != faces[1]:
            raise BlueprintError(f"{path}.faces: 反対向きの二面を指定してください")
        floor_y = a.layout.entrance.y - 1
        if floor_y != b.layout.entrance.y - 1 or a.floor_height != b.floor_height:
            raise BlueprintError(f"{path}: 二棟の床高・階高は一致する必要があります")
        if a.floors != b.floors:
            raise BlueprintError(
                f"{path}.buildings: 異階数の独立屋根は上側外壁・床に干渉するため未対応です"
            )
        face_a = next(face.bounds for face in a.layout.faces if face.direction == faces[0])
        face_b = next(face.bounds for face in b.layout.faces if face.direction == faces[1])
        normal_axis = "x" if faces[0] in ("east", "west") else "z"
        along_axis = "z" if normal_axis == "x" else "x"
        if face_a.min.axis(normal_axis) != face_b.min.axis(normal_axis):
            raise BlueprintError(f"{path}.faces: 二面が同じ座標平面で接していません")
        low, high = Vec3.max(face_a.min, face_b.min), Vec3.min(face_a.max, face_b.max)
        if high.axis(along_axis) - low.axis(along_axis) < 2:
            raise BlueprintError(f"{path}.faces: 共有面長は角を含めて3以上必要です")
        shared = AABB(low, high)
        self._owned_face = AABB(low.with_axis("y", floor_y), high)
        for index, building in enumerate(buildings):
            entrance = building.layout.entrance
            entry_box = AABB(entrance, entrance + Vec3(0, 1, 0))
            if _intersects(self._owned_face, entry_box):
                raise BlueprintError(f"{path}.buildings[{index}]: 共有面が主要入口に干渉します")
        if not openings:
            raise BlueprintError(f"{path}.openings: 内部開口を1つ以上指定してください")
        boxes = []
        for index, item in enumerate(openings):
            sub = f"{path}.openings[{index}]"
            floor = parse_int(item, "floor", sub, minimum=0, default=0)
            offset = parse_int(item, "offset", sub, minimum=1)
            width = parse_int(item, "width", sub, minimum=1, default=1)
            height = parse_int(item, "height", sub, minimum=2, default=2)
            if floor >= a.floors or height >= a.floor_height:
                raise BlueprintError(f"{sub}: 両棟に存在する階で、天井下に収まる高さが必要です")
            start = low.axis(along_axis) + offset
            end = start + width - 1
            if end >= high.axis(along_axis):
                raise BlueprintError(f"{sub}: 開口の両端には共有壁の角を1セル以上残してください")
            y = floor_y + floor * a.floor_height + 1
            box = AABB(
                low.with_axis(along_axis, start).with_axis("y", y),
                high.with_axis(along_axis, end).with_axis("y", y + height - 1),
            )
            for building, direction in zip(buildings, faces, strict=True):
                inward = -direction_vec(direction)
                passage = AABB.of(box.min + inward, box.max + inward)
                stairs = building.stairs_bounds
                if stairs is not None and _intersects(passage, stairs):
                    raise BlueprintError(f"{sub}: 開口の通路が階段・頭上・到着域に干渉します")
            if any(_intersects(box, other) for other in boxes):
                raise BlueprintError(f"{sub}: 内部開口が重複しています")
            boxes.append(box)
        self.layout = JoinLayout(
            (a.layout, b.layout), BuildingFace(faces[0], shared), floor_y, tuple(boxes),
        )
        self._roof_operations = self._joined_roof() if join_roof else []
        roof_operations = self._roof_operations if join_roof else b.expanded[-1].expanded
        roof_boxes = tuple(
            part.bounds() for operation in roof_operations
            for part in (
                operation.expanded if isinstance(operation, RoofOperation) else [operation]
            )
        )
        if not join_roof:
            roof_boxes = tuple(b.transform.bounds(box) for box in roof_boxes)
        self._roof_exclusions = (roof_boxes, roof_boxes if join_roof else ())

    def _joined_roof(self) -> list[Operation]:
        a, b = self.buildings
        ridge = a.layout.roof_ridge
        if ridge != b.layout.roof_ridge:
            raise BlueprintError(f"{self.path}.joinRoof: 直交する棟は未対応です")
        direction = self.layout.shared_face.direction
        normal_axis = "x" if direction in ("east", "west") else "z"
        if normal_axis != ridge:
            raise BlueprintError(f"{self.path}.joinRoof: 妻面同士の接触だけに対応します")
        if a.width != b.width:
            raise BlueprintError(f"{self.path}.joinRoof: 屋根幅は一致する必要があります")
        if a.materials["roof"] != b.materials["roof"]:
            raise BlueprintError(f"{self.path}.joinRoof: 屋根材は一致する必要があります")
        footprints = [reduce(AABB.union, (face.bounds for face in op.layout.faces))
                      for op in self.buildings]
        along = "z" if ridge == "x" else "x"
        if (footprints[0].min.axis(along) != footprints[1].min.axis(along) or
                footprints[0].max.axis(along) != footprints[1].max.axis(along)):
            raise BlueprintError(f"{self.path}.joinRoof: 位置の揃った全幅の妻面が必要です")
        footprint = footprints[0].union(footprints[1])
        y = footprint.max.y
        roof = RoofOperation(
            f"{self.path}<joined-roof>", footprint.min.with_axis("y", y), footprint.max,
            BlockSpec(block=a.materials["roof"]), style="gable", ridge=ridge,
        )
        owners = sorted(zip(footprints, self.buildings, strict=True),
                        key=lambda item: item[0].min.axis(ridge))
        materials = (owners[0][1].materials["wall"], owners[1][1].materials["wall"])
        gables = roof._gable_walls(
            footprint.min.x - 1, footprint.max.x + 1,
            footprint.min.z - 1, footprint.max.z + 1, y, materials,
        )
        return [roof, *(build_operation(raw, f"{self.path}<outer-gable>[{i}]")
                        for i, raw in enumerate(gables))]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        raw = data.get("buildings")
        faces = data.get("faces")
        if not isinstance(raw, list) or len(raw) != 2:
            raise BlueprintError(f"{path}.buildings: building をちょうど二棟指定してください")
        if not isinstance(faces, list) or len(faces) != 2 or any(
            face not in ("north", "east", "south", "west") for face in faces
        ):
            raise BlueprintError(f"{path}.faces: 接する二面の方角を指定してください")
        buildings = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping) or item.get("type") != "building":
                raise BlueprintError(f"{path}.buildings[{index}]: building が必要です")
            buildings.append(build_operation(item, f"{path}.buildings[{index}]"))
        wall = parse_block(data, "wall", path)
        if wall is None:
            raise BlueprintError(f"{path}.wall: 共有壁材を指定してください")
        join_roof = data.get("joinRoof", False)
        if not isinstance(join_roof, bool):
            raise BlueprintError(f"{path}.joinRoof: boolean が必要です")
        return cls(path, (buildings[0], buildings[1]), (faces[0], faces[1]), wall,
                   data.get("openings", []), data.get("comment"), join_roof=join_roof)

    def bounds(self) -> AABB:
        return self.buildings[0].bounds().union(self.buildings[1].bounds())

    def apply(self, ctx: ExecutionContext) -> None:
        for building, excluded in zip(self.buildings, self._roof_exclusions, strict=True):
            ctx.execute(building, apply=partial(
                building.apply_without_face, face=self._owned_face,
                include_roof=not self.join_roof,
                excluded=excluded,
            ))
        floor = BlockSpec(block=self.buildings[0].materials["floor"])
        air = BlockSpec(block=BlockState.parse("air"))
        for x in range(self._owned_face.min.x, self._owned_face.max.x + 1):
            for y in range(self._owned_face.min.y, self._owned_face.max.y + 1):
                for z in range(self._owned_face.min.z, self._owned_face.max.z + 1):
                    pos = Vec3(x, y, z)
                    if any(box.contains(pos) for box in self.layout.openings):
                        spec = air
                    else:
                        floor_row = (y - self.layout.floor_y) % self.buildings[0].floor_height == 0
                        spec = floor if floor_row else self.wall
                    ctx.place(pos, spec)
        for operation in self._roof_operations:
            ctx.execute(operation)
