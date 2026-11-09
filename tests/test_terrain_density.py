"""局所密度の形状保証・分割・Blueprint再生契約。"""

import copy
import json
import weakref
from collections import deque
from dataclasses import replace

import numpy as np
import pytest

from mcblueprint.cli import main
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GenerationLimits, generate, generate_regions
from mcblueprint.importers import read_terrain
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.terrain_density import CaveNode, TerrainDensity, check_density_region, plan_density
from mcblueprint.terrain_field import plan_terrain
from mcblueprint.validator import validate
from mcblueprint.volume import CellAttributes, RegionVolume

BOUNDS = AABB(Vec3(-7, -3, 9), Vec3(33, 36, 45))


def field(**settings):
    return plan_terrain(BOUNDS, "mountain", {"erosion": 0, "vegetation": 0, **settings}, 42)


def blueprint(plan):
    return {"formatVersion": 1, "minecraftVersion": "1.21.11", "name": "density",
            "operations": [{"type": "terrain_density", "minecraftVersion": "1.21.11",
                            "plan": plan.to_dict()}]}


@pytest.mark.parametrize("features", [{}, {"arches": True}, {"main_cave": True, "entrances": 4},
    {"main_cave": True, "chambers": (
        CaveNode("central", Vec3(13, 14, 27), Vec3(14, 12, 12)),
        CaveNode("auxiliary", Vec3(23, 14, 27), Vec3(7, 10, 9)))}])
@pytest.mark.parametrize("core", [16, 32, 64, 7])
def test_density_partition_reverse_restart(features, core):
    plan = plan_density(field(caves=0.7), **features)
    expected = plan.sample(BOUNDS)
    restored = TerrainDensity.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert restored.to_dict() == plan.to_dict()
    arrays = [np.empty_like(expected.density), np.empty_like(expected.cavity),
              np.empty_like(expected.support), np.empty_like(expected.opening)]
    for y in reversed(range(BOUNDS.min.y, BOUNDS.max.y + 1, core)):
        for z in reversed(range(BOUNDS.min.z, BOUNDS.max.z + 1, core)):
            for x in reversed(range(BOUNDS.min.x, BOUNDS.max.x + 1, core)):
                box = AABB(Vec3(x, y, z), Vec3(min(x + core - 1, BOUNDS.max.x),
                                             min(y + core - 1, BOUNDS.max.y),
                                             min(z + core - 1, BOUNDS.max.z)))
                tile = restored.sample(box)
                offset = box.min - BOUNDS.min
                slices = tuple(slice(offset[i], offset[i] + box.size[i]) for i in (1, 2, 0))
                for target, values in zip(arrays, (tile.density, tile.cavity, tile.support,
                                                   tile.opening),
                                          strict=True):
                    target[slices] = values
    for result, values in zip(arrays, (expected.density, expected.cavity, expected.support,
                                      expected.opening),
                              strict=True):
        np.testing.assert_array_equal(result, values)


def test_main_cave_zero_branches_graph_and_dry_default():
    plan = plan_density(field(caves=0), main_cave=True, entrances=4)
    tile = plan.sample(BOUNDS)
    assert tile.cavity.sum() > 100
    assert np.all(tile.density[tile.cavity] < 0)
    assert plan.water_level is None
    nodes = {n.id: n for n in plan.nodes}
    for a, b in plan.edges:
        first, second = nodes[a], nodes[b]
        delta = first.center - second.center
        assert max(abs(delta.x), abs(delta.z)) <= min(first.radius.x, first.radius.z)
    for node in nodes.values():
        point = AABB(node.center, node.center)
        assert plan.sample(point).cavity[0, 0, 0]
    # 上下左右の殻は主空洞のノイズ変位に潰されない。
    n = plan.nodes[0]
    for delta in (Vec3(0, n.radius.y + 1, 0), Vec3(0, -n.radius.y - 1, 0)):
        point = n.center + delta
        assert plan.sample(AABB(point, point)).density.item() >= 0
    assert all(value.ndim == 2 for value in (plan.field.heights, plan.field.coarse_delta))


