"""地下の主空洞・床・構造予約・到達経路を計画と完成セルで照合する。"""

import copy
import gzip
import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.critic import evaluate, plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion, GenerationLimits, generate, generate_regions
from mcblueprint.materialize import materialize_plan
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.pipeline import STAGES, make
from mcblueprint.plan_contract import plan_buildings
from mcblueprint.planning import plan_buildspec
from mcblueprint.terrain_density import TerrainDensity
from mcblueprint.terrain_field import TerrainField
from mcblueprint.traversal import check_route
from mcblueprint.underground_planning import cave_surfaces
from mcblueprint.validator import validate_and_load
from mcblueprint.volume import CellAttributes, RegionVolume


def underground_spec(archetype="underground_city", **changes):
    cases = json.loads(Path("evaluation/cases/large-scale.json").read_bytes())["cases"]
    raw = next(c["buildSpec"] for c in cases if c["buildSpec"]["archetype"] == archetype)
    return {**raw, "scale": "huge" if archetype == "underground_city" else "medium",
            "seed": 42, **changes}


def concrete_plan(tmp_path, **changes):
    result = plan_buildspec(resolve_buildspec(underground_spec(**changes), project_dir=tmp_path))
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    bp, errors = validate_and_load(concrete.blueprint)
    assert not errors, errors
    return result, bp


@pytest.mark.parametrize("seed", (42, 73, 101))
@pytest.mark.parametrize("archetype", ("cave", "underground_city"))
def test_underground_reproducible_surfaces_and_reservations(tmp_path, seed, archetype):
    raw = underground_spec(archetype, seed=seed)
    if archetype == "cave" and seed == 101:
        raw["features"] = {"entrances": 4}
    resolved = resolve_buildspec(raw, project_dir=tmp_path)
    before = copy.deepcopy(resolved.buildspec)
    result = plan_buildspec(resolved)
    assert result == plan_buildspec(resolved)
    assert resolved.buildspec == before
    assert len(plan_buildings(result.massing)) == (8 if archetype == "underground_city" else 0)
    density = TerrainDensity.from_dict(result.site["density"])
    bottom, ceiling = cave_surfaces(density)
    ground = TerrainField.from_dict(result.site["groundHeight"]["field"])
    assert not np.array_equal(ground.heights, density.field.heights)
    for z, x in ((density.nodes[0].center.z, density.nodes[0].center.x), (10, 10)):
        tile = density.sample(AABB(Vec3(x, 0, z), Vec3(x, resolved.buildspec["size"][1] - 1, z)))
        levels = np.flatnonzero(tile.cavity[:, 0, 0])
        if len(levels):
            assert (bottom[z, x], ceiling[z, x]) == (levels[0], levels[-1])
    for parcel in result.site["parcels"]:
        (x, z), (east, south) = parcel["bounds"]
        assert np.all(ground.heights[z:south + 1, x:east + 1] == result.site["floorY"])
    assert result.site["supports"]
    assert all(op["reserved"] == "structure" for op in result.site["supports"])
    for support in result.site["supports"]:
        low = Vec3.from_seq(support["from"]) - Vec3(0, 1, 0)
        high = Vec3.from_seq(support["to"]) + Vec3(0, 1, 0)
        tile = density.sample(AABB(low, high))
        assert tile.support[0, 0, 0] and tile.support[-1, 0, 0]
    assert any(op.get("block") == "minecraft:glowstone" for op in result.blueprint["operations"])
    assert all(op["reserved"] == "circulation" for op in result.blueprint["operations"]
               if op["type"] == "path")
    assert all(op["clearance"] == result.site["layoutRules"]["floorClearance"][0]
               for op in result.blueprint["operations"]
               if op["type"] == "path" and not op["element"].endswith(".threshold"))


