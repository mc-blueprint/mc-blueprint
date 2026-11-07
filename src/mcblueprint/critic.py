"""計画された構造・外壁をWeather後の生成領域から評価する。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from mcblueprint.errors import BlueprintError
from mcblueprint.facade_projection import NORMALS as _NORMAL
from mcblueprint.facade_projection import Facade as Facade
from mcblueprint.facade_projection import FacadeCell as FacadeCell
from mcblueprint.facade_projection import project_facades
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import ExecutionContext
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.transform import Transform
from mcblueprint.plan_contract import plan_buildings

_AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
_WALL_ROLES = frozenset({"wall_main", "wall_secondary", "frame", "trim", "window_frame", "accent"})


@dataclass(frozen=True)
class StructuralRun:
    """構造の期待セルと予約owner。role=Noneの屋根は意味役割を限定せず検査する。"""

    owner: str
    cells: tuple[Vec3, ...]
    role: str | None = "frame"


@dataclass(frozen=True)
class CriticPlan:
    """Criticの期待構造・面・装飾を表す内部計画。"""

    columns: tuple[StructuralRun, ...]
    beams: tuple[StructuralRun, ...]
    facades: tuple[Facade, ...]
    decoration_operations: Mapping[str, tuple[str, str]]
    decoration_range: tuple[float, float]
    fixed_roles: frozenset[str] = frozenset()
    terrain_only: bool = False
    building_fixed_roles: Mapping[str, frozenset[str]] = field(default_factory=dict)
    junction_cells: Mapping[Vec3, str] = field(default_factory=dict)


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
    building_fixed_roles=None,
) -> CriticPlan:
    """計画範囲を期待セルへ展開する。massingの構造形状は梁の輪郭とownerに使う。

    offsetはSiteの平行移動。装飾はdecorationsの明示指定だけを使い、省略時は対象なし。
    building_fixed_rolesは棟IDごとの固定役割。単独棟は従来のfixed_rolesを使う。
    計画の欠落・不整合はBlueprintError。最終生成セルから計画を推定しない。
    """

    try:
        plan = _convert_plan(planning, massing, offset, decoration_range, fixed_roles)
        return replace(plan, building_fixed_roles={
            root: frozenset(roles) for root, roles in (building_fixed_roles or {}).items()
        })
    except (KeyError, TypeError, ValueError) as exc:
        raise BlueprintError(f"Critic計画が不正です: {exc}") from exc


def _convert_plan(planning, massing, offset, decoration_range, fixed_roles):
    def box(item):
        return AABB.of(Vec3.from_seq(item["from"]), Vec3.from_seq(item["to"]))

    if "formatVersion" in planning:
        buildings = plan_buildings(planning)
        shapes = {b["element"]: b for b in plan_buildings(massing)} if massing is not None else {}
        if massing is not None and set(shapes) != {b["element"] for b in buildings}:
            raise BlueprintError("Critic計画とMassingの棟IDが一致しません")
        if massing is not None and planning.get("supports", []) != massing.get("supports", []):
            raise BlueprintError("Critic計画とMassingの敷地支柱が一致しません")
        plans = [_convert_plan(b, shapes.get(b["element"]), b["offset"],
                               decoration_range, fixed_roles) for b in buildings]
        from mcblueprint.junctions import junction_geometry

        junctions = junction_geometry(planning)
        internal = {point for junction in junctions for point in junction.shared}
        transferred = set(internal)
        transferred.update(point for junction in junctions for point in junction.removed_frame)

        def retain(runs):
            return tuple(replace(run, cells=cells) for run in runs
                         if (cells := tuple(p for p in run.cells if p not in transferred)))

        junction_beams = tuple(StructuralRun(j.element, tuple(
            point for point, parts in j.shared.items() if point not in j.passages
            and any(point.y == part.top or point.y in part.floors for part in parts)
        )) for j in junctions)
        import random

        from mcblueprint.volume import BlockVolume

        roof_runs = tuple(StructuralRun(j.element, tuple(j.roof_cells(
            ExecutionContext(BlockVolume(), random.Random(0), {}))), None) for j in junctions)
        support_runs = tuple(StructuralRun(j.element, tuple(j.supports))
                             for j in junctions if j.supports)
        site_supports = tuple(StructuralRun(item["owner"], tuple(iter_box(box(item))))
                              for item in planning.get("supports", ()))
        jamb_runs = tuple(StructuralRun(j.element, tuple(j.jambs - j.passages.keys()))
                          for j in junctions if j.jambs - j.passages.keys())
        transferred.update(point for run in roof_runs for point in run.cells)
        facades, shared_surfaces = [], set()
        for p in plans:
            for facade in p.facades:
                cells = []
                for cell in facade.cells:
                    if any(j.internal_face(cell.pos, facade.face, facade.building)
                           for j in junctions):
                        continue
                    if cell.pos in internal:
                        key = facade.face, cell.pos
                        if key in shared_surfaces:
                            continue
                        shared_surfaces.add(key)
                    cells.append(cell)
                if cells and any(not c.opening for c in cells):
                    facades.append(replace(facade, cells=tuple(cells)))
        targets = {(f.element, f.face) for f in facades}
        return CriticPlan(
            retain(run for p in plans for run in p.columns)
            + support_runs + jamb_runs + site_supports,
            retain(run for p in plans for run in p.beams) + junction_beams
            + tuple(run for run in roof_runs if run.cells),
            tuple(facades),
            {key: value for p in plans for key, value in p.decoration_operations.items()
             if value in targets},
            tuple(decoration_range), frozenset(fixed_roles), not buildings,
            junction_cells={**{point: j.element for j in junctions for point in j.shared},
                            **{point: j.element for j in junctions for point in j.supports},
                            **{point: run.owner for run in roof_runs for point in run.cells}},
        )
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
    facades = project_facades(planning, offset=offset)
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
    """Weather後coreの要約だけを保持し、全領域の消費後に評価を確定する。

    max_area_ratiosは役割ごとの補正済み面積上限。Noneなら面積を評価しない。
    面積の分母はfacade.buildingで示した建物root配下の全役割セルとする。
    """

    def __init__(self, plan: CriticPlan, *, max_memory_mb: float = 2048,
                 max_area_ratios: Mapping[str, float] | None = None,
                 building_area_ratios: Mapping[str, Mapping[str, float]] | None = None):
        self.plan = plan
        self.max_area_ratios = max_area_ratios
        self.building_area_ratios = building_area_ratios or {}
        self.role_counts = {root: Counter() for root in sorted({
            facade.building for facade in plan.facades if facade.building
        })} if max_area_ratios is not None else {}
        self.color_cores = set()
        if max_memory_mb <= 0:
            raise ValueError("max_memory_mb は正数が必要です")
        facade_cells = sum(len(f.cells) for f in self.plan.facades)
        estimated = facade_cells * 1152 + sum(
            len(run.cells) * 384 for run in (*plan.columns, *plan.beams)
        )
        if estimated > max_memory_mb * 1024 * 1024:
            raise BlueprintError("Criticの面要約が max_memory_mb に収まりません")
        self.summary_bytes = estimated
        self.max_memory_bytes = max_memory_mb * 1024 * 1024
        if not self.plan.terrain_only and (not self.plan.columns or not self.plan.facades):
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
        self.structural_tiles = {}
        for kind, index, run in self.structural_runs:
            for pi, pos in enumerate(run.cells):
                tile = pos.x // 32, pos.y // 16, pos.z // 32
                self.structural_tiles.setdefault(tile, []).append((kind, index, pi, pos, run))
        self.facade_tiles = {}
        self.facade_blocks: dict[tuple[str, str, str], Counter] = {}
        self.facade_seen: set[tuple[int, int]] = set()
        self.surface_present: set[tuple[int, int]] = set()
        self.missing_sources = {}
        self.decorated = Counter()
        self.decorated_seen: set[Vec3] = set()
        self.surfaces = {}
        self.wall_seen = set()
        self.projections = {}
        for fi, facade in enumerate(plan.facades):
            for ci, cell in enumerate(facade.cells):
                if cell.opening:
                    continue
                pos = cell.pos
                tile = pos.x // 32, pos.y // 16, pos.z // 32
                self.facade_tiles.setdefault(tile, []).append((fi, ci, cell))
                axis = facade.ray_axis
                self.projections.setdefault((axis, *facade.ray_key(cell.pos)), []).append((fi, ci))

    @staticmethod
    def _core_items(index, core):
        for x in range(core.min.x // 32, core.max.x // 32 + 1):
            for y in range(core.min.y // 16, core.max.y // 16 + 1):
                for z in range(core.min.z // 32, core.max.z // 32 + 1):
                    yield from index.get((x, y, z), ())

    def consume(self, region: GeneratedRegion) -> None:
        """同じcoreの再入力は重複計数せず、haloは参照しない。"""
        count_color = self.max_area_ratios is not None and region.core not in self.color_cores
        if count_color:
            self.color_cores.add(region.core)
        for kind, index, pos_index, pos, run in self._core_items(
            self.structural_tiles, region.core,
        ):
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
                and (run.role is None or attr.role == run.role)
            )

        for fi, ci, cell in self._core_items(self.facade_tiles, region.core):
            if not region.core.contains(cell.pos):
                continue
            key = fi, ci
            if key in self.facade_seen:
                continue
            self.facade_seen.add(key)
            state, attr = _state(region, cell.pos)
            if not _present(state) and (attr.source or attr.operation):
                self.missing_sources[key] = {
                    "source": attr.source, "operation": attr.operation,
                }
        for pos, state in region.volume:
            if not region.core.contains(pos) or not _present(state):
                continue
            attr = region.volume.attributes(pos)
            if count_color and attr.role and attr.element:
                for root, counts in self.role_counts.items():
                    if attr.element == root or attr.element.startswith(root + "."):
                        counts[attr.role] += 1
                        break
            target = self.plan.decoration_operations.get(attr.operation)
            if target is not None and pos not in self.decorated_seen:
                self.decorated_seen.add(pos)
                self.decorated[target] += 1
            if attr.role not in _WALL_ROLES and not (
                attr.reserved == "structure" and self.plan.junction_cells.get(pos) == attr.owner
            ):
                continue
            for axis in (0, 2):
                projected = self.projections.get((axis, pos[axis], pos.y), ())
                if not projected:
                    continue
                nearest_by_building = {}
                for fi, ci in projected:
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
                        or self.plan.junction_cells.get(cell.pos) == attr.owner
                        and attr.reserved == "structure"
                    ):
                        continue
                    if attr.role not in _WALL_ROLES:
                        if not (pos == cell.pos and attr.reserved == "structure"
                                and self.plan.junction_cells.get(pos) == attr.owner):
                            continue
                    depth = facade.depth_at(pos)
                    if facade.building and not element.startswith(facade.element):
                        depth_axis = 2 if axis == 0 else 0
                        distance = abs(pos[depth_axis] - cell.pos[depth_axis])
                        nearest = nearest_by_building.get(facade.building)
                        if nearest is None:
                            nearest = min(
                                abs(pos[depth_axis]
                                    - self.plan.facades[fj].cells[cj].pos[depth_axis])
                                for fj, cj in self.projections[(axis, pos[axis], pos.y)]
                                if self.plan.facades[fj].building == facade.building
                            )
                            nearest_by_building[facade.building] = nearest
                        if distance != nearest:
                            continue
                    key = fi, ci
                    if pos == cell.pos:
                        self.surface_present.add(key)
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
        if self.plan.terrain_only and not self.structural_runs:
            return CriticResult({name: {"value": None, "status": "n/a",
                                        "reason": "棟なしの敷地計画"}
                                 for name in ("column_continuity", "beam_continuity", "flat_ratio",
                                              "decoration_density", "facade_mix",
                                              "facade_surface_missing")}, ())
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
                if key not in self.surface_present:
                    self.missing_surface.append(
                        {
                            "element": facade.element,
                            "face": facade.face,
                            "position": cell.pos.to_list(),
                            "reason": "missing" if key in self.facade_seen else "unobserved",
                            **self.missing_sources.get(key, {}),
                        }
                    )
                if key in self.surfaces:
                    window = (facade.element, facade.face, cell.segment, *cell.window)
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
            overrides = sorted({
                f"overrides[{v['source'].removeprefix('overrides.')}]"
                for v in violations if (v.get("source") or "").startswith("overrides.")
            })
            self.diagnostics.append(
                {
                    "code": "E203",
                    "severity": "fail",
                    "metric": "facade_surface_missing",
                    "element": element,
                    "face": face,
                    "actual": len(violations),
                    "expected": 0,
                    "repairPaths": [*overrides, "config.weathering.missing_blocks"],
                    "violations": violations,
                }
            )

        flat = sum(
            bool(values) and max(values) == min(values) for values in self.flat_windows.values()
        )
        densities = {}
        for key, area in self.facade_area.items():
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
        fixed_roles = {facade.element: self.plan.building_fixed_roles.get(
            facade.building, self.plan.fixed_roles) for facade in self.plan.facades}
        for key, counts in self.facade_blocks.items():
            element, face, role = key
            if role in fixed_roles[element]:
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
        area_ratios = {}
        for root, counts in self.role_counts.items():
            total = sum(counts.values())
            for role, maximum in self.building_area_ratios.get(root, self.max_area_ratios).items():
                value = counts[role] / total if total else None
                area_ratios[f"{root}:{role}"] = {
                    "value": value, "numerator": counts[role], "denominator": total,
                    "maximum": maximum,
                    **({"status": "failed", "reason": "建物の役割セルなし"} if not total else {}),
                }
                if not total or value > maximum:
                    self.diagnostics.append({
                        "code": "accent_area_ratio", "metric": "accent_area_ratio",
                        "severity": "fail", "element": root, "role": role,
                        "actual": value, "expected": maximum,
                        "repairPaths": ["config.palette.accent"],
                    })
        if self.max_area_ratios is not None and not area_ratios:
            area_ratios = {"value": None, "status": "n/a", "reason": "対象建物なし"}
        return CriticResult(
            {
                **({"accent_area_ratio": area_ratios}
                   if self.max_area_ratios is not None else {}),
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
                **({name: {"value": None, "status": "n/a", "reason": "棟なしの敷地計画"}
                    for name in ("flat_ratio", "decoration_density", "facade_mix",
                                 "facade_surface_missing")} if self.plan.terrain_only else {}),
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