def test_arch_opening_and_grounded_supports():
    plan = plan_density(field(caves=1), arches=True)
    a = plan.arch
    assert a is not None
    center = Vec3((a.min.x + a.max.x) // 2, a.min.y + 2, a.min.z)
    assert plan.sample(AABB(center, center)).density.item() < 0
    for x in (a.min.x, a.max.x):
        column = AABB(Vec3(x, a.min.y, a.min.z), Vec3(x, a.max.y, a.min.z))
        sample = plan.sample(column)
        assert np.all(sample.support)
        assert np.all(sample.density >= 0)
    roof = Vec3(center.x, a.max.y, center.z)
    assert plan.sample(AABB(roof, roof)).support.item()


def test_noise_displacement_branches_and_overhang():
    settings = {"amplitude": 1, "roughness": 1, "noise": {"scale": 16}}
    base = field(**settings)
    plain = plan_density(base, overhangs=False).sample(BOUNDS).density
    displaced = plan_density(base).sample(BOUNDS).density
    assert 0 < np.max(abs(plain - displaced)) <= (base.band_top - BOUNDS.min.y) * 0.12
    branched = plan_density(field(caves=1, **settings)).sample(BOUNDS).density
    assert np.any((displaced >= 0) & (branched < 0))
    # 下にair、その上に地盤がある列が実在する。
    assert np.any((displaced[1:] >= 0) & (displaced[:-1] < 0))


@pytest.mark.parametrize("core,reverse", [(16, False), (32, True), (64, False), (7, True)])
def test_operation_replay_regions_water_surface_and_trees(core, reverse):
    plan = plan_density(field(vegetation=1, water_level=4), overhangs=False)
    raw = json.loads(json.dumps(blueprint(plan)))
    assert not validate(raw)
    loaded = load_blueprint_dict(raw)
    expected = generate(loaded)
    count = 0
    for result in generate_regions(loaded, limits=GenerationLimits(region=(core, 9, core), halo=0),
                                   reverse=reverse):
        assert result.volume.region.size.x <= core
        assert result.volume.region.size.y <= 9
        for pos, state in result.volume:
            assert state == expected.get(pos)
            assert result.volume.attributes(pos) == expected.attributes(pos)
            if state.id == "minecraft:water":
                assert pos.y <= 4
            count += 1
    assert count == BOUNDS.volume
    states = {str(state) for _, state in expected}
    assert {"minecraft:water", "minecraft:grass_block", "minecraft:dirt"} <= states
    assert "minecraft:oak_log[axis=y]" in states
    assert "minecraft:oak_leaves[persistent=true]" in states


def test_required_shape_attributes_and_reservations():
    raw = blueprint(plan_density(field(caves=0), main_cave=True))
    volume = generate(load_blueprint_dict(raw))
    node = raw["operations"][0]["plan"]["caveGraph"]["nodes"][0]
    p = Vec3.from_seq(node["center"])
    assert volume.get(p) == BlockState.parse("air")
    assert volume.attributes(p).role == "terrain.cavity"
    assert volume.attributes(p).reserved is None
    raw["operations"].insert(0, {"type": "set", "position": p.to_list(), "block": "stone",
                                  "reserved": "structure", "owner": "building#0"})
    with pytest.raises(BlueprintError, match="予約セル"):
        generate(load_blueprint_dict(raw))


def test_main_cave_explicit_water_and_version_mismatch():
    plan = plan_density(field(water_level=10), main_cave=True)
    raw = blueprint(plan)
    volume = generate(load_blueprint_dict(raw))
    assert volume.get(plan.nodes[0].center).id == "minecraft:water"
    assert not check_density_region(plan, volume, BOUNDS)
    raw["operations"][0]["minecraftVersion"] = "1.21.4"
    assert any("版が不一致" in error.message for error in validate(raw))


def test_invalid_plan_shape_bounds_and_budget():
    plan = plan_density(field(), arches=True)
    broken = copy.deepcopy(plan.to_dict())
    broken["arch"]["to"][1] -= 1
    with pytest.raises(BlueprintError, match="hash|形状"):
        TerrainDensity.from_dict(broken)
    broken = copy.deepcopy(plan.to_dict())
    broken["field"].pop("sha256")
    raw = blueprint(plan)
    raw["operations"][0]["plan"] = broken
    assert validate(raw)
    small = plan_terrain(AABB(Vec3(0, 0, 0), Vec3(8, 8, 8)), "flat", {}, 42)
    with pytest.raises(BlueprintError, match="必須空洞"):
        plan_density(small, main_cave=True)
    with pytest.raises(BlueprintError, match="入口数"):
        plan_density(field(), main_cave=True, entrances=5)
    with pytest.raises(BlueprintError, match="範囲外"):
        plan.sample(AABB(BOUNDS.min - Vec3(1, 0, 0), BOUNDS.max))
    loaded = load_blueprint_dict(blueprint(plan))
    with pytest.raises(BlueprintError, match="収まりません"):
        next(generate_regions(loaded, limits=GenerationLimits(max_memory_mb=1)))


@pytest.mark.parametrize("node", [
    CaveNode(7, Vec3(23, 14, 27), Vec3(7, 10, 9)),
    CaveNode("central", Vec3(23, 14, 27), Vec3(7, 10, 9)),
    CaveNode("cave.entrance#0", Vec3(23, 14, 27), Vec3(7, 10, 9)),
    CaveNode("auxiliary", Vec3(23, 15, 27), Vec3(7, 10, 9)),
    CaveNode("auxiliary", Vec3(23, 14, 27), Vec3(9, 10, 9)),
    CaveNode("auxiliary", Vec3(23, 14, 27), Vec3(0, 10, 9)),
    CaveNode("auxiliary", Vec3(28, 14, 40), Vec3(2, 10, 2)),
])
def test_chambers_reject_invalid_geometry(node):
    central = CaveNode("central", Vec3(13, 14, 27), Vec3(14, 12, 12))
    with pytest.raises(BlueprintError, match="chambers"):
        plan_density(field(), main_cave=True, chambers=(central, node))


def test_chambers_roundtrip_tampering_and_old_version():
    chambers = (CaveNode("central", Vec3(13, 14, 27), Vec3(14, 12, 12)),
                CaveNode("auxiliary", Vec3(23, 14, 27), Vec3(7, 10, 9)))
    plan = plan_density(field(), main_cave=True, chambers=chambers)
    assert plan.chambers == chambers
    assert ("central", "auxiliary") in plan.edges
    assert not validate(blueprint(plan))
    for key in ("chambers", "caveGraph", "sha256", "formatVersion"):
        data = plan.to_dict()
        if key == "chambers":
            data[key][1]["radius"][0] -= 1
        elif key == "caveGraph":
            data[key]["edges"].pop()
        elif key == "sha256":
            data[key] = "0" * 64
        else:
            data[key] = 1
        with pytest.raises(BlueprintError, match="再生成"):
            TerrainDensity.from_dict(data)
    with pytest.raises(BlueprintError, match="主空洞"):
        plan_density(field(), chambers=chambers)


@pytest.mark.parametrize("features", [{"main_cave": True}, {"arches": True}])
def test_completed_shapes_and_negative_cases(features):
    plan = plan_density(field(caves=1), **features)
    volume = generate(load_blueprint_dict(blueprint(plan)))
    assert not check_density_region(plan, volume, BOUNDS)
    masks = plan.sample(BOUNDS)
    if plan.main_cave:
        point = plan.nodes[0].center
        volume.set(point, BlockState.parse("stone"))
        assert "主空洞が閉塞しています" in check_density_region(plan, volume, BOUNDS)
    else:
        iy, iz, ix = np.argwhere(masks.opening)[0]
        point = BOUNDS.min + Vec3(int(ix), int(iy), int(iz))
        volume.set(point, BlockState.parse("stone"))
        assert "アーチの開口が閉塞しています" in check_density_region(plan, volume, BOUNDS)
    iy, iz, ix = np.argwhere(masks.support)[0]
    point = BOUNDS.min + Vec3(int(ix), int(iy), int(iz))
    volume.set(point, BlockState.parse("air"))
    assert "地形支持部が欠落しています" in check_density_region(plan, volume, BOUNDS)


@pytest.mark.parametrize("overhangs", [False, True])
def test_arch_opening_connects_to_exterior(overhangs):
    plan = plan_density(field(caves=0), arches=True, overhangs=overhangs)
    volume = generate(load_blueprint_dict(blueprint(plan)))
    sample = plan.sample(BOUNDS)
    passable = np.zeros(sample.density.shape, dtype=bool)
    for pos, state in volume:
        offset = pos - BOUNDS.min
        passable[offset.y, offset.z, offset.x] = state.id in {
            "minecraft:air", "minecraft:water"}
    reached = np.zeros_like(passable)
    boundary = np.zeros_like(passable)
    boundary[:, (0, -1), :] = True
    queue = deque(map(tuple, np.argwhere(passable & boundary)))
    for point in queue:
        reached[point] = True
    while queue:
        point = queue.popleft()
        for axis in range(3):
            for step in (-1, 1):
                neighbor = list(point)
                neighbor[axis] += step
                if not 0 <= neighbor[axis] < passable.shape[axis]:
                    continue
                neighbor = tuple(neighbor)
                if passable[neighbor] and not reached[neighbor]:
                    reached[neighbor] = True
                    queue.append(neighbor)
    assert sample.opening.any()
    assert np.all(reached[sample.opening])
    assert not check_density_region(plan, volume, BOUNDS)
    a = plan.arch
    for z in (BOUNDS.min.z, (BOUNDS.min.z + a.min.z) // 2, a.min.z, BOUNDS.max.z):
        point = Vec3((a.min.x + a.max.x) // 2, a.min.y + 8, z)
        assert volume.attributes(point).role == "terrain.opening"
        volume.set(point, BlockState.parse("stone"))
        assert "アーチの開口が閉塞しています" in check_density_region(plan, volume, BOUNDS)
        volume.set(point, BlockState.parse("air"))


@pytest.mark.parametrize("features", [{"main_cave": True}, {"arches": True}])
def test_region_required_shapes_with_halo(features):
    plan = plan_density(field(caves=1), **features)
    loaded = load_blueprint_dict(blueprint(plan))
    for result in generate_regions(loaded, limits=GenerationLimits(region=(7, 8, 9), halo=2),
                                   reverse=True):
        assert not check_density_region(plan, result.volume, result.core)


def test_build_blueprint_and_export_roundtrip(tmp_path):
    plan = plan_density(field(caves=0), main_cave=True)
    path = tmp_path / "density.json"
    path.write_text(json.dumps(blueprint(plan)), encoding="utf-8")
    out = tmp_path / "density.schem"
    assert main(["build", str(path), "-o", str(out)]) == 0
    restored = read_terrain(out)
    for node in plan.nodes:
        local = restored.to_local(node.center)
        assert restored.get(local).id == "minecraft:air"


def test_bounded_scratch_and_region_lifetime(monkeypatch):
    plan = plan_density(field(), main_cave=True)
    original = TerrainDensity.sample
    largest = 0

    def sample(self, region):
        nonlocal largest
        largest = max(largest, region.volume)
        return original(self, region)

    monkeypatch.setattr(TerrainDensity, "sample", sample)
    loaded = load_blueprint_dict(blueprint(plan))
    stream = generate_regions(loaded, limits=GenerationLimits(region=(64, 12, 64), halo=0))
    previous = None
    for result in stream:
        if previous is not None:
            assert previous() is None
        previous = weakref.ref(result.volume)
        del result
    assert largest <= 32 * 32 * 16
    assert previous() is None


def test_masked_array_write_preserves_unselected_reservations():
    box = AABB(Vec3(0, 0, 0), Vec3(2, 0, 0))
    volume = RegionVolume(box)
    reserved = CellAttributes(reserved="structure", owner="building#0")
    ground = CellAttributes(role="terrain.ground")
    stone, dirt = BlockState.parse("stone"), BlockState.parse("dirt")
    volume.set(box.min, stone, reserved)
    mask = np.array([[[False, True, True]]])
    volume.set_box(box, (dirt,), None, ground, mask=mask)
    assert volume.get(box.min) == stone
    assert volume.attributes(box.min) == reserved
    assert volume.get(box.max) == dirt
    mask[0, 0, 0] = True
    with pytest.raises(BlueprintError, match="予約セル"):
        volume.set_box(box, (dirt,), None, ground, mask=mask)
    volume.set_box(box, (dirt,), None, ground, mask=mask, on_conflict="skip")
    assert volume.attributes(box.min) == reserved
    assert volume.get(box.min) == stone


def test_tree_canopy_does_not_replace_neighboring_cliff():
    base = field(vegetation=1)
    heights = np.full_like(base.heights, 4)
    heights[12 - BOUNDS.min.z, 14 - BOUNDS.min.x] = 9
    heights.flags.writeable = False
    plan = plan_density(replace(base, heights=heights), overhangs=False)
    volume = generate(load_blueprint_dict(blueprint(plan)))
    assert volume.get(Vec3(12, 5, 12)).id == "minecraft:oak_log"
    assert volume.get(Vec3(14, 8, 12)).id == "minecraft:dirt"
    assert volume.get(Vec3(14, 9, 12)).id == "minecraft:stone"
