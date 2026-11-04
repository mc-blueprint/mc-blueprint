"""Turn a Blueprint into a BlockVolume by executing its operations in order."""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, replace

from mcblueprint.errors import BlueprintError
from mcblueprint.foundation import FoundationResult
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.base import ExecutionContext, Operation, intersect
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.randomness import PaletteSampler
from mcblueprint.surface_path import check_surface_path
from mcblueprint.terrain import ColumnWrites, RegionTerrainSnapshot
from mcblueprint.volume import EMPTY_ATTRIBUTES, MAX_VOLUME, BlockVolume, RegionVolume

WEATHER_BYTES_PER_CELL = 384


def estimate_weather_memory(cells: int) -> int:
    return cells * WEATHER_BYTES_PER_CELL


@dataclass(frozen=True)
class GenerationLimits:
    """領域生成の上限。region のNoneは全高、メモリ単位はMiB。

    max_memory_mb は生成用の領域・パレットと後段の作業領域の予算。
    プロセス全体のRSS上限ではない。reference_radius指定時はその半径を使う。
    """

    region: tuple[int, int | None, int] = (64, None, 64)
    halo: int = 16
    max_memory_mb: float = 2048

    def __post_init__(self) -> None:
        if not isinstance(self.region, tuple | list) or len(self.region) != 3 or any(
            not (i == 1 and n is None) and (type(n) is not int or n < 1)
            for i, n in enumerate(self.region)
        ):
            raise BlueprintError("limits.region は正整数3個（高さのみnull可）が必要です")
        if type(self.halo) is not int or self.halo < 0:
            raise BlueprintError("limits.halo は0以上の整数が必要です")
        if (
            isinstance(self.max_memory_mb, bool)
            or not isinstance(self.max_memory_mb, int | float)
            or not math.isfinite(self.max_memory_mb) or self.max_memory_mb <= 0
        ):
            raise BlueprintError("limits.max_memory_mb は有限の正数が必要です")


@dataclass(frozen=True)
class GeneratedRegion:
    """coreだけを後段で確定する。volumeはhaloを含み、世界座標で参照する。"""

    core: AABB
    volume: RegionVolume
    world_bounds: AABB | None = None


def _check_local(operations: list[Operation]) -> tuple[int, bool, int]:
    count = 0
    has_palettes = False
    local_memory = 0
    for operation in operations:
        if operation.type == "copy":
            raise BlueprintError(f"{operation.path}: copy は領域生成では未対応です")
        count += 1
        has_palettes |= bool(getattr(operation, "palettes", {}))
        local_memory += getattr(operation, "generation_memory_bytes", 0)
        children = getattr(operation, "operations", None)
        if children is not None:
            multiplier = operation.count if operation.type == "repeat" else 1
            if operation.type == "mirror" and operation.keep_original:
                multiplier = 2
            child_count, child_palettes, child_memory = _check_local(children)
            count += multiplier * child_count
            has_palettes |= child_palettes
            local_memory += child_memory
        expanded = getattr(operation, "expanded", None)
        if expanded is not None:
            child_count, child_palettes, child_memory = _check_local(expanded)
            count += child_count
            has_palettes |= child_palettes
            local_memory += child_memory
    return count, has_palettes, local_memory