def test_underground_routes_preserve_buildings(tmp_path):
    result, bp = concrete_plan(tmp_path)
    # 全体密度はmake行列で実行。ここは造成・棟・経路の衝突を実セルで調べる。
    bp.operations = [op for op in bp.operations if op.type != "terrain_density"]
    volume = next(generate_regions(bp, limits=GenerationLimits((256, None, 256), 0))).volume
    entry = next(n["position"] for n in result.massing["connections"]["nodes"]
                 if n["id"] == "site#1.entry")
    for building in plan_buildings(result.massing):
        for point in [building["entrance"], *building["arrivals"]]:
            assert check_route(volume, Vec3.from_seq(entry), Vec3.from_seq(point),
                               bp.minecraft_version).status == "reachable"
    for support in result.site["supports"]:
        x, low, z = support["from"]
        for y in range(low, support["to"][1] + 1):
            point = Vec3(x, y, z)
            assert volume.get(point).id != "minecraft:air"
            assert volume.attributes(point).reserved == "structure"
    tested = 0
    for region in generate_regions(bp, limits=GenerationLimits((32, None, 32), 0)):
        for point, state in region.volume:
            if region.core.contains(point):
                assert volume.get(point) == state
                assert volume.attributes(point) == region.volume.attributes(point)
                tested += 1
    assert tested > 100


@pytest.mark.parametrize("archetype", ("cave", "underground_city"))
@pytest.mark.parametrize("version", ("1.21.11", "26.1.2", "26.2", "26.3"))
@pytest.mark.parametrize("fmt", ("schem", "litematic"))
def test_underground_fixed_versions_formats(tmp_path, archetype, version, fmt):
    source = tmp_path / "underground.yaml"
    source.write_text(yaml.safe_dump(underground_spec(archetype, scale="huge",
        minecraftVersion=version,
        config={"preview": {"enabled": False}, "output": {"format": fmt}})),
        encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / f"underground.{fmt}")
    assert result.code == 0, result.diagnostics
    record = json.loads(result.record.read_bytes())
    assert result.output.is_file()
    routes = record["metrics"]["walkability"]["routes"]
    assert routes and all(r["status"] == "reachable" for r in routes)


@pytest.mark.parametrize("archetype", ("cave", "underground_city"))
@pytest.mark.parametrize("seed", (73, 101))
def test_underground_other_seeds_and_branches(tmp_path, archetype, seed):
    source = tmp_path / "underground.yaml"
    source.write_text(yaml.safe_dump(underground_spec(archetype, scale="huge", seed=seed,
        config={"terrain": {"caves": 1}, "preview": {"enabled": False}})),
        encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / "underground.schem")
    assert result.code == 0, result.diagnostics
    record = json.loads(result.record.read_bytes())
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


@pytest.mark.parametrize("changes, message", [
    ({"size": [64, 48, 64]}, "主空洞"),
    ({"size": [256, 32, 256]}, "天井"),
    ({"config": {"terrain": {"water_level": 100}}}, "水没"),
    ({"features": {"entrances": 5}}, "入口数"),
    ({"elements": {"house#1": {"width": 20}}}, "区画"),
    ({"size": [256, 64, 256], "elements": {"house#1": {"floors": 2,
                                                       "floor_height": 5}}}, "天井"),
    ({"elements": {"missing#1": {"width": 9}}}, "未知"),
])
def test_underground_rejects_unusable_sites(tmp_path, changes, message):
    with pytest.raises(BlueprintError, match=message):
        plan_buildspec(resolve_buildspec(underground_spec(**changes), project_dir=tmp_path))


@pytest.mark.parametrize("target", ("support", "footing", "bearing", "entrance", "surface"))
def test_underground_overrides_cannot_cut_support_or_close_entrance(tmp_path, target):
    result, bp = concrete_plan(tmp_path)
    support = result.site["supports"][0]
    if target in ("support", "footing", "bearing"):
        point = support["to"] if target == "bearing" else support["from"].copy()
        if target == "support":
            point[1] += 1
    else:
        ident = "site#1.entry" + (".surface" if target == "surface" else "")
        point = next(n["position"] for n in result.site["connections"]["nodes"]
                     if n["id"] == ident)
    result, bp = concrete_plan(tmp_path, overrides=[{"id": "obstruction", "at": point,
        "block": "air" if target in ("support", "footing", "bearing") else "stone"}])
    bp.operations = [op for op in bp.operations if op.type != "terrain_density"]
    with pytest.raises(BlueprintError, match="予約|reserved"):
        generate(bp)


