"""異なる棟の接触帯と屋根包絡を、一意な所有者で施工する。"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import reduce
from itertools import pairwise
from typing import Any, Self

from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.operations.base import BlockSpec, ExecutionContext, Operation
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.building import BuildingOperation
from mcblueprint.operations.composite import HORIZONTAL, direction_vec, spec_json
from mcblueprint.operations.registry import build_operation, register
from mcblueprint.operations.tower import TowerOperation
from mcblueprint.operations.transform import Transform
from mcblueprint.volume import BlockVolume


@dataclass(frozen=True, slots=True)
class JunctionPart:
    element: str
    shape: str
    operation: Operation
    footprint: set[tuple[int, int]]
    contour: set[tuple[int, int]]
    floors: tuple[int, ...]
    top: int
    roofs: list[Operation]
    body: list[Operation]
    transform: Transform
    stairs: tuple[AABB, ...]


class _FilteredContext(ExecutionContext):
    def __init__(self, ctx, inverse, accept):
        super().__init__(ctx.volume, ctx.rng, ctx.palettes, ctx.transform, ctx.depth,
                         seed=ctx.seed, attributes=ctx.attributes, operation_id=ctx.operation_id,
                         on_conflict=ctx.on_conflict, sampler=ctx.sampler)
        self.inverse = inverse
        self.accept = accept

    def place(self, pos, spec):
        if self.accept(self.inverse.apply(self.transform.apply(pos))):
            super().place(pos, spec)

    def fill(self, box, spec):
        clipped = self.clip_box(box)
        if clipped is not None:
            self.place_all(iter_box(clipped), spec)

    def child(self, *args, **kwargs):
        return _FilteredContext(super().child(*args, **kwargs), self.inverse, self.accept)


def _inverse(transform):
    rows = transform.rows
    inverse = Transform(tuple(tuple(rows[j][i] for j in range(3)) for i in range(3)))
    return Transform(inverse.rows, -inverse.apply(transform.offset))


def _frame_cells(operation, transform, structural=False):
    structural |= operation.attributes.get("reserved") == "structure"
    children = getattr(operation, "operations", None)
    if children is not None:
        transform = getattr(operation, "transform", Transform.identity()).then(transform)
    else:
        children = getattr(operation, "expanded", None)
    if children is not None:
        for child in children:
            yield from _frame_cells(child, transform, structural)
    elif structural and hasattr(operation, "cells"):
        yield from map(transform.apply, operation.cells())


def _part(raw, shape, profile, path, stair_block):
    from mcblueprint.facade_projection import plan_facade_runs

    op = build_operation(raw, path)
    element = raw["element"]
    if isinstance(op, BuildingOperation):
        params = {"width": op.width, "depth": op.depth}
        transform = op.transform
        floors = tuple(op.layout.entrance.y - 1 + i * op.floor_height for i in range(op.floors))
        top = floors[0] + op.floors * op.floor_height
        body, roofs = op.expanded[:-1], op.expanded[-1:]
        stairs = (op.stairs_bounds,) if op.stairs_bounds else ()
        actual_shape = "rectangle"
    elif isinstance(op, TowerOperation):
        params = {"radius": op.radius, "shape": op.shape}
        transform = Transform.translation(op.position)
        floors, top = tuple(op.storey_floors), op.platform_y
        body, roofs = [], []
        stairs = []
        # tower の展開は既に position を含む。
        for child_raw in op.expand():
            if child_raw["type"] == "spiral_stairs":
                continue
            child = build_operation(child_raw, path + ".tower")
            local = build_operation({"type": "translate", "offset": (-op.position).to_list(),
                                     "operations": [child_raw]}, path + ".local")
            (roofs if child.bounds().min.y >= top else body).append(local)
        if op.stairs is not None:
            levels = [*floors, top]
            for i, (lo, hi) in enumerate(pairwise(levels)):
                height = hi - lo
                if height < 4 or height > 2 * op.radius - 4:
                    raise BlueprintError(f"{path}: 接合塔の階段には階高4以上と十分な半径が必要です")
                start = Vec3(op.position.x + (-1 if i % 2 == 0 else 1),
                             lo + 1, op.position.z - op.radius + 2)
                stair = {"type": "stairs", "start": start.to_list(), "height": height,
                         "direction": "south", "headroom": 3, "block": stair_block,
                         "base": spec_json(op.floor), "reserved": "circulation",
                         "owner": element + ".stairs", "element": element + ".stairs"}
                landing = start + Vec3(0, height - 1, height)
                raw_landing = {"type": "set", "position": landing.to_list(),
                               **spec_json(op.floor), "reserved": "circulation",
                               "owner": element + ".stairs", "element": element + ".stairs"}
                stairs.append(build_operation(stair, path + ".stairs").bounds())
                body.append(build_operation({"type": "translate",
                                             "offset": (-op.position).to_list(),
                                             "operations": [stair, raw_landing]}, path + ".local"))
        stairs = tuple(stairs)
        actual_shape = op.shape
    else:
        if profile is None or raw["type"] != "translate":
            raise BlueprintError(f"{path}: building/tower、または計画付きtranslateが必要です")
        params = profile["params"]
        if (raw["offset"] != profile["offset"]
                or raw.get("rotation", 0) != params.get("angle", 0)):
            raise BlueprintError(f"{path}: profilesと施工の変換が一致しません")
        actual_shape = params.get("shape", "rectangle")
        transform = Transform.rotation(params.get("angle", 0), Vec3(0, 0, 0)).then(
            Transform.translation(Vec3.from_seq(profile["offset"])))
        floors = tuple(transform.offset.y + i * params["floor_height"]
                       for i in range(params["floors"]))
        top = transform.offset.y + params["floors"] * params["floor_height"]
        body, roofs, stairs = [], [], []
        for index, child_raw in enumerate(raw["operations"]):
            child = build_operation(child_raw, f"{path}.operations[{index}]")
            roof = (child_raw.get("role") == "roof" or child.type == "roof"
                    or child.bounds().min.y >= top - transform.offset.y)
            if child_raw.get("reserved") == "circulation":
                roof = False
            (roofs if roof else body).append(child)
            if child_raw.get("reserved") == "circulation":
                stairs.append(transform.bounds(child.bounds()))
        stairs = tuple(stairs)
    if shape != actual_shape:
        raise BlueprintError(f"{path}: parts.shape と実際の形状が一致しません")
    contour = set()
    for face in HORIZONTAL:
        axis = 0 if face in ("north", "south") else 2
        for start, end, fixed in plan_facade_runs(params, face):
            for u in range(start, end + 1):
                p = transform.apply(Vec3(u, 0, fixed) if axis == 0 else Vec3(fixed, 0, u))
                contour.add((p.x, p.z))
    if shape == "rectangle":
        points = (Vec3(x, 0, z) for x in range(params["width"])
                  for z in range(params["depth"]))
    elif shape == "square":
        r = params["radius"]
        points = (Vec3(x, 0, z) for x in range(-r, r + 1) for z in range(-r, r + 1))
    else:
        from mcblueprint.operations.shapes import disc_offsets

        points = disc_offsets(params["radius"], "solid", "y")
    footprint = {(p.x, p.z) for p in map(transform.apply, points)}
    return JunctionPart(element, shape, op, footprint, contour, floors, top,
                        roofs, body, transform, stairs)


@register
class BuildingJunctionOperation(Operation):
    type = "building_junction"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str) -> Self:
        op = cls(path, data.get("comment"))
        op.element = data["element"]
        if data["owner"] != op.element:
            raise BlueprintError(f"{path}.owner: 接合IDと一致させてください")
        profiles = {p["element"]: p for p in data.get("profiles", [])}
        shapes = {p["element"]: p["shape"] for p in data["parts"]}
        raw_parts = data["buildings"]
        if (len(shapes) != len(data["parts"]) or len(raw_parts) != len(shapes)
                or {p["element"] for p in raw_parts} != set(shapes)):
            raise BlueprintError(f"{path}.parts: 実在する各棟を一度ずつ参照してください")
        op.parts = [_part(raw, shapes[raw["element"]], profiles.get(raw["element"]),
                          f"{path}.buildings[{i}]", data["stairs"])
                    for i, raw in enumerate(raw_parts)]
        op.wall = BlockSpec.from_dict({"block": data["wall"]}, path + ".wall")
        op.frame = BlockSpec.from_dict({"block": data["frame"]}, path + ".frame")
        op.floor = BlockSpec.from_dict({"block": data["floor"]}, path + ".floor")
        op.shared = {}
        pairs = {}
        for i, a in enumerate(op.parts):
            for b in op.parts[i + 1:]:
                common = a.footprint & b.footprint
                if common - (a.contour & b.contour):
                    raise BlueprintError(f"{path}: 棟の内部が重複しています")
                cells = {Vec3(x, y, z) for x, z in common
                         for y in range(max(a.floors[0], b.floors[0]), min(a.top, b.top) + 1)}
                if cells:
                    pairs[frozenset((a.element, b.element))] = cells
                    for p in cells:
                        op.shared.setdefault(p, []).extend((a, b))
        covered = set()
        for contact in data["contacts"]:
            pair = frozenset((contact["first"], contact["second"]))
            box = AABB.of(Vec3.from_seq(contact["from"]), Vec3.from_seq(contact["to"]))
            cells = set(iter_box(box))
            if pair not in pairs or not cells <= pairs[pair]:
                raise BlueprintError(f"{path}.contacts: 実際の共有輪郭だけを指定してください")
            covered.update(cells)
        required = {point for point, parts in op.shared.items()
                    if point.y > max(part.floors[0] for part in parts)}
        if not required <= covered or not pairs:
            raise BlueprintError(f"{path}.contacts: 全接触面を過不足なく指定してください")
        op.jambs = {point for point in op.shared if sum(
            point + Vec3(dx, 0, dz) in op.shared for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1))
        ) <= 1}
        op.removed_frame, op.supports, op.structural_cells = set(), set(), set()
        for part in op.parts:
            for child in part.body:
                for point in _frame_cells(child, part.transform):
                    op.structural_cells.add(point)
                    if (point.x, point.z) in part.footprint:
                        continue
                    neighbors = [other for other in op.parts if other is not part
                                 and (point.x, point.z) in other.footprint
                                 and other.floors[0] <= point.y <= other.top]
                    if neighbors:
                        op.removed_frame.add(point)
                        if any(part.top > other.top for other in neighbors):
                            op.supports.add(point)
        op.passages, op.steps = {}, []
        nodes = {n["id"]: n for n in data["nodes"]}
        if len(nodes) != len(data["nodes"]):
            raise BlueprintError(f"{path}.nodes: 節点IDが重複しています")
        linked = set()
        for passage in data["passages"]:
            try:
                a, b = (nodes[passage[k]] for k in ("from", "to"))
                first, second = (next(p for p in op.parts if p.element == n["element"])
                                 for n in (a, b))
            except (KeyError, StopIteration):
                raise BlueprintError(f"{path}.passages: 実在する接続節点が必要です") from None
            pair = frozenset((first.element, second.element))
            if pair not in pairs:
                raise BlueprintError(f"{path}.passages: 接触していない棟です")
            op._passage(a, b, first, second, data["stairs"])
            linked.add(pair)
        if linked != set(pairs):
            raise BlueprintError(f"{path}.passages: 各接触面に通路が必要です")
        connected = {op.parts[0].element}
        while True:
            reached = connected | {root for pair in pairs if pair & connected for root in pair}
            if reached == connected:
                break
            connected = reached
        if connected != set(shapes):
            raise BlueprintError(f"{path}.parts: 全棟を接続してください")
        if op.supports.intersection(op.passages):
            raise BlueprintError(f"{path}.passages: 高棟の支持柱に干渉します")
        op.roof_policy = data["roofPolicy"]
        op.legacy = None
        op.joined_roofs = []
        if op.roof_policy == "join":
            if all(isinstance(p.operation, BuildingOperation) for p in op.parts):
                op.legacy = op._legacy(data)
            else:
                if len(op.parts) != 2 or any(p.shape != "rectangle" for p in op.parts):
                    raise BlueprintError(f"{path}.roofPolicy: joinは矩形二棟に限定されます")
                if "roof" not in data:
                    raise BlueprintError(f"{path}.roof: 計画済みjoinの屋根材が必要です")
                synthetic = []
                for part in op.parts:
                    profile = profiles[part.element]
                    params = profile["params"]
                    if params.get("roof", "gable") != "gable":
                        raise BlueprintError(f"{path}.roofPolicy: joinには切妻屋根が必要です")
                    synthetic.append({"type": "building", "element": part.element,
                                      "position": profile["offset"],
                                      "width": params["width"], "depth": params["depth"],
                                      "floors": params["floors"],
                                      "floorHeight": params["floor_height"],
                                      "angle": params.get("angle", 0),
                                      "wall": data["wall"], "floor": data["floor"],
                                      "roof": data["roof"], "stairs": data["stairs"],
                                      "door": "oak_door", "window": "glass"})
                proxy = cls(path)
                proxy.shared = op.shared
                proxy.parts = [replace(part, operation=build_operation(raw, path + ".join-part"))
                               for part, raw in zip(op.parts, synthetic, strict=True)]
                legacy = proxy._legacy({**data, "buildings": synthetic})
                op.joined_roofs = legacy._roof_operations
        op.operations = [part.operation for part in op.parts]
        op.generation_memory_bytes = (
            len(op.shared) + len(op.passages) + len(op.structural_cells)
        ) * 512 + sum(
            sum(roof.bounds().volume for roof in part.roofs) * 512 for part in op.parts)
        return op

    def _legacy(self, data):
        if len(self.parts) != 2 or any(not isinstance(p.operation, BuildingOperation)
                                       for p in self.parts):
            raise BlueprintError(f"{self.path}.roofPolicy: joinは矩形二棟に限定されます")
        a, b = self.parts
        shared = AABB.from_points(self.shared)
        faces = []
        for part in (a, b):
            face = next((f.direction for f in part.operation.layout.faces
                         if f.bounds.contains(shared.min.with_axis("y", f.bounds.min.y))
                         and f.bounds.contains(shared.max.with_axis("y", f.bounds.max.y))), None)
            faces.append(face)
        along = "z" if faces[0] in ("east", "west") else "x"
        openings = []
        for passage in data["passages"]:
            node = next(n for n in data["nodes"] if n["id"] == passage["from"])
            point = Vec3.from_seq(node["position"])
            openings.append({"floor": (point.y - a.floors[0] - 1) // a.operation.floor_height,
                             "offset": point.axis(along) - shared.min.axis(along)})
        return build_operation({"type": "join_buildings", "buildings": data["buildings"],
                                "faces": faces, "wall": data["wall"], "openings": openings,
                                "joinRoof": True}, self.path + ".join")

    def _passage(self, a, b, first, second, stairs):
        start, end = (Vec3.from_seq(n["position"]) for n in (a, b))
        if (start.y - 1 not in first.floors or end.y - 1 not in second.floors
                or (start.x, start.z) not in first.footprint - first.contour
                or (end.x, end.z) not in second.footprint - second.contour
                or (start.x != end.x and start.z != end.z)):
            raise BlueprintError(f"{self.path}.passages: 両棟の床上を直線で結んでください")
        if start.y > end.y:
            start, end = end, start
        delta = end - start
        length = abs(delta.x) + abs(delta.z)
        height = delta.y
        if length < height + 2:
            raise BlueprintError(f"{self.path}.passages: 段差階段と到着床の長さが不足しています")
        forward = Vec3((delta.x > 0) - (delta.x < 0), 0,
                       (delta.z > 0) - (delta.z < 0))
        crossed = False
        for i in range(length + 1):
            foot = start + forward * i + Vec3(0, min(max(0, i - 1), height), 0)
            top = foot.y + (2 if 1 <= i <= height else 1)
            for y in range(foot.y - 1, top + 1):
                point = foot.with_axis("y", y)
                inside = [p for p in (first, second)
                          if (point.x, point.z) in p.footprint and p.floors[0] <= y <= p.top]
                if not inside or any(y >= p.top for p in inside):
                    raise BlueprintError(
                        f"{self.path}.passages: 階段の頭上または床の支持が不足しています")
                if any(box.contains(point) for p in (first, second) for box in p.stairs):
                    raise BlueprintError(f"{self.path}.passages: 既存階段・到着予約に干渉します")
                if point in self.structural_cells and (point not in self.shared or y >= foot.y):
                    raise BlueprintError(f"{self.path}.passages: 既存の構造柱・梁に干渉します")
                crossed |= point in self.shared
                state = self.floor if y < foot.y else BlockSpec(block=BlockState.of("air"))
                if point in self.passages and self.passages[point] != state:
                    raise BlueprintError(f"{self.path}.passages: 通路が矛盾しています")
                self.passages[point] = state
        if not crossed:
            raise BlueprintError(f"{self.path}.passages: 共有面を通過していません")
        if height:
            direction = next(name for name in HORIZONTAL if direction_vec(name) == forward)
            self.steps.append(build_operation({"type": "stairs",
                                                "start": (start + forward).to_list(),
                                                "direction": direction, "height": height,
                                                "headroom": 2, "block": stairs,
                                                "base": self.floor.block.to_string()},
                                               self.path + ".stairs"))

    def bounds(self) -> AABB:
        boxes = [part.operation.bounds() for part in self.parts]
        boxes.extend(part.transform.bounds(child.bounds()) for part in self.parts
                     for child in (*part.body, *part.roofs))
        return reduce(AABB.union, boxes)

    def internal_face(self, point: Vec3, face: str, building: str | None) -> bool:
        """外向きrayが相手棟へ入る共有面だけを内部面とする。外周の角は残す。"""
        if point not in self.shared:
            return False
        neighbor = point + direction_vec(face)
        return any(part.element != building and (neighbor.x, neighbor.z) in part.footprint
                   and part.floors[0] <= point.y <= part.top for part in self.parts)

    def roof_cells(self, ctx: ExecutionContext) -> dict[Vec3, BlockState]:
        """接合座標の屋根包絡。被覆と同じセル集合をCriticの欠損検査にも渡す。"""
        if self.joined_roofs:
            volume = BlockVolume()
            child = ExecutionContext(volume, random.Random(0), ctx.palettes)
            for roof in self.joined_roofs:
                child.execute(roof)
            cells = {point: state for point, state in volume
                     if point not in self.shared and point not in self.passages}
            self.roof_owners = {point: next(
                (part.element for part in self.parts if (point.x, point.z) in part.footprint),
                self.parts[0].element) for point in cells}
            self.roof_roles = {point: "roof" for point in cells}
            return cells
        roofs, roles, heights = [], [], []
        for part in self.parts:
            volume = BlockVolume()
            roof_ctx = ExecutionContext(volume, random.Random(0), ctx.palettes, part.transform,
                                        seed=ctx.seed,
                                        attributes=replace(ctx.attributes, role="roof"),
                                        operation_id=part.element + "/roof")
            for operation in part.roofs:
                roof_ctx.execute(operation)
            roofs.append(dict(volume))
            roles.append({point: volume.attributes(point).role for point, _ in volume})
            surface = {}
            for point, state in volume:
                if state.id != "minecraft:air":
                    key = point.x, point.z
                    surface[key] = max(surface.get(key, point.y), point.y)
            heights.append(surface)
        envelope = {}
        for index, cells in enumerate(roofs):
            for point, state in cells.items():
                if state.id == "minecraft:air":
                    continue
                if any((point.x, point.z) in part.footprint and point.y <= part.top
                       for j, part in enumerate(self.parts) if j != index):
                    continue
                key = point.x, point.z
                candidate = (point.y, -index)
                if candidate > envelope.get(key, (-10**30, 0)):
                    envelope[key] = candidate
        result = {}
        seals = {}
        self.roof_owners = {}
        self.roof_roles = {}
        for index, cells in enumerate(roofs):
            for point, state in cells.items():
                key = point.x, point.z
                winner = key in envelope and envelope[key][1] == -index
                gable = (key in self.parts[index].contour
                         and point.y < heights[index].get(key, point.y))
                if gable and key in envelope and not winner:
                    low = heights[index][key]
                    if low < envelope[key][0]:
                        seals[key] = min(seals.get(key, low), low)
                if winner or gable:
                    if any((point.x, point.z) in part.footprint and point.y <= part.top
                           for j, part in enumerate(self.parts) if j != index):
                        continue
                    if point not in self.shared and point not in self.passages:
                        if any(box.contains(point) for part in self.parts for box in part.stairs):
                            continue
                        result[point] = state
                        self.roof_owners[point] = self.parts[index].element
                        self.roof_roles[point] = roles[index][point]
        for (x, z), (height, owner) in envelope.items():
            for dx, dz in ((1, 0), (0, 1)):
                neighbor = envelope.get((x + dx, z + dz))
                if neighbor is None or neighbor[1] == owner or abs(neighbor[0] - height) <= 1:
                    continue
                high_x, high_z = x, z
                low_y = neighbor[0]
                if height < low_y:
                    high_x, high_z = x + dx, z + dz
                    low_y = height
                key = high_x, high_z
                low = low_y + 1
                seals[key] = min(seals.get(key, low), low)
        for (x, z), low in seals.items():
            high, owner = envelope[x, z]
            for y in range(low, high):
                point = Vec3(x, y, z)
                if point in result or point in self.structural_cells:
                    continue
                if any((x, z) in part.footprint and part.floors[0] <= y <= part.top
                       for part in self.parts):
                    continue
                if point in self.passages or any(box.contains(point) for part in self.parts
                                                for box in part.stairs):
                    raise BlueprintError(
                        f"{self.path}: 屋根谷の閉鎖が通路・階段予約に干渉します")
                result[point] = self.wall.block
                self.roof_owners[point] = self.parts[-owner].element
                self.roof_roles[point] = "wall_main"
        return result

    def apply(self, ctx: ExecutionContext) -> None:
        if self.legacy is not None:
            ctx.execute(self.legacy)
            for point in self.shared:
                if ctx.clip_box(AABB(point, point)) is None:
                    continue
                world = ctx.transform.apply(point)
                state = ctx.volume.get(world)
                attrs = replace(ctx.attributes, owner=self.element, element=self.element,
                                reserved="circulation" if point in self.passages else "structure")
                ctx.volume.set(world, state, attrs)
            return
        if ctx.seed is None and ctx.generation_seed is not None:
            ctx = ExecutionContext(ctx.volume, ctx.rng, ctx.palettes, ctx.transform, ctx.depth,
                                   seed=ctx.generation_seed, attributes=ctx.attributes,
                                   operation_id=ctx.operation_id, on_conflict=ctx.on_conflict,
                                   sampler=ctx.sampler)
        inverse = _inverse(ctx.transform)
        roofs = self.roof_cells(ctx)
        for part in self.parts:
            def accept(point, part=part):
                if (point in self.shared or point in self.passages or point in roofs
                        or point in self.supports):
                    return False
                return not any(other is not part and (point.x, point.z) in other.footprint
                               and other.floors[0] <= point.y <= other.top
                               for other in self.parts)

            child = _FilteredContext(ctx.child(part.transform, nesting=False), inverse, accept)
            child.attributes = replace(child.attributes, **{
                "element": part.element, "owner": part.element, "reserved": None,
                **part.operation.attributes,
            })
            child.operation_id = (f"/{part.operation.id or part.element}"
                                  f"@:{child.transform.rows}:{child.transform.offset}")
            for operation in part.body:
                child.execute(operation)
        owned = replace(ctx.attributes, element=self.element, owner=self.element,
                        reserved="structure", role="frame", face=None)
        child = ExecutionContext(ctx.volume, ctx.rng, ctx.palettes, ctx.transform, ctx.depth,
                                 seed=ctx.seed, attributes=owned, operation_id=ctx.operation_id,
                                 sampler=ctx.sampler)
        for point, state in roofs.items():
            child.attributes = replace(owned, element=self.roof_owners[point] + ".roof",
                                       role=self.roof_roles[point])
            child.place(point, BlockSpec(block=state))
        child.attributes = owned
        for point in sorted(self.supports, key=lambda p: (p.x, p.y, p.z)):
            child.place(point, self.frame)
        for point, parts in self.shared.items():
            spec = self.wall
            if any(point.y in p.floors or point.y == p.top for p in parts):
                spec = self.frame
            if point in self.jambs:
                spec = self.frame
            if point not in self.passages:
                child.place(point, spec)
        child.attributes = replace(owned, reserved="circulation", role="floor")
        for point, spec in self.passages.items():
            child.place(point, spec)
        for step in self.steps:
            child.execute(step)
