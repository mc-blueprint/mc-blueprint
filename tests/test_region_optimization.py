"""領域高速化で距離・境界・経路・合計予算を変えないことを確認する。"""

import hashlib

import numpy as np
import pytest

from mcblueprint.blockdata import load_block_data
from mcblueprint.config import DEFAULTS
from mcblueprint.critic import CriticAccumulator, CriticPlan, StructuralRun, evaluate
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.facade_projection import Facade, FacadeCell
from mcblueprint.generator import GeneratedRegion
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.model.palette import Palette, PaletteEntry
from mcblueprint.operations.box_like import iter_box
from mcblueprint.operations.randomness import PaletteSampler
from mcblueprint.pipeline import _SpoolWindow, _working_budget, generated_blueprint_regions
from mcblueprint.traversal import check_route, check_routes
from mcblueprint.volume import BlockVolume, CellAttributes, RegionVolume
from mcblueprint.weather import _NEIGHBORS, _water_distances, _weather_fields


@pytest.mark.parametrize("radius", [0, 1, 4, 254, 255, 256, 1024])
def test_array_distance_is_truncated_l1_with_multiple_sources(radius):
    bounds = AABB(Vec3(-4, -2, -3), Vec3(260, 3, 4))
    volume = RegionVolume(bounds)
    sources = (bounds.min, Vec3(3, 0, 2))
    for pos in sources:
        volume.set(pos, BlockState.parse("water"))
    distances = _water_distances(volume, radius)
    for pos in iter_box(bounds):
        expected = min(sum(abs(a - b) for a, b in zip(pos, p, strict=True)) for p in sources)
        actual = int(distances[volume._offset(pos)])
        assert actual == expected if expected <= radius else actual > radius


def test_no_water_has_no_distance_even_when_radius_exceeds_volume():
    volume = RegionVolume(AABB(Vec3(-1, 0, 0), Vec3(2, 1, 2)))
    assert _water_distances(volume, 1024) is None


@pytest.mark.parametrize("margin", [0, 3])
def test_array_exposure_preserves_unknown_halo_and_world_boundary(margin):
    bounds = AABB(Vec3(-2, -1, -3), Vec3(2, 2, 1))
    world = AABB(bounds.min - Vec3(margin, margin, margin),
                 bounds.max + Vec3(margin, margin, margin))
    volume = RegionVolume(bounds)
    volume.set_box(bounds, (BlockState.parse("stone_bricks"),), None, CellAttributes())
    volume.set(Vec3(-1, 0, -2), BlockState.parse("cave_air"))
    volume.set(Vec3(1, 1, 0), BlockState.parse("water"))
    _, exposure = _weather_fields(volume, world, moss=1, cracks=1, missing=1,
                                  vegetation=1, block_data=load_block_data("1.21.11"))
    for pos in iter_box(bounds):
        expected = 0
        for bit, (offset, _) in enumerate(_NEIGHBORS):
            neighbor = pos + offset
            state = volume.get(neighbor)
            if not world.contains(neighbor) or bounds.contains(neighbor) and (
                state is None or state.id in {"minecraft:air", "minecraft:cave_air",
                                               "minecraft:void_air"}
            ):
                expected |= 1 << bit
        assert int(exposure[volume._offset(pos)]) == expected


def test_sampler_only_draws_requested_cells_and_preserves_overlapping_values(monkeypatch):
    bounds = AABB(Vec3(-33, -2, -4), Vec3(33, 2, 4))
    sampler = PaletteSampler(bounds, 1024 * 1024)
    palette = Palette("wall", (PaletteEntry(BlockState.parse("stone")),
                               PaletteEntry(BlockState.parse("bricks"))))
    prefix = hashlib.sha256(b"region-test")
    sampled = []
    sample = sampler._sample

    def record(box, *args):
        sampled.append(box)
        return sample(box, *args)

    monkeypatch.setattr(sampler, "_sample", record)
    request = AABB(Vec3(-32, 0, -1), Vec3(32, 0, 1))
    values = {}
    for tile, choices in sampler.tiles(request, prefix, palette):
        for pos in iter_box(tile):
            delta = pos - tile.min
            values[pos] = int(choices[delta.y, delta.z, delta.x])
    assert sum(box.volume for box in sampled) == request.volume
    for tile, choices in sampler.tiles(bounds, prefix, palette):
        for pos in values.keys():
            if tile.contains(pos):
                delta = pos - tile.min
                assert choices[delta.y, delta.z, delta.x] == values[pos]
    assert sampler._cache_bytes == sum(value.nbytes + 1024 for value in sampler._cache.values())
    assert sampler._cache_bytes <= sampler.cache_limit