def test_cave_emit_resume_and_four_entrances(tmp_path):
    source = tmp_path / "underground.yaml"
    source.write_text(yaml.safe_dump(underground_spec("cave", features={"entrances": 4},
        config={"preview": {"enabled": False}})), encoding="utf-8", newline="\n")
    first = make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    assert first.code == 0, first.diagnostics
    expected = json.loads(first.record.read_bytes())
    assert len(expected["metrics"]["walkability"]["routes"]) == 8
    for stage in STAGES:
        resumed = make(source, output=tmp_path / f"{stage}.schem",
                       resume=tmp_path / f"first.{stage}.json")
        assert resumed.code == 0, (stage, resumed.diagnostics)
        record = json.loads(resumed.record.read_bytes())
        assert gzip.decompress(first.output.read_bytes()) == gzip.decompress(
            resumed.output.read_bytes())
        assert expected["metrics"] == record["metrics"]
        assert expected["structuredDiagnostics"] == record["structuredDiagnostics"]
        assert gzip.decompress((tmp_path / "first.scene.json.gz").read_bytes()) == gzip.decompress(
            (tmp_path / f"{stage}.scene.json.gz").read_bytes())


@pytest.mark.parametrize("factor", ("ground", "water"))
def test_weather_uses_underground_floor_and_voxel_water_distance(tmp_path, monkeypatch, factor):
    from mcblueprint import pipeline, weather

    resolved = resolve_buildspec(underground_spec(config={"weathering": {
        "age": 0, "moss": {"amount": 1, "north_facing": 0,
            "ground_proximity": {"weight": int(factor == "ground"), "height": 2},
            "water_proximity": {"weight": int(factor == "water"), "radius": 2}}},
        "randomness": 0}), project_dir=tmp_path)
    result = plan_buildspec(resolved)
    concrete = materialize_plan(result.blueprint, palette=result.palette).blueprint
    concrete["metadata"]["generation"] = {"mode": pipeline.GENERATION,
        "config": resolved.config, "groundHeight": result.site["groundHeight"]}
    bp, errors = validate_and_load(concrete)
    assert not errors, errors
    x, z = result.site["parcels"][0]["bounds"][0]
    floor = result.site["floorY"]
    near, far = Vec3(x + 2, floor, z + 2), Vec3(x + 9, floor, z + 2)
    if factor == "ground":
        far = Vec3(x + 2, floor + 5, z + 2)
    box = AABB(Vec3(x, floor, z), Vec3(x + 11, floor + 6, z + 4))
    volume = RegionVolume(box)
    for point in (near, far):
        volume.set(point, BlockState.of("cobblestone"), CellAttributes(role="wall"))
    if factor == "water":
        volume.set(near + Vec3(1, 0, 0), BlockState.of("water"))
    monkeypatch.setattr(pipeline, "generate_regions", lambda *a, **k:
                        iter([GeneratedRegion(box, volume)]))
    monkeypatch.setattr(weather, "_random", lambda *a: 0.4)
    region = next(pipeline.generated_blueprint_regions(bp))
    assert region.volume.get(near).id == "minecraft:mossy_cobblestone"
    assert region.volume.get(far).id == "minecraft:cobblestone"


def test_underground_city_resume_keeps_weather_and_scene(tmp_path):
    source = tmp_path / "underground.yaml"
    source.write_text(yaml.safe_dump(underground_spec(size=[96, 64, 96],
        features={"buildings": 1}, config={"preview": {"enabled": False}})),
        encoding="utf-8", newline="\n")
    first = make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    assert first.code == 0, first.diagnostics
    expected = json.loads(first.record.read_bytes())
    for stage in STAGES:
        resumed = make(source, output=tmp_path / f"{stage}.schem",
                       resume=tmp_path / f"first.{stage}.json")
        assert resumed.code == 0, (stage, resumed.diagnostics)
        record = json.loads(resumed.record.read_bytes())
        assert expected["metrics"] == record["metrics"]
        assert expected["structuredDiagnostics"] == record["structuredDiagnostics"]
        assert gzip.decompress(first.output.read_bytes()) == gzip.decompress(
            resumed.output.read_bytes())
        assert gzip.decompress((tmp_path / "first.scene.json.gz").read_bytes()) == gzip.decompress(
            (tmp_path / f"{stage}.scene.json.gz").read_bytes())