def generate_regions(
    blueprint: Blueprint, *, limits: GenerationLimits | None = None,
    seed: int | None = None, reference_radius: int = 0, reverse: bool = False,
) -> Iterator[GeneratedRegion]:
    """決定的な生成をcore/halo単位で返す。全体BlockVolumeは作らない。

    呼出し元は1領域ずつ消費・解放する。core外は参照専用で、出力・集計しない。
    copyは従来generateを使用する。予算不足時はcoreを縮め、
    1セルのcoreと必要haloでも収まらなければBlueprintErrorになる。
    各セルの適用順は元のOperation順を維持する。reverseは領域の列挙順だけを変える。
    """
    limits = limits or GenerationLimits()
    if type(reference_radius) is not int or reference_radius < 0:
        raise BlueprintError("reference_radius は0以上の整数が必要です")
    operation_count, has_palettes, local_memory = _check_local(blueprint.operations)
    if blueprint.terrain is not None:
        local_memory += getattr(blueprint.terrain.snapshot, "generation_memory_bytes", 0)
    for plan in (blueprint.foundation, blueprint.surface_path):
        if plan is not None:
            local_memory += (plan.writes.generation_memory_bytes
                             if isinstance(plan.writes, ColumnWrites) else len(plan.writes) * 256)
    if blueprint.surface_path is not None:
        local_memory += (len(blueprint.surface_path.surfaces)
                         + len(blueprint.surface_path.head_tops)) * 256
    has_palettes |= bool(blueprint.palettes)
    bounds = blueprint.bounds()
    if blueprint.terrain is not None and bounds.volume > MAX_VOLUME:
        raise BlueprintError(f"施工範囲が {MAX_VOLUME:,} セルを超えています（airを含む）")
    halo = reference_radius if reference_radius else limits.halo
    if blueprint.surface_path is not None:
        halo = max(halo, blueprint.surface_path.clearance + 2)
    size = bounds.size
    core_size = [min(n or size[i], size[i]) for i, n in enumerate(limits.region)]
    budget = limits.max_memory_mb * 1024 * 1024
    sampler = PaletteSampler(bounds, int(budget))
    # セル配列8 bytesに加え、後段snapshot・距離配列・パレットの余裕を確保する。
    def estimated_bytes() -> int:
        cells = math.prod(min(core_size[i] + 2 * halo, size[i]) for i in range(3))
        scratch = sampler.batch_size * 2600 if has_palettes else 0
        cache = 2 * sampler.cache_limit if has_palettes else 0
        generation = cells * 64 + operation_count * 4096 + scratch + cache
        weather = estimate_weather_memory(cells)
        return max(generation, weather) + local_memory

    while estimated_bytes() > budget:
        axis = max(range(3), key=lambda i: core_size[i])
        if core_size[axis] == 1:
            raise BlueprintError("最小coreと必要haloが limits.max_memory_mb に収まりません")
        core_size[axis] = max(1, core_size[axis] // 2)

    xs = range(bounds.min.x, bounds.max.x + 1, core_size[0])
    ys = range(bounds.min.y, bounds.max.y + 1, core_size[1])
    zs = range(bounds.min.z, bounds.max.z + 1, core_size[2])
    for x in reversed(xs) if reverse else xs:
        for y in reversed(ys) if reverse else ys:
            for z in reversed(zs) if reverse else zs:
                low = Vec3(x, y, z)
                high = Vec3.min(low + Vec3(*core_size) - Vec3(1, 1, 1), bounds.max)
                core = AABB(low, high)
                margin = Vec3(halo, halo, halo)
                area = intersect(AABB(low - margin, high + margin), bounds)
                assert area is not None
                try:
                    volume = RegionVolume(area)
                except MemoryError as exc:
                    raise BlueprintError("領域配列を保持するメモリを確保できません") from exc
                if blueprint.terrain is not None:
                    snapshot = blueprint.terrain.snapshot
                    if isinstance(snapshot, RegionTerrainSnapshot):
                        lo, hi = snapshot.to_local(area.min), snapshot.to_local(area.max)
                        for row_y in range(lo.y, hi.y + 1):
                            for row_z in range(lo.z, hi.z + 1):
                                row = snapshot.row(row_y, row_z)[lo.x:hi.x + 1]
                                start = Vec3(area.min.x, row_y + snapshot.offset.y,
                                             row_z + snapshot.offset.z)
                                volume.set_box(AABB(start, start.with_axis("x", area.max.x)),
                                               snapshot.palette, row.reshape(1, 1, -1),
                                               EMPTY_ATTRIBUTES)
                    else:
                        for pos in iter_box(area):
                            volume.set(pos, snapshot.get(snapshot.to_local(pos)))
                for plan in (blueprint.foundation, blueprint.surface_path):
                    if plan is not None:
                        writes = (plan.writes.iter_region(area)
                                  if isinstance(plan.writes, ColumnWrites) else plan.writes)
                        for pos, state in writes:
                            if area.contains(pos):
                                volume.set(pos, state)
                ctx = ExecutionContext(
                    volume, random.Random(0), blueprint.palettes,
                    seed=blueprint.seed if seed is None else seed,
                    sampler=sampler,
                )
                for operation in blueprint.operations:
                    ctx.execute(operation)
                if blueprint.surface_path is not None:
                    check_surface_path(volume, blueprint.surface_path, core=core)
                    for pos, top in blueprint.surface_path.head_tops:
                        for head_y in range(pos.y - 1, top + 1):
                            cell = pos.with_axis("y", head_y)
                            if area.contains(cell):
                                attributes = volume.attributes(cell)
                                if attributes.reserved is None:
                                    volume.set(cell, volume.get(cell), replace(
                                        attributes, reserved="circulation", owner="surfacePath",
                                    ))
                yield GeneratedRegion(core, volume, bounds)
                del ctx, volume


def generate(blueprint: Blueprint, *, seed: int | None = None) -> BlockVolume:
    """元地形の施工範囲（指定時）へOperationを順に適用し、新しいvolumeを返す。

    seedはBlueprintのseedを上書きする。snapshotは変更・再読込せず、呼出すたびに
    元状態から生成する。施工セル総数の上限超過・範囲外配置はBlueprintErrorになる。
    """
    return generate_with_report(blueprint, seed=seed)[0]


def generate_with_report(
    blueprint: Blueprint, *, seed: int | None = None,
) -> tuple[BlockVolume, FoundationResult | None]:
    """generateと同じ生成に、造成だけの集計（指定なしはNone）を添えて返す。

    検査済みの全造成writesを通常Operationより前に適用する。
    改変数は元snapshotとの差であり、後続Operationの変更は含めない。
    """
    effective_seed = blueprint.seed if seed is None else seed
    terrain = blueprint.terrain
    if terrain is not None and terrain.region.volume > MAX_VOLUME:
        raise BlueprintError(f"施工範囲が {MAX_VOLUME:,} セルを超えています（airを含む）")
    volume = BlockVolume(write_bounds=terrain.region if terrain is not None else None)
    if terrain is not None:
        for pos in iter_box(terrain.region):
            state = terrain.snapshot.get(terrain.snapshot.to_local(pos))
            assert state is not None
            volume.set(pos, state)
    if blueprint.foundation is not None:
        for pos, state in blueprint.foundation.writes:
            volume.set(pos, state)
    if blueprint.surface_path is not None:
        for pos, state in blueprint.surface_path.writes:
            volume.set(pos, state)
    ctx = ExecutionContext(volume, random.Random(effective_seed), blueprint.palettes)
    for operation in blueprint.operations:
        ctx.execute(operation)
    if blueprint.surface_path is not None:
        check_surface_path(volume, blueprint.surface_path)
    return volume, blueprint.foundation.result if blueprint.foundation is not None else None