def test_critic_tiles_preserve_negative_unaligned_cores_and_duplicate_reverse_consumption():
    bounds = AABB(Vec3(-35, -18, -2), Vec3(35, 18, -2))
    volume = RegionVolume(bounds)
    attr = CellAttributes(role="wall_main", element="wall", reserved="structure", owner="frame")
    volume.set_box(bounds, (BlockState.parse("stone_bricks"),), None, attr)
    cells = tuple(FacadeCell(pos, pos.x + 35, pos.y + 18, 2) for pos in iter_box(bounds))
    plan = CriticPlan((StructuralRun("frame", tuple(iter_box(bounds)), None),), (),
                      (Facade("wall", "north", cells),), {}, (0, 1))
    expected = evaluate(plan, [GeneratedRegion(bounds, volume)])
    cores = []
    for x in range(bounds.min.x, bounds.max.x + 1, 13):
        for y in range(bounds.min.y, bounds.max.y + 1, 7):
            cores.append(AABB(Vec3(x, y, -2), Vec3(min(x + 12, 35), min(y + 6, 18), -2)))
    accumulator = CriticAccumulator(plan)
    for core in (*reversed(cores), *cores):
        accumulator.consume(GeneratedRegion(core, volume))
    assert accumulator.finalize() == expected


@pytest.mark.parametrize("failure", [None, "low-ceiling", "unsupported", "no-floor"])
def test_shared_walker_preserves_full_paths_and_diagnostic_order(failure):
    volume = BlockVolume()
    for x in range(9):
        volume.set(Vec3(x, 0, 0), BlockState.parse("stone"))
    if failure == "low-ceiling":
        volume.set(Vec3(4, 2, 0), BlockState.parse("stone"))
    elif failure == "unsupported":
        volume.set(Vec3(4, 1, 0), BlockState.parse("oak_slab"))
    elif failure == "no-floor":
        volume.set(Vec3(4, 0, 0), BlockState.parse("air"))
    pairs = [(Vec3(0, 1, 0), Vec3(8, 1, 0)), (Vec3(8, 1, 0), Vec3(0, 1, 0)),
             (Vec3(1, 1, 0), Vec3(7, 1, 0))]
    expected = tuple(check_route(volume, a, b, "1.21.11") for a, b in pairs)
    assert tuple(check_routes(volume, pairs, "1.21.11")) == expected
    with pytest.raises(BlueprintError, match="max_memory_mb"):
        tuple(check_routes(volume, pairs, "1.21.11", max_nodes=1))


def test_total_budget_counts_shared_arrays_once_and_rejects_retained_input():
    array = np.zeros(1024 * 1024, dtype=np.uint8)
    available = _working_budget(128, array)
    assert available == _working_budget(128, array, array)
    assert _working_budget(128, array, array.copy()) < available - array.nbytes
    with pytest.raises(BlueprintError, match="max_memory_mb"):
        _working_budget(64, array)


@pytest.mark.parametrize("allocated", [float("nan"), float("inf")])
def test_nonfinite_allocation_cannot_be_masked_by_saved_limit(allocated):
    blueprint = load_blueprint_dict({
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "limits",
        "operations": [{"type": "fill", "from": [0, 0, 0], "to": [0, 0, 0], "block": "stone"}],
        "metadata": {"generation": {"mode": "buildspec-v1", "config": DEFAULTS, "groundHeight": 0}},
    })
    with pytest.raises(BlueprintError, match="max_memory_mb"):
        next(generated_blueprint_regions(blueprint, max_memory_mb=allocated))


def test_spool_rows_preserve_air_unset_cells_order_and_clipped_core(tmp_path):
    bounds = AABB(Vec3(-4, -1, -2), Vec3(4, 2, 3))
    volume = RegionVolume(bounds)
    for i, pos in enumerate(iter_box(bounds)):
        if i % 3:
            volume.set(pos, BlockState.parse("air" if i % 5 else "stone"))
    spool = RegionSpool(tmp_path / "cells", bounds)
    spool.write([GeneratedRegion(bounds, volume)])
    with spool.path.open("rb") as stream:
        window = _SpoolWindow(spool, stream, 2)
        for core in (bounds, AABB(Vec3(-8, -4, -4), Vec3(0, 1, 0)),
                     AABB(Vec3(1, 0, -2), Vec3(10, 2, 4)),
                     AABB(Vec3(8, 0, 0), Vec3(10, 1, 2))):
            window.core = core
            expected = [(pos, state) for pos in iter_box(core)
                        if (state := volume.get(pos)) is not None]
            assert list(window) == expected
            assert all(window.get(pos) == state for pos, state in expected)
