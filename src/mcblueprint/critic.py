"""計画された構造・外壁をWeather後の生成領域から評価する。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model.vec import Vec3

_AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
_NORMAL = {"north": (0, 0, -1), "south": (0, 0, 1),
           "east": (1, 0, 0), "west": (-1, 0, 0)}


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


def _state(region: GeneratedRegion, pos: Vec3):
    if not region.core.contains(pos):
        return None, None
    return region.volume.get(pos), region.volume.attributes(pos)


def _present(state) -> bool:
    return state is not None and state.id not in _AIR


def _aggregate(plan: CriticPlan, regions: Iterable[GeneratedRegion], *,
               max_memory_mb: float) -> CriticResult:
    if max_memory_mb <= 0:
        raise ValueError("max_memory_mb は正数が必要です")
    facade_cells = sum(len(f.cells) for f in plan.facades)
    estimated = facade_cells * 160
    if estimated > max_memory_mb * 1024 * 1024:
        raise BlueprintError("Criticの面要約が max_memory_mb に収まりません")
    if not plan.columns or not plan.facades:
        raise BlueprintError("Critic計画に期待柱または外壁面がありません")
    if any(f.face not in _NORMAL or not f.cells or not any(not c.opening for c in f.cells)
           for f in plan.facades):
        raise BlueprintError("Critic計画の外壁面または投影セルが不正です")
    if any(not run.cells for run in (*plan.columns, *plan.beams)):
        raise BlueprintError("Critic計画に期待セルのない構造runがあります")
    low, high = plan.decoration_range
    if not 0 <= low <= high <= 1:
        raise BlueprintError("Critic計画の装飾密度範囲が不正です")

    columns_total = columns_present = beams_total = beams_present = 0
    missing_structure = []
    structural_seen: dict[tuple, bool] = {}
    structural_runs = [(kind, index, run) for kind, runs in
                       (("column", plan.columns), ("beam", plan.beams))
                       for index, run in enumerate(runs)]
    flat_windows: dict[tuple, list[int]] = {}
    facade_blocks: dict[tuple[str, str, str], Counter] = {}
    facade_seen: set[tuple[int, int]] = set()
    missing_surface = []
    decorated = Counter()
    decorated_seen: set[Vec3] = set()
    facade_area = Counter()
    diagnostics = []

    for region in regions:
        for kind, index, run in structural_runs:
            for pos_index, pos in enumerate(run.cells):
                if not region.core.contains(pos):
                    continue
                key = (kind, index, pos_index)
                if key in structural_seen:
                    continue
                state, attr = _state(region, pos)
                structural_seen[key] = (
                    _present(state) and attr.owner == run.owner
                    and attr.reserved == "structure" and attr.role == "frame")

        for facade_index, facade in enumerate(plan.facades):
            role_counts = facade_blocks.setdefault(
                (facade.element, facade.face, facade.role), Counter())
            surface = {}
            normal = _NORMAL[facade.face]
            for pos, state in region.volume:
                if not region.core.contains(pos):
                    continue
                attr = region.volume.attributes(pos)
                if (not _present(state) or attr.face != facade.face
                        or not (attr.element or "").startswith(facade.element)):
                    continue
                u = pos.x if facade.face in ("north", "south") else pos.z
                key = (u, pos.y)
                depth = sum(pos[i] * normal[i] for i in range(3))
                if key not in surface or depth > surface[key][0]:
                    surface[key] = (depth, state, attr)
            for cell_index, cell in enumerate(facade.cells):
                if not region.core.contains(cell.pos):
                    continue
                if cell.opening:
                    continue
                key = (facade_index, cell_index)
                if key in facade_seen:
                    continue
                facade_seen.add(key)
                facade_area[(facade.element, facade.face)] += 1
                actual = surface.get((cell.u, cell.v))
                if actual is None or actual[2].role != facade.role:
                    missing_surface.append({"element": facade.element, "face": facade.face,
                                            "position": cell.pos.to_list(), "reason": "missing"})
                else:
                    depth, state, attr = actual
                    if attr.role == facade.role:
                        role_counts[state.id] += 1
                    key = (facade.element, facade.face, cell.segment,
                           cell.u // 4, cell.v // 4)
                    flat_windows.setdefault(key, []).append(depth - cell.depth)
        for pos, state in region.volume:
            if not region.core.contains(pos) or pos in decorated_seen:
                continue
            decorated_seen.add(pos)
            attr = region.volume.attributes(pos)
            target = plan.decoration_operations.get(attr.operation)
            if _present(state) and target is not None:
                decorated[target] += 1

    for facade_index, facade in enumerate(plan.facades):
        for cell_index, cell in enumerate(facade.cells):
            key = (facade_index, cell_index)
            if cell.opening or key in facade_seen:
                continue
            facade_area[(facade.element, facade.face)] += 1
            missing_surface.append({"element": facade.element, "face": facade.face,
                                    "position": cell.pos.to_list(), "reason": "unobserved"})

    for kind, index, run in structural_runs:
        expected = [(kind, index, pos_index) for pos_index in range(len(run.cells))]
        run_missing = [(pos_index, "unobserved" if key not in structural_seen else "missing")
                       for pos_index, key in enumerate(expected)
                       if key not in structural_seen or not structural_seen[key]]
        if kind == "column":
            columns_total += 1
            columns_present += not run_missing
        else:
            beams_total += 1
            beams_present += not run_missing
        missing_structure.extend({"kind": kind, "owner": run.owner,
                                  "position": run.cells[i].to_list(), "reason": reason}
                                 for i, reason in run_missing)

    if missing_structure:
        diagnostics.append({"code": "E101", "severity": "error",
                            "metric": "column_continuity", "actual": len(missing_structure),
                            "expected": 0, "violations": missing_structure})
    if missing_surface:
        diagnostics.append({"code": "E203", "severity": "fail",
                            "metric": "facade_surface_missing", "actual": len(missing_surface),
                            "expected": 0, "violations": missing_surface})

    flat = sum(bool(values) and max(values) == min(values)
               for values in flat_windows.values())
    densities = {}
    for key, area in facade_area.items():
        density = decorated[key] * 100 / area
        densities[f"{key[0]}:{key[1]}"] = {"value": density, "numerator": decorated[key],
                                                  "denominator": area}
        if not low * 100 <= density <= high * 100:
            diagnostics.append({"code": "E202", "severity": "fail",
                                "metric": "decoration_density", "element": key[0],
                                "face": key[1], "actual": density,
                                "expected": [low * 100, high * 100],
                                "repairPaths": ["config.decoration.density"]})

    mixes = {}
    for key, counts in facade_blocks.items():
        element, face, role = key
        if role in plan.fixed_roles:
            continue
        total = sum(counts.values())
        if total:
            value = max(counts.values()) / total
            mixes[f"{element}:{face}:{role}"] = {"value": value,
                                                  "numerator": max(counts.values()),
                                                  "denominator": total}
            if value > 0.85:
                diagnostics.append({"code": "E305", "severity": "warn",
                                    "metric": "facade_mix", "element": element,
                                    "face": face, "role": role, "actual": value,
                                    "expected": 0.85,
                                    "repairPaths": ["config.palette.mix"]})
    flat_by_face = Counter()
    windows_by_face = Counter()
    for (element, face, _segment, _u, _v), values in flat_windows.items():
        windows_by_face[(element, face)] += 1
        flat_by_face[(element, face)] += bool(values) and max(values) == min(values)
    for key, total in windows_by_face.items():
        value = flat_by_face[key] / total
        if value > 0.35:
            diagnostics.append({"code": "E201", "severity": "fail", "metric": "flat_ratio",
                                "element": key[0], "face": key[1], "actual": value,
                                "numerator": flat_by_face[key], "denominator": total,
                                "expected": 0.35,
                                "repairPaths": ["config.decoration.depth",
                                                "config.decoration.density"]})
    return CriticResult({
        "column_continuity": {"value": columns_present / columns_total if columns_total else None,
                              "numerator": columns_present, "denominator": columns_total},
        "beam_continuity": {"value": beams_present / beams_total if beams_total else None,
                            "numerator": beams_present, "denominator": beams_total},
        "flat_ratio": {"value": flat / len(flat_windows) if flat_windows else None,
                       "numerator": flat, "denominator": len(flat_windows)},
        "decoration_density": densities,
        "facade_mix": mixes,
        "facade_surface_missing": {"value": len(missing_surface),
                                   "denominator": sum(facade_area.values())},
    }, tuple(diagnostics))


def evaluate(plan: CriticPlan, regions: Iterable[GeneratedRegion], *,
             max_memory_mb: float = 2048) -> CriticResult:
    """期待計画をWeather後の各region coreから一度ずつ評価する。"""
    return _aggregate(plan, regions, max_memory_mb=max_memory_mb)
