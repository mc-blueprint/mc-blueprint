"""地形場と固定版ノイズの領域分割契約。"""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from pyfastnoiselite.pyfastnoiselite import FastNoiseLite

from mcblueprint.blockdata import load_block_data
from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, Vec3
from mcblueprint.terrain_field import RELIEF, TerrainField, plan_terrain, sample_noise

BOUNDS = AABB(Vec3(-17, -4, 9), Vec3(63, 91, 81))


@pytest.mark.parametrize("dimension", [2, 3])
def test_noise_partition_and_restart(dimension):
    coords = np.array([
        [i * (axis + 1.25) - 30 for i in range(97)] for axis in range(dimension)
    ], dtype=np.float32)
    noise = FastNoiseLite(seed=42)
    whole = noise.gen_from_coords(coords)
    split = np.concatenate([
        noise.gen_from_coords(coords[:, :31]), noise.gen_from_coords(coords[:, 31:])
    ])
    np.testing.assert_array_equal(whole, split)
    np.testing.assert_array_equal(
        whole, noise.gen_from_coords(coords[:, ::-1].copy())[::-1]
    )
    np.testing.assert_array_equal(whole, FastNoiseLite(seed=42).gen_from_coords(coords))


@pytest.mark.parametrize("dimension", [2, 3])
@pytest.mark.parametrize("persistence", [0, 0.5, 1])
def test_octaves_partition(dimension, persistence):
    coords = np.arange(dimension * 301).reshape(dimension, -1) - 203
    options = {"wavelength": 32, "octaves": 5, "persistence": persistence}
    whole = sample_noise(coords, 42, **options)
    split = np.concatenate([sample_noise(c, 42, **options)
                            for c in np.array_split(coords, 3, axis=1)])
    np.testing.assert_array_equal(whole, split)
    np.testing.assert_array_equal(whole, sample_noise(coords[:, ::-1], 42, **options)[::-1])
    assert np.max(abs(whole)) <= 1
    assert not np.array_equal(whole, sample_noise(coords, 73, **options))


@pytest.mark.parametrize("kind", RELIEF)
@pytest.mark.parametrize("amplitude", [0, 1])
@pytest.mark.parametrize("roughness", [0, 1])
def test_type_endpoints_and_bounds(kind, amplitude, roughness):
    field = plan_terrain(BOUNDS, kind, {"amplitude": amplitude, "roughness": roughness,
                                      "erosion": 0}, 42)
    assert field.heights.ndim == field.coarse_delta.ndim == 2
    assert field.coarse_delta.shape == (65, 65)
    assert field.heights.min() >= BOUNDS.min.y
    assert field.heights.max() <= BOUNDS.max.y
    if amplitude == 0 or kind == "flat":
        assert np.ptp(field.heights) == 0
    else:
        assert np.ptp(field.heights) > 0
    if kind == "island" and amplitude:
        assert np.max(field.heights[[0, -1], :]) <= field.water_level
        assert np.max(field.heights[:, [0, -1]]) <= field.water_level


@pytest.mark.parametrize("erosion", [0.35, 1])
@pytest.mark.parametrize("origin", [Vec3(0, 0, 0), Vec3(-8, -20, 4)])
@pytest.mark.parametrize("water", [None, 0])
def test_eroded_island_edges_stay_below_water(erosion, origin, water):
    bounds = AABB(origin, origin + Vec3(16, 255, 16))
    settings = {"amplitude": 1, "erosion": erosion, "water_level": water}
    field = plan_terrain(bounds, "island", settings, 1)
    assert np.any(field.coarse_delta != 0)
    assert field.heights[[0, -1], :].max() <= field.water_level
    assert field.heights[:, [0, -1]].max() <= field.water_level
    again = plan_terrain(bounds, "island", settings, 1)
    assert field.to_dict()["sha256"] == again.to_dict()["sha256"]
    restored = TerrainField.from_dict(json.loads(json.dumps(field.to_dict())))
    regions = [AABB(origin, origin + Vec3(7, 255, 16)),
               AABB(origin + Vec3(8, 0, 0), bounds.max)]
    tiles = [restored.sample(region) for region in reversed(regions)]
    np.testing.assert_array_equal(np.concatenate(tiles[::-1], axis=1), field.heights)


def test_erosion_restart_hash_and_coarse_boundaries():
    original = plan_terrain(BOUNDS, "mountain", {"erosion": 0}, 42)
    eroded = plan_terrain(BOUNDS, "mountain", {"erosion": 1}, 42)
    assert np.count_nonzero(eroded.coarse_delta) > 10
    assert not np.array_equal(original.heights, eroded.heights)
    again = plan_terrain(BOUNDS, "mountain", {"erosion": 1}, 42)
    assert eroded.to_dict()["sha256"] == again.to_dict()["sha256"]
    restored = TerrainField.from_dict(json.loads(json.dumps(eroded.to_dict())))
    assert eroded.to_dict() == restored.to_dict()
    for field in (eroded, restored):
        assembled = np.empty_like(field.heights)
        for z in reversed(range(BOUNDS.min.z, BOUNDS.max.z + 1, 11)):
            for x in reversed(range(BOUNDS.min.x, BOUNDS.max.x + 1, 13)):
                box = AABB(Vec3(x, 0, z), Vec3(min(x + 12, BOUNDS.max.x), 0,
                                             min(z + 10, BOUNDS.max.z)))
                tile = field.sample(box)
                assembled[z - BOUNDS.min.z:z - BOUNDS.min.z + tile.shape[0],
                          x - BOUNDS.min.x:x - BOUNDS.min.x + tile.shape[1]] = tile
        np.testing.assert_array_equal(assembled, eroded.heights)
    broken = eroded.to_dict()
    broken["heights"][0][0] += 1
    with pytest.raises(BlueprintError, match="hash"):
        TerrainField.from_dict(broken)