@pytest.mark.parametrize("point", ([23, 5, 23], [23, 19, 23]))
def test_cave_critic_detects_broken_rock_connections(tmp_path, point):
    result, bp = concrete_plan(tmp_path, archetype="cave", size=[64, 48, 64])
    volume = generate(bp)
    critic = plan_from_metadata(bp.metadata["planning"], massing=result.massing)
    measured = evaluate(critic, [GeneratedRegion(bp.bounds(), volume)])
    assert measured.metrics["column_continuity"]["value"] == 1
    assert measured.metrics["flat_ratio"]["status"] == "n/a"
    pos = Vec3.from_seq(point)
    assert volume.get(pos).id == "minecraft:stone"
    assert volume.attributes(pos).reserved == "structure"
    volume.set(pos, BlockState.of("air"), volume.attributes(pos))
    damaged = evaluate(critic, [GeneratedRegion(bp.bounds(), volume)])
    assert damaged.metrics["column_continuity"]["value"] < 1
    assert any(d["code"] == "E101" and d["severity"] == "error" for d in damaged.diagnostics)


@pytest.mark.parametrize("height, floor", ((48, 8), (40, 7)))
def test_cave_selects_connected_floor_and_reaches_real_surface(tmp_path, height, floor):
    result, bp = concrete_plan(tmp_path, archetype="cave", size=[64, height, 64])
    assert result.site["floorY"] == floor
    volume = generate(bp)
    nodes = {n["id"]: Vec3.from_seq(n["position"])
             for n in result.site["connections"]["nodes"]}
    entry, hub, surface = (nodes[k] for k in ("site#1.entry", "site#1.hub", "site#1.entry.surface"))
    density = TerrainDensity.from_dict(result.site["density"])
    assert bp.bounds().contains(surface)
    below = surface - Vec3(0, 1, 0)
    assert density.sample(AABB(below, below)).density.item() >= 0
    assert np.all(density.sample(AABB(surface, surface.with_axis("y", height - 1))).density < 0)
    for start in (surface, entry, *(Vec3(x, int(density.field.heights[z, x]) + 1, z)
                                  for x, z in ((0, 0), (0, 63), (63, 0), (63, 63)))):
        route = check_route(volume, start, hub, bp.minecraft_version)
        assert route.status == "reachable", (start, route.issues)
    bottom, ceiling = cave_surfaces(density)
    path = next(op for op in bp.operations if op.type == "path" and
                op.attributes["element"] == "site#1.entrance#1")
    assert min(int(ceiling[p.z, p.x]) - floor for p, _ in path.centre_line()) >= 4
    assert max(int(bottom[p.z, p.x]) - 1 for p, _ in path.centre_line()) <= floor


def test_cave_missing_support_expectation_requires_regeneration(tmp_path):
    result, _ = concrete_plan(tmp_path, archetype="cave", size=[64, 48, 64])
    planning = copy.deepcopy(result.massing)
    del planning["supports"]
    with pytest.raises(BlueprintError, match="再生成"):
        plan_from_metadata(planning, massing=planning)


def test_cave_floor_search_includes_surface_access(tmp_path):
    result, bp = concrete_plan(tmp_path, archetype="cave", size=[64, 48, 64],
                               config={"terrain": {"water_level": 8}})
    assert result.site["floorY"] == 10
    nodes = {n["id"]: Vec3.from_seq(n["position"])
             for n in result.site["connections"]["nodes"]}
    volume = generate(bp)
    route = check_route(volume, nodes["site#1.entry.surface"], nodes["site#1.hub"],
                        bp.minecraft_version)
    assert route.status == "reachable", route.issues
    assert volume.get(nodes["site#1.entry.surface"] - Vec3(0, 1, 0)).id != "minecraft:water"
