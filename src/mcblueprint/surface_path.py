"""明示経由点の地表道路を造成後の地形から既存pathへ具体化する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.foundation import AIR, STABLE_GROUND, FoundationPlan
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import BlockSpec
from mcblueprint.operations.path import PathOperation, parse_points
from mcblueprint.terrain import ColumnWrites, RegionTerrainSnapshot, TerrainPlacement
from mcblueprint.volume import MAX_VOLUME, BlockVolume

INTERACTIVE_DOORS = frozenset(
    f"minecraft:{wood}_door" for wood in (
        "oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry",
        "bamboo", "pale_oak", "crimson", "warped",
    )
) | frozenset(
    f"minecraft:{wax}{age}copper_door"
    for wax in ("", "waxed_") for age in ("", "exposed_", "weathered_", "oxidized_")
)


@dataclass(frozen=True, slots=True)
class SurfacePathSpec:
    """Yを持たないXZ経由点と、入口足元・探索帯・幅方向も含めた上限。"""

    points: tuple[tuple[int, int], ...]
    entrance: Vec3
    width: int
    block: BlockState
    stairs: BlockState
    clearance: int
    scan_min_y: int
    scan_max_y: int
    ground: frozenset[str]
    max_cut: int
    max_fill: int
    max_support_depth: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SurfacePathSpec:
        """Schema検証済みのsurfacePath項目を変換する。対応外条件はBlueprintError。"""
        ground = [BlockState.parse(text) for text in raw["ground"]]
        if any(state.properties for state in ground):
            raise BlueprintError("surfacePath.ground は安定地盤材IDを指定してください")
        return cls(
            tuple(tuple(point) for point in raw["points"]), Vec3.from_seq(raw["entrance"]),
            raw["width"], BlockState.parse(raw["block"]), BlockState.parse(raw["stairs"]),
            raw["clearance"], raw["scan"]["minY"], raw["scan"]["maxY"],
            frozenset(state.id for state in ground), raw["maxCut"], raw["maxFill"],
            raw["maxSupportDepth"],
        )

    def __post_init__(self) -> None:
        if (not self.ground or not self.ground <= STABLE_GROUND
                or self.block.id not in STABLE_GROUND or self.block.properties):
            raise BlueprintError("surfacePath のground/blockは対応する安定地盤材IDが必要です")
        if (not self.stairs.id.endswith("_stairs")
                or any(self.stairs.get(key) is not None for key in ("facing", "half", "shape"))
                or self.stairs.get("waterlogged") == "true"):
            raise BlueprintError("surfacePath.stairs は向き未指定・非水没の階段材が必要です")
        if (self.width < 1 or self.clearance < 2 or self.max_cut < 0 or self.max_fill < 0
                or self.max_support_depth < 1 or self.scan_min_y >= self.scan_max_y):
            raise BlueprintError("surfacePath の幅・頭上空間・探索帯・上限が不正です")
        points = [Vec3(x, 0, z) for x, z in self.points]
        parse_points({"points": [p.to_list() for p in points]}, "surfacePath")
        for before, point, after in zip(points, points[1:], points[2:], strict=False):
            incoming, outgoing = _step(before, point), _step(point, after)
            if incoming.x * outgoing.x + incoming.z * outgoing.z != 0:
                raise BlueprintError("surfacePath の中間点は90度の曲がり角にしてください")
        last_step = _step(points[-2], points[-1])
        if self.entrance.with_axis("y", 0) != points[-1] + last_step:
            raise BlueprintError("surfacePath 終点の進行方向1セル先をentranceにしてください")


def _step(a: Vec3, b: Vec3) -> Vec3:
    return Vec3((b.x > a.x) - (b.x < a.x), 0, (b.z > a.z) - (b.z < a.z))


@dataclass(frozen=True, slots=True)
class SurfacePathIssue:
    position: Vec3
    reason: str


class SurfacePathError(BlueprintError):
    """全幅の道路・入口が成立しない。issuesの座標・原因を確認して入力を修正する。"""

    def __init__(self, issues: tuple[SurfacePathIssue, ...]) -> None:
        self.issues = issues
        super().__init__("surfacePath を解決できません:\n" + "\n".join(
            f"{issue.position}: {issue.reason}" for issue in issues
        ))


@dataclass(frozen=True, slots=True)
class SurfacePathResult:
    points: tuple[Vec3, ...]
    entrance: Vec3
    width: int
    surface_cells: int
    changed_cells: int
    cut_cells: int
    fill_cells: int
    required_cut: int
    required_fill: int
    required_support_depth: int

    def to_json(self) -> dict[str, Any]:
        return {
            "points": [point.to_list() for point in self.points],
            "entrance": self.entrance.to_list(), "width": self.width,
            "surfaceCells": self.surface_cells, "changedCells": self.changed_cells,
            "cutCells": self.cut_cells, "fillCells": self.fill_cells,
            "surfaceChangedCells": self.changed_cells - self.cut_cells - self.fill_cells,
            "requiredCut": self.required_cut, "requiredFill": self.required_fill,
            "requiredSupportDepth": self.required_support_depth,
        }


@dataclass(frozen=True, slots=True)
class SurfacePathPlan:
    """Blueprint座標の不変変更列。surfacesは道路本体、入口セルはentranceで別管理。"""

    writes: tuple[tuple[Vec3, BlockState], ...] | ColumnWrites
    surfaces: tuple[tuple[Vec3, BlockState], ...]
    head_tops: tuple[tuple[Vec3, int], ...]
    clearance: int
    result: SurfacePathResult
    minecraft_version: str


def surface_geometry(points, width, block, stairs, clearance=2):
    """解決済みの路面経由点をpathへ写し、幅に応じた曲角の踊り場を検査する。

    地表道路とSite plannerで共有する工学規則。材質はBlockState、pointsはVec3。
    支持地盤・切盛土・水域の検査は呼出し側の責務。不正な経由点はBlueprintError、
    踊り場不足はSurfacePathError。
    """
    parse_points({"points": [point.to_list() for point in points]}, "surfacePath")
    path = PathOperation("surfacePath", points, BlockSpec(block=block), width=width,
                         stairs=stairs, clearance=clearance)
    line = path.centre_line()
    radius = (width + 1) // 2
    for corner in points[1:-1]:
        index = next(i for i, (point, _) in enumerate(line) if point == corner)
        if any(point.y != corner.y for point, _ in line[max(0, index - radius):index + radius + 1]):
            raise SurfacePathError((SurfacePathIssue(
                corner, "曲がり角の前後は幅に応じた平坦な踊り場が必要です"),))
    return path


def plan_surface_path(
    spec: SurfacePathSpec, terrain: TerrainPlacement, foundation: FoundationPlan | None = None,
) -> SurfacePathPlan:
    """元snapshotへfoundation.writesを反映した状態から、全幅を検査して計画する。

    入力snapshot・造成計画・volumeを変更しない。失敗はSurfacePathError。
    高さは経由点断面中央値（終点は入口足元-1）と既存pathの区間補間で決める。
    探索帯外の地盤・頭上や、別経路・別の高さ候補は探索しない。
    """
    region = terrain.region
    first = Vec3(spec.points[0][0], spec.scan_min_y, spec.points[0][1])
    compact = isinstance(terrain.snapshot, RegionTerrainSnapshot)
    if region.volume > MAX_VOLUME or spec.width > max(region.size.x, region.size.z):
        raise SurfacePathError((SurfacePathIssue(first, "施工セル数または幅が範囲上限を超えます"),))
    if compact:
        length = sum(abs(a[0] - b[0]) + abs(a[1] - b[1])
                     for a, b in zip(spec.points, spec.points[1:], strict=False)) + 1
        columns = length * spec.width + max(0, len(spec.points) - 2) * spec.width ** 2 + 1
        if columns * 4096 > terrain.snapshot.plan_budget_bytes:
            raise BlueprintError("道路の列検査・計画が limits.max_memory_mb に収まりません")
    blocks = blockdata.load_block_data(terrain.snapshot.minecraft_version)
    problem = blocks.check_state(spec.stairs)
    if problem is not None:
        raise SurfacePathError((SurfacePathIssue(first, problem),))
    overlay = (foundation.writes if foundation is not None
               and isinstance(foundation.writes, ColumnWrites)
               else dict(foundation.writes) if foundation is not None else {})

    def current(pos: Vec3) -> BlockState | None:
        state = overlay.get(pos)
        if state is not None:
            return state
        return terrain.snapshot.get(terrain.snapshot.to_local(pos))

    raw_points = [Vec3(x, 0, z) for x, z in spec.points]
    for point in raw_points:
        if not region.contains(point.with_axis("y", spec.scan_min_y)):
            raise SurfacePathError((SurfacePathIssue(
                point, "経由点・探索下端が施工/取得範囲外です",
            ),))
    dummy = PathOperation("surfacePath", raw_points, BlockSpec(block=spec.block),
                          width=spec.width, clearance=0)
    line = dummy.centre_line()
    if len({(point.x, point.z) for point, _ in line}) != len(line):
        raise SurfacePathError((SurfacePathIssue(first, "経路の自己交差・折り返しは対応外です"),))
    footprint = {(op["position"][0], op["position"][2]) for op in dummy.expand()}
    entry = spec.entrance
    if (entry.x, entry.z) in footprint:
        raise SurfacePathError((SurfacePathIssue(entry, "入口が道路本体と重複しています"),))
    footprint.add((entry.x, entry.z))
    surfaces: dict[tuple[int, int], int] = {}
    issues: list[SurfacePathIssue] = []
    for x, z in sorted(footprint):
        surface = None
        first_air = None
        for y in range(spec.scan_min_y, spec.scan_max_y + 1):
            pos = Vec3(x, y, z)
            state = current(pos) if region.contains(pos) else None
            if state is None:
                issues.append(SurfacePathIssue(pos, "道路全幅の探索帯が施工/取得範囲外です"))
                break
            if state == AIR:
                if first_air is None:
                    first_air = y
            elif state.id not in spec.ground:
                issues.append(SurfacePathIssue(pos, f"対応外の地盤・水・障害物です: {state}"))
                break
            elif first_air is not None:
                issues.append(SurfacePathIssue(Vec3(x, first_air, z), "地盤下に空洞があります"))
                break
            else:
                surface = y
        else:
            if surface is None or first_air is None:
                issues.append(SurfacePathIssue(Vec3(x, spec.scan_min_y, z),
                                               "支持地盤または上端airがなく地表未確定です"))
            else:
                surfaces[(x, z)] = surface
    if issues:
        raise SurfacePathError(tuple(issues))
    resolved = []
    for index, point in enumerate(raw_points):
        directions = []
        if index:
            directions.append(_step(raw_points[index - 1], point))
        if index + 1 < len(raw_points):
            directions.append(_step(point, raw_points[index + 1]))
        sample = set()
        for direction in directions:
            across = Vec3(direction.z, 0, direction.x)
            sample.update((point.x + across.x * k, point.z + across.z * k)
                          for k in dummy.across_range)
        if len(directions) == 2:
            a, b = directions
            sample.update((point.x + a.z * k1 + b.z * k2, point.z + a.x * k1 + b.x * k2)
                          for k1 in dummy.across_range for k2 in dummy.across_range)
        heights = sorted(surfaces[pos] for pos in sample)
        height = heights[(len(heights) - 1) // 2]
        resolved.append(point.with_axis(
            "y", entry.y - 1 if index == len(raw_points) - 1 else height,
        ))
    try:
        parse_points({"points": [point.to_list() for point in resolved]}, "surfacePath")
    except BlueprintError as exc:
        raise SurfacePathError((SurfacePathIssue(resolved[-1], str(exc)),)) from exc
    path = surface_geometry(resolved, spec.width, spec.block, spec.stairs, spec.clearance)
    line = path.centre_line()
    road: dict[tuple[int, int], tuple[Vec3, BlockState]] = {}
    for operation in path.expand():
        if operation["type"] != "set":
            continue
        pos = Vec3.from_seq(operation["position"])
        state = BlockState.parse(operation["block"])
        previous = road.get((pos.x, pos.z))
        if previous is not None and previous != (pos, state):
            issues.append(SurfacePathIssue(pos, "道路全幅の重複部分で高さ・階段方向が競合します"))
        road[(pos.x, pos.z)] = pos, state
    for before, corner, after in zip(resolved, resolved[1:], resolved[2:], strict=False):
        incoming, outgoing = _step(before, corner), _step(corner, after)
        across_in = Vec3(incoming.z, 0, incoming.x)
        across_out = Vec3(outgoing.z, 0, outgoing.x)
        for k1 in path.across_range:
            for k2 in path.across_range:
                pos = corner + across_in * k1 + across_out * k2
                previous = road.get((pos.x, pos.z))
                if previous is not None and previous != (pos, spec.block):
                    issues.append(SurfacePathIssue(
                        pos, "角squareが他区間の高さ・路面材と競合します",
                    ))
                road[(pos.x, pos.z)] = pos, spec.block
    road_body = tuple(road.values())
    road[(entry.x, entry.z)] = entry - Vec3(0, 1, 0), spec.block
    tops = {key: point.y + spec.clearance for key, (point, _) in road.items()}
    for (a, direction), (b, _) in zip(line, line[1:], strict=False):
        if a.y == b.y:
            continue
        across = Vec3(direction.z, 0, direction.x)
        for k in path.across_range:
            for point in (a + across * k, b + across * k):
                key = point.x, point.z
                tops[key] = max(tops[key], max(a.y, b.y) + spec.clearance)
    writes = ColumnWrites(terrain.snapshot.plan_budget_bytes) if compact else {}
    cut_cells = fill_cells = required_cut = required_fill = 0
    required_support = 1
    for key, (point, state) in road.items():
        surface = surfaces[key]
        cut, fill = max(0, surface - point.y), max(0, point.y - surface)
        support = max(1, fill)
        if (cut > spec.max_cut or fill > spec.max_fill or support > spec.max_support_depth
                or point.y - 1 < spec.scan_min_y or point.y + spec.clearance + 1 > spec.scan_max_y):
            issues.append(SurfacePathIssue(point, f"上限または探索帯超過 (cut={cut}, fill={fill}, "
                                           f"support={support}, headTop={tops[key]})"))
            continue
        required_cut, required_fill = max(required_cut, cut), max(required_fill, fill)
        required_support = max(required_support, support)
        for y in range(min(surface + 1, point.y), max(surface, point.y) + 1):
            pos = point.with_axis("y", y)
            target = AIR if y > point.y else (state if y == point.y else spec.block)
            if current(pos) != target:
                if compact:
                    writes.append((pos, target))
                else:
                    writes[pos] = target
                if y > point.y:
                    cut_cells += 1
                elif y < point.y:
                    fill_cells += 1
    if issues:
        raise SurfacePathError(tuple(issues))
    ordered = (writes if compact else
               tuple(sorted(writes.items(), key=lambda item: (item[0].z, item[0].x, item[0].y))))
    return SurfacePathPlan(
        ordered, road_body, tuple((point, tops[key]) for key, (point, _) in road.items()),
        spec.clearance, SurfacePathResult(tuple(resolved), entry, spec.width, len(road),
                                         len(ordered), cut_cells, fill_cells, required_cut,
                                         required_fill, required_support),
        terrain.snapshot.minecraft_version,
    )


def check_surface_path(
    volume: BlockVolume, plan: SurfacePathPlan, *, core: AABB | None = None,
) -> None:
    """通常Operation適用後の全幅路面・支持・昇降遷移頭上・入口を確認する。

    入口の明示した木製・銅製ドアは開けて通る前提で許容する。鉄ドアや他の
    衝突形状は推測しない。失敗はSurfacePathErrorで、volumeを変更しない。
    """
    issues = []
    entry = plan.result.entrance
    floor = entry - Vec3(0, 1, 0)
    for pos, state in plan.surfaces:
        if core is not None and not core.contains(pos):
            continue
        if volume.get(pos) != state:
            issues.append(SurfacePathIssue(pos, "後続Operationが路面を変更しました"))
        below = volume.get(pos - Vec3(0, 1, 0))
        if below is None or below.id not in STABLE_GROUND:
            issues.append(SurfacePathIssue(pos, "道路全幅の支持がありません"))
    entry_floor = volume.get(floor)
    check_entry = core is None or core.contains(floor)
    if check_entry and (entry_floor is None or (entry_floor.id not in STABLE_GROUND
                               and not entry_floor.id.endswith("_planks"))):
        issues.append(SurfacePathIssue(floor, "入口足元の床が通常完全ブロックではありません"))
    entry_below = volume.get(floor - Vec3(0, 1, 0))
    if check_entry and (entry_below is None or entry_below.id not in STABLE_GROUND):
        issues.append(SurfacePathIssue(floor, "入口接続セルの支持がありません"))
    door = volume.get(entry)
    interactive_door = (door is not None and door.id in INTERACTIVE_DOORS
                        and door.get("half") in (None, "lower"))
    if interactive_door and check_entry:
        upper = volume.get(entry + Vec3(0, 1, 0))
        direction = _step(plan.result.points[-1], entry)
        allowed = ("east", "west") if direction.x else ("north", "south")
        blocks = blockdata.load_block_data(plan.minecraft_version)
        lower_state = blocks.complete(door)
        upper_state = blocks.complete(upper) if upper is not None and upper.id == door.id else None
        if (upper_state is None or upper_state.get("half") != "upper"
                or lower_state.get("facing") not in allowed
                or any(lower_state.get(key) != upper_state.get(key)
                       for key in ("facing", "open", "hinge", "powered"))):
            issues.append(SurfacePathIssue(entry, "ドアの上下状態または入口の向きが不一致です"))
    for pos, top in plan.head_tops:
        for y in range(pos.y + 1, top + 1):
            cell = pos.with_axis("y", y)
            if core is not None and not core.contains(cell):
                continue
            state = volume.get(cell)
            if state == AIR:
                continue
            if pos == floor and interactive_door and state is not None and state.id == door.id:
                if (y == entry.y and state.get("half") in (None, "lower")
                        or y == entry.y + 1 and state.get("half") == "upper"):
                    continue
            issues.append(SurfacePathIssue(cell, "道路全幅・昇降遷移・入口の頭上に干渉があります"))
    if issues:
        raise SurfacePathError(tuple(issues))