@pytest.mark.parametrize("setting,values", [
    ("roughness", [0, 1]), ("amplitude", [0, 1]),
    ("noise", [{"scale": 16}, {"scale": 128}]),
    ("noise", [{"octaves": 1}, {"octaves": 6}]),
    ("noise", [{"persistence": 0}, {"persistence": 1}]),
])
def test_settings_have_effect(setting, values):
    fields = [plan_terrain(BOUNDS, "hill", {setting: v, "erosion": 0}, 42) for v in values]
    assert not np.array_equal(fields[0].heights, fields[1].heights)


def test_seed_plateau_and_default_water():
    options = {"erosion": 0, "amplitude": 1}
    first = plan_terrain(BOUNDS, "mountain", options, 42)
    second = plan_terrain(BOUNDS, "mountain", options, 73)
    assert not np.array_equal(first.heights, second.heights)
    plateau = plan_terrain(BOUNDS, "plateau", options, 42)
    assert np.ptp(plateau.heights[28:44, 31:48]) == 0
    with pytest.raises(ValueError):
        first.sample(BOUNDS)[0, 0] = 0
    for kind, ratio in (("island", 0.25), ("hill", 0.15)):
        field = plan_terrain(BOUNDS, kind, options, 42)
        assert field.water_level == BOUNDS.min.y + int((BOUNDS.size.y - 1) * ratio + 0.5)
    data = load_block_data("1.21.11")
    broad = plan_terrain(AABB(Vec3(0, 0, 0), Vec3(200, 95, 200)), "mountain",
                         {**options, "noise": {"scale": 512}}, 42)
    z, x = np.unravel_index(np.argmax(broad.heights), broad.heights.shape)
    assert broad.column(int(x), int(z), data).surface == "minecraft:snow_block"


@pytest.mark.parametrize("water", [-5, 92, 0.5, "20", True])
def test_invalid_water(water):
    with pytest.raises(BlueprintError):
        plan_terrain(BOUNDS, "hill", {"water_level": water}, 42)


def test_clearance_and_degenerate_footprint():
    with pytest.raises(BlueprintError, match="地形帯"):
        plan_terrain(BOUNDS, "hill", {}, 42, clearance=95)
    with pytest.raises(BlueprintError, match="地形帯外"):
        plan_terrain(BOUNDS, "hill", {"water_level": 90}, 42, clearance=10)
    for size in ((1, 2, 1), (1, 16, 15), (16, 16, 1)):
        bounds = AABB(Vec3(0, 0, 0), Vec3(*(s - 1 for s in size)))
        field = plan_terrain(bounds, "hill", {}, 42)
        assert field.heights.shape == (size[2], size[0])
    field = plan_terrain(BOUNDS, "hill", {}, 42, clearance=20)
    assert field.heights.max() <= BOUNDS.max.y - 20
    with pytest.raises(BlueprintError, match="範囲外"):
        field.sample(AABB(Vec3(-18, 0, 9), Vec3(-17, 0, 9)))


def test_fixed_h01_conditions():
    cases = json.loads((Path(__file__).parents[1] / "evaluation/cases/large-scale.json")
                       .read_text(encoding="utf-8"))
    for case in cases["cases"]:
        spec = case["buildSpec"]
        if spec["archetype"] != "terrain":
            continue
        for size in cases["scales"].values():
            bounds = AABB(Vec3(0, 0, 0), Vec3(*(s - 1 for s in size)))
            for seed in cases["seeds"]:
                field = plan_terrain(bounds, spec["site"]["terrain"],
                                     spec["config"]["terrain"], seed)
                assert field.heights.shape == (size[2], size[0])
                assert field.heights.max() < size[1]
                if spec["config"]["terrain"]["amplitude"] == 0:
                    assert np.ptp(field.heights) == 0


def test_surface_water_and_site_vegetation():
    data = load_block_data("1.21.11")
    settings = {"erosion": 0, "vegetation": 1, "water_level": 12}
    field = plan_terrain(BOUNDS, "mountain", settings, 42)
    columns = [field.column(x, z, data)
               for z in range(BOUNDS.min.z, BOUNDS.max.z + 1)
               for x in range(BOUNDS.min.x, BOUNDS.max.x + 1)]
    assert {c.surface for c in columns} >= {"minecraft:sand", "minecraft:grass_block",
                                          "minecraft:stone"}
    assert {c.vegetation for c in columns} == {None, "grass", "tree"}
    for column in columns:
        if column.water is not None:
            assert column.top < column.water
            assert column.vegetation is None
    settings["vegetation"] = 0
    empty = plan_terrain(BOUNDS, "mountain", settings, 42)
    assert all(empty.column(x, 45, data).vegetation is None for x in range(-17, 64))
    restored = TerrainField.from_dict(field.to_dict())
    assert field.column(10, 45, data) == restored.column(10, 45, data)
    assert copy.deepcopy(field.to_dict()) == field.to_dict()


def test_signed_noise_creates_pools_below_default_water():
    field = plan_terrain(BOUNDS, "mountain", {"amplitude": 1, "erosion": 0}, 42)
    assert np.any(field.heights < field.water_level)
    data = load_block_data("1.21.11")
    z, x = np.argwhere(field.heights < field.water_level)[0]
    column = field.column(int(x) + BOUNDS.min.x, int(z) + BOUNDS.min.z, data)
    assert column.water == field.water_level
