"""計画された構造・外壁をWeather後の生成領域から評価する。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.shapes import disc_offsets
from mcblueprint.operations.transform import Transform

_AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
_NORMAL = {"north": (0, 0, -1), "south": (0, 0, 1), "east": (1, 0, 0), "west": (-1, 0, 0)}


@dataclass(frozen=True)
class StructuralRun:
    """柱または梁の期待セルと予約owner。"""

    owner: str
    cells: tuple[Vec3, ...]


@dataclass(frozen=True)
class FacadeCell:
    """外壁投影のセル。depthは面法線方向の基準面からの距離。"""

    pos: Vec3
    u: int
    v: int
    depth: int
    opening: bool = False
    segment: int = 0


@dataclass(frozen=True)
class Facade:
    """一つの建物・向き付き面に属する外壁投影。"""

    element: str
    face: str
    cells: tuple[FacadeCell, ...]
    role: str = "wall_main"
    building: str | None = None


@dataclass(frozen=True)
class CriticPlan:
    """Criticの期待構造・面・装飾を表す内部計画。"""

    columns: tuple[StructuralRun, ...]
    beams: tuple[StructuralRun, ...]
    facades: tuple[Facade, ...]
    decoration_operations: Mapping[str, tuple[str, str]]
    decoration_range: tuple[float, float]
    fixed_roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class CriticResult:
    metrics: dict[str, dict]
    diagnostics: tuple[dict, ...]


def plan_from_metadata(
    planning: Mapping,
    *,
    massing=None,
    offset=(0, 0, 0),
    decoration_range=(0.0, 1.0),
    fixed_roles=(),
) -> CriticPlan:
    """計画範囲を期待セルへ展開する。massingの構造形状は梁の輪郭とownerに使う。

    offsetはSiteの平行移動。装飾はdecorationsの明示指定だけを使い、省略時は対象なし。
    計画の欠落・不整合はBlueprintError。最終生成セルから計画を推定しない。
    """

    try:
        return _convert_plan(planning, massing, offset, decoration_range, fixed_roles)
    except (KeyError, TypeError, ValueError) as exc:
        raise BlueprintError(f"Critic計画が不正です: {exc}") from exc


def _convert_plan(planning, massing, offset, decoration_range, fixed_roles):
    def box(item):
        return AABB.of(Vec3.from_seq(item["from"]), Vec3.from_seq(item["to"]))

    shapes = {}
    params = planning.get("params", {})
    transform = Transform.rotation(params.get("angle", 0), Vec3(0, 0, 0)).then(
        Transform.translation(Vec3.from_seq(offset))
    )
    if massing is not None:
        for op in massing["operations"]:
            if op.get("reserved") != "structure":
                continue
            owner = op.get("owner", op["element"])
            for child in op["operations"]:
                shape = build_operation(child, "critic.plan")
                bounds = transform.bounds(shape.bounds())
                shapes[bounds] = (owner, tuple(transform.apply(p) for p in shape.cells()))

    def run(item):
        bounds = box(item)
        if massing is not None:
            if bounds not in shapes:
                raise BlueprintError(f"Critic計画の構造形状がありません: {item['id']}")
            owner, cells = shapes[bounds]
        else:
            owner, cells = item.get("owner", item["id"]), tuple(iter_box(bounds))
        return StructuralRun(owner, cells)

    columns = tuple(run(item) for item in planning["columns"])
    beams = tuple(run(item) for item in planning["beams"])
    contour = None
    if params.get("shape") == "round":
        contour = [transform.apply(p) for p in disc_offsets(params["radius"], "hollow", "y")]
    facades = []
    used = set()
    for item in planning["facades"]:
        normal = Vec3.from_seq(item["normal"])
        origin = Vec3.from_seq(item["origin"])
        axis = Vec3.from_seq(item["uAxis"])
        face = max(
            _NORMAL,
            key=lambda name: sum(
                a * b for a, b in zip(_NORMAL[name], normal.to_list(), strict=True)
            ),
        )
        direction = Vec3.from_seq(_NORMAL[face])
        bounds = box(item)
        points = list(iter_box(bounds))
        if contour is not None:
            points = [
                Vec3(p.x, y, p.z)
                for p in contour
                if bounds.min.x <= p.x <= bounds.max.x and bounds.min.z <= p.z <= bounds.max.z
                for y in range(bounds.min.y, bounds.max.y + 1)
            ]
        else:
            points = [
                p for p in points if (p - origin).x * normal.x + (p - origin).z * normal.z == 0
            ]
        # 同じ投影rayには輪郭の最外セルを一つだけ残す。
        rays = {}
        for pos in points:
            u = pos.x if direction.z else pos.z
            key = u, pos.y
            depth = pos.x * direction.x + pos.z * direction.z
            if key not in rays or depth > rays[key][0]:
                rays[key] = depth, pos
        projected = []
        for depth, pos in rays.values():
            if (face, pos) in used:
                continue
            used.add((face, pos))
            u = (pos - origin).x * axis.x + (pos - origin).z * axis.z
            v = pos.y - origin.y
            opening = False
            for entry in item["openings"]:
                if "projection" in entry:
                    mask = entry["projection"]
                    opening |= mask["u"][0] <= u <= mask["u"][1] and (
                        mask["v"][0] <= v <= mask["v"][1]
                    )
                else:
                    opening |= box(entry).contains(pos)
            # roundの階段状輪郭はそれぞれを深度0に正規化する。
            local_u = pos.x - bounds.min.x if direction.z else pos.z - bounds.min.z
            projected.append(FacadeCell(pos, local_u, v, depth, opening, segment=len(facades)))
        facades.append(Facade(item["id"], face, tuple(projected), building=planning.get("element")))
    decorations = {
        item["operation"]: (item["element"], item["face"])
        for item in planning.get("decorations", [])
    }
    targets = {(f.element, f.face) for f in facades}
    if any(target not in targets for target in decorations.values()):
        raise BlueprintError("Critic計画の装飾対象外壁がありません")
    return CriticPlan(
        columns, beams, tuple(facades), decorations, tuple(decoration_range), frozenset(fixed_roles)
    )


def _state(region: GeneratedRegion, pos: Vec3):
    if not region.core.contains(pos):
        return None, None
    return region.volume.get(pos), region.volume.attributes(pos)


def _present(state) -> bool:
    return state is not None and state.id not in _AIR


class CriticAccumulator:
    """Weather後coreの要約だけを保持し、全領域の消費後に評価を確定する。"""

    def __init__(self, plan: CriticPlan, *, max_memory_mb: float = 2048):
        self.plan = plan
        if max_memory_mb <= 0:
            raise ValueError("max_memory_mb は正数が必要です")
        facade_cells = sum(len(f.cells) for f in self.plan.facades)
        estimated = facade_cells * 1024 + sum(
            len(run.cells) * 256 for run in (*plan.columns, *plan.beams)
        )
        if estimated > max_memory_mb * 1024 * 1024:
            raise BlueprintError("Criticの面要約が max_memory_mb に収まりません")
        self.summary_bytes = estimated
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        if not self.plan.columns or not self.plan.facades:
            raise BlueprintError("Critic計画に期待柱または外壁面がありません")
        if any(
            f.face not in _NORMAL or not f.cells or not any(not c.opening for c in f.cells)
            for f in self.plan.facades
        ):
            raise BlueprintError("Critic計画の外壁面または投影セルが不正です")
        if any(not run.cells for run in (*self.plan.columns, *self.plan.beams)):
            raise BlueprintError("Critic計画に期待セルのない構造runがあります")
        self.low, self.high = self.plan.decoration_range
        if not 0 <= self.low <= self.high <= 1:
            raise BlueprintError("Critic計画の装飾密度範囲が不正です")

        self.structural_seen: dict[tuple, bool] = {}
        self.structural_runs = [
            (kind, index, run)
            for kind, runs in (("column", self.plan.columns), ("beam", self.plan.beams))
            for index, run in enumerate(runs)
        ]
        self.facade_blocks: dict[tuple[str, str, str], Counter] = {}
        self.facade_seen: set[tuple[int, int]] = set()
        self.decorated = Counter()
        self.decorated_seen: set[Vec3] = set()
        self.surfaces = {}
        self.wall_seen = set()
        self.projections = {}
        for fi, facade in enumerate(plan.facades):
            for ci, cell in enumerate(facade.cells):
                if cell.opening:
                    continue
                axis = 0 if facade.face in ("north", "south") else 2
                self.projections.setdefault((axis, cell.pos[axis], cell.pos.y), []).append((fi, ci))

    def consume(self, region: GeneratedRegion) -> None:
        """同じcoreの再入力は重複計数せず、haloは参照しない。"""
        for kind, index, run in self.structural_runs:
            for pos_index, pos in enumerate(run.cells):
                if not region.core.contains(pos):
                    continue
                key = (kind, index, pos_index)
                if key in self.structural_seen:
                    continue
                state, attr = _state(region, pos)
                self.structural_seen[key] = (
                    _present(state)
                    and attr.owner == run.owner
                    and attr.reserved == "structure"
                    and attr.role == "frame"
                )

        for fi, facade in enumerate(self.plan.facades):
            for ci, cell in enumerate(facade.cells):
                if not cell.opening and region.core.contains(cell.pos):
                    self.facade_seen.add((fi, ci))
        for pos, state in region.volume:
            if not region.core.contains(pos) or not _present(state):
                continue
            attr = region.volume.attributes(pos)
            target = self.plan.decoration_operations.get(attr.operation)
            if target is not None and pos not in self.decorated_seen:
                self.decorated_seen.add(pos)
                self.decorated[target] += 1
            for axis in (0, 2):
                for fi, ci in self.projections.get((axis, pos[axis], pos.y), ()):
                    facade = self.plan.facades[fi]
                    cell = facade.cells[ci]
                    if attr.face is not None and attr.face != facade.face:
                        continue
                    element = attr.element or ""
                    if not (
                        element == facade.element
                        or element.startswith(facade.element + ".")
                        or facade.building
                        and element.startswith(facade.building + ".")
                    ):
                        continue
                    if attr.role not in (
                        "wall_main",
                        "wall_secondary",
                        "frame",
                        "trim",
                        "window_frame",
                        "accent",
                    ):
                        continue
                    normal = _NORMAL[facade.face]
                    depth = pos.x * normal[0] + pos.z * normal[2]
                    if facade.building and not element.startswith(facade.element):
                        depth_axis = 2 if axis == 0 else 0
                        distance = abs(pos[depth_axis] - cell.pos[depth_axis])
                        nearest = min(
                            abs(pos[depth_axis] - self.plan.facades[fj].cells[cj].pos[depth_axis])
                            for fj, cj in self.projections[(axis, pos[axis], pos.y)]
                            if self.plan.facades[fj].building == facade.building
                        )
                        if distance != nearest:
                            continue
                    key = fi, ci
                    previous = self.surfaces.get(key)
                    if previous is None or depth > previous:
                        self.surfaces[key] = depth
                    wall_key = fi, pos
                    if (
                        attr.role in ("wall_main", "wall_secondary")
                        and wall_key not in self.wall_seen
                    ):
                        self.wall_seen.add(wall_key)
                        self.facade_blocks.setdefault(
                            (facade.element, facade.face, attr.role), Counter()
                        )[state.id] += 1
        used = self.summary_bytes + len(self.decorated_seen) * 128 + len(self.wall_seen) * 160
        if used > self.max_memory_bytes:
            raise BlueprintError("Criticの集計要約が max_memory_mb に収まりません")

    def finalize(self) -> CriticResult:
        """全coreの集計値から指標と診断を返す。"""
        self.columns_total = self.columns_present = self.beams_total = self.beams_present = 0
        self.missing_structure = []
        self.missing_surface = []
        self.facade_area = Counter()
        self.flat_windows = {}
        self.diagnostics = []
        for fi, facade in enumerate(self.plan.facades):
            for ci, cell in enumerate(facade.cells):
                if cell.opening:
                    continue
                key = fi, ci
                self.facade_area[(facade.element, facade.face)] += 1
                if key not in self.surfaces:
                    self.missing_surface.append(
                        {
                            "element": facade.element,
                            "face": facade.face,
                            "position": cell.pos.to_list(),
                            "reason": "missing" if key in self.facade_seen else "unobserved",
                        }
                    )
                else:
                    window = (facade.element, facade.face, cell.segment, cell.u // 4, cell.v // 4)
                    self.flat_windows.setdefault(window, []).append(self.surfaces[key] - cell.depth)

        for kind, index, run in self.structural_runs:
            expected = [(kind, index, pos_index) for pos_index in range(len(run.cells))]
            run_missing = [
                (pos_index, "unobserved" if key not in self.structural_seen else "missing")
                for pos_index, key in enumerate(expected)
                if key not in self.structural_seen or not self.structural_seen[key]
            ]
            if kind == "column":
                self.columns_total += 1
                self.columns_present += not run_missing
            else:
                self.beams_total += 1
                self.beams_present += not run_missing
            self.missing_structure.extend(
                {
                    "kind": kind,
                    "owner": run.owner,
                    "position": run.cells[i].to_list(),
                    "reason": reason,
                }
                for i, reason in run_missing
            )

        if self.missing_structure:
            self.diagnostics.append(
                {
                    "code": "E101",
                    "severity": "error",
                    "metric": "column_continuity",
                    "element": self.missing_structure[0]["owner"],
                    "actual": len(self.missing_structure),
                    "expected": 0,
                    "repairPaths": [],
                    "violations": self.missing_structure,
                }
            )
        missing_by_face = {}
        for violation in self.missing_surface:
            key = violation["element"], violation["face"]
            missing_by_face.setdefault(key, []).append(violation)
        for (element, face), violations in missing_by_face.items():
            self.diagnostics.append(
                {
                    "code": "E203",
                    "severity": "fail",
                    "metric": "facade_surface_missing",
                    "element": element,
                    "face": face,
                    "actual": len(violations),
                    "expected": 0,
                    "repairPaths": ["config.weathering.missing_blocks"],
                    "violations": violations,
                }
            )

        flat = sum(
            bool(values) and max(values) == min(values) for values in self.flat_windows.values()
        )
        densities = {}
        for key, area in self.facade_area.items():
            if key not in self.plan.decoration_operations.values():
                continue
            density = self.decorated[key] * 100 / area
            densities[f"{key[0]}:{key[1]}"] = {
                "value": density,
                "numerator": self.decorated[key],
                "denominator": area,
            }
            if not self.low * 100 <= density <= self.high * 100:
                self.diagnostics.append(
                    {
                        "code": "E202",
                        "severity": "fail",
                        "metric": "decoration_density",
                        "element": key[0],
                        "face": key[1],
                        "actual": density,
                        "expected": [self.low * 100, self.high * 100],
                        "repairPaths": ["config.decoration.density"],
                    }
                )

        mixes = {}
        for key, counts in self.facade_blocks.items():
            element, face, role = key
            if role in self.plan.fixed_roles:
                continue
            total = sum(counts.values())
            if total:
                value = max(counts.values()) / total
                mixes[f"{element}:{face}:{role}"] = {
                    "value": value,
                    "numerator": max(counts.values()),
                    "denominator": total,
                }
                if value > 0.85:
                    self.diagnostics.append(
                        {
                            "code": "E305",
                            "severity": "warn",
                            "metric": "facade_mix",
                            "element": element,
                            "face": face,
                            "role": role,
                            "actual": value,
                            "expected": 0.85,
                            "repairPaths": ["config.palette.mix"],
                        }
                    )
        flat_by_face = Counter()
        windows_by_face = Counter()
        for (element, face, _segment, _u, _v), values in self.flat_windows.items():
            windows_by_face[(element, face)] += 1
            flat_by_face[(element, face)] += bool(values) and max(values) == min(values)
        for key, total in windows_by_face.items():
            value = flat_by_face[key] / total
            if value > 0.35:
                self.diagnostics.append(
                    {
                        "code": "E201",
                        "severity": "fail",
                        "metric": "flat_ratio",
                        "element": key[0],
                        "face": key[1],
                        "actual": value,
                        "numerator": flat_by_face[key],
                        "denominator": total,
                        "expected": 0.35,
                        "repairPaths": ["config.decoration.depth", "config.decoration.density"],
                    }
                )
        return CriticResult(
            {
                "column_continuity": {
                    "value": self.columns_present / self.columns_total
                    if self.columns_total
                    else None,
                    "numerator": self.columns_present,
                    "denominator": self.columns_total,
                },
                "beam_continuity": {
                    "value": self.beams_present / self.beams_total if self.beams_total else None,
                    "numerator": self.beams_present,
                    "denominator": self.beams_total,
                },
                "flat_ratio": {
                    "value": flat / len(self.flat_windows) if self.flat_windows else None,
                    "numerator": flat,
                    "denominator": len(self.flat_windows),
                },
                "decoration_density": densities,
                "facade_mix": mixes,
                "facade_surface_missing": {
                    "value": len(self.missing_surface),
                    "denominator": sum(self.facade_area.values()),
                },
            },
            tuple(self.diagnostics),
        )


def evaluate(
    plan: CriticPlan, regions: Iterable[GeneratedRegion], *, max_memory_mb: float = 2048
) -> CriticResult:
    """期待計画をWeather後の各region coreから一度ずつ評価する。"""
    accumulator = CriticAccumulator(plan, max_memory_mb=max_memory_mb)
    for region in regions:
        accumulator.consume(region)
    return accumulator.finalize()
