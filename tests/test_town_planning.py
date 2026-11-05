"""街並みの区画・街路・複合棟を計画と実セルの両方で確認する。"""

import copy
import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.critic import evaluate, plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion, GenerationLimits, generate, generate_regions
from mcblueprint.materialize import materialize_plan
from mcblueprint.model import BlockState, Vec3
from mcblueprint.parcels import split_frontage
from mcblueprint.pipeline import STAGES, make
from mcblueprint.plan_contract import plan_buildings
from mcblueprint.planning import plan_buildspec
from mcblueprint.town_planning import street_route
from mcblueprint.traversal import check_route
from mcblueprint.validator import validate_and_load


def town_spec(**changes):
    cases = json.loads(Path("evaluation/cases/large-scale.json").read_bytes())["cases"]
    raw = next(c["buildSpec"] for c in cases if c["id"] == "town-medieval")
    return {**raw, "scale": "huge", "seed": 42, **changes}


@pytest.mark.parametrize("seed", (42, 73, 101))
def test_town_plan_reproducible_and_nonoverlapping(tmp_path, seed):
    resolved = resolve_buildspec(town_spec(seed=seed), project_dir=tmp_path)
    original = copy.deepcopy(resolved.buildspec)
    result = plan_buildspec(resolved)
    assert result == plan_buildspec(resolved)
    assert resolved.buildspec == original
    parcels = result.site["parcels"]
    assert len(parcels) == 8
    assert {p["groups"][0]["shape"] for p in parcels} == {"rectangle", "L", "T"}
    assert {b["params"]["floors"] for b in plan_buildings(result.massing)} == {1, 2}
    for index, first in enumerate(parcels):
        (x, z), (east, south) = first["bounds"]
        for second in parcels[index + 1:]:
            (a, b), (c, d) = second["bounds"]
            assert east < a or c < x or south < b or d < z
    assert all(any(op.get("element") == name for op in result.blueprint["operations"])
               for name in ("plaza#1", "well#1", "market#1"))


@pytest.mark.parametrize("seed", (42, 73))
def test_town_real_routes_and_core_boundaries(tmp_path, seed):
    raw = town_spec(seed=seed, config={"palette": {"materials": {
        "wall_main": "minecraft:stone_bricks", "foundation": "minecraft:stone_bricks",
        "path": "minecraft:stone_bricks", "frame": "minecraft:oak_log"}}})
    result = plan_buildspec(resolve_buildspec(raw, project_dir=tmp_path))
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    bp, errors = validate_and_load(concrete.blueprint)
    assert not errors, errors
    # 地形全体の統合はmake行列で確認し、ここでは造成・棟・街路を分割比較する。
    bp.operations = [op for op in bp.operations if op.type != "terrain_density"]
    volume = generate(bp)
    well = next(op for op in concrete.blueprint["operations"]
                if op.get("element") == "well#1.water")
    water = Vec3.from_seq(well["position"])
    assert volume.get(water).id == "minecraft:water"
    for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        assert volume.get(water + Vec3(dx, 0, dz)).id == "minecraft:stone_bricks"
    nodes = {n["id"]: Vec3.from_seq(n["position"])
             for n in result.massing["connections"]["nodes"]}
    for building in plan_buildings(result.massing):
        for target in [building["entrance"], *building["arrivals"]]:
            route = check_route(volume, nodes["site#1.entry"], Vec3.from_seq(target),
                                bp.minecraft_version)
            assert route.status == "reachable", (building["element"], target, route.issues)
    tested = 0
    for region in generate_regions(bp, limits=GenerationLimits((16, None, 16), 0)):
        for point, state in region.volume:
            if region.core.contains(point) and region.volume.attributes(point).role == "path":
                assert volume.get(point) == state
                assert volume.attributes(point).reserved == region.volume.attributes(point).reserved
                tested += 1
    assert tested > 100


def test_hill_town_without_public_features(tmp_path):
    source = tmp_path / "town.yaml"
    source.write_text(yaml.safe_dump(town_spec(scale="medium", site={"terrain": "hill"},
        features={"buildings": 4, "plaza": False, "well": False, "market": False},
        config={"preview": {"enabled": False}})), encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / "town.schem")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, record["structuredDiagnostics"]
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


@pytest.mark.parametrize("version", ("1.21.11", "26.1.2", "26.2", "26.3"))
@pytest.mark.parametrize("fmt", ("schem", "litematic"))
def test_town_fixed_versions_and_formats(tmp_path, version, fmt):
    source = tmp_path / "town.yaml"
    source.write_text(yaml.safe_dump(town_spec(minecraftVersion=version,
        config={"preview": {"enabled": False}, "output": {"format": fmt}})),
        encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / f"town.{fmt}")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, [d for d in record["structuredDiagnostics"]
                              if d["severity"] == "fail"]
    assert result.output.is_file()
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


@pytest.mark.parametrize("seed", (73, 101))
def test_town_fixed_other_seeds(tmp_path, seed):
    source = tmp_path / "town.yaml"
    source.write_text(yaml.safe_dump(town_spec(seed=seed, config={"preview": {"enabled": False}})),
                      encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / "town.schem")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, record["structuredDiagnostics"]
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


def test_city_reuses_town(tmp_path):
    result = plan_buildspec(resolve_buildspec(town_spec(archetype="city"), project_dir=tmp_path))
    assert result.site["archetype"] == "city"
    assert len(result.site["parcels"]) == 8


def test_town_medium_odd_seed(tmp_path):
    result = plan_buildspec(resolve_buildspec(town_spec(scale="medium", seed=73,
        features={"buildings": 4}), project_dir=tmp_path))
    assert len(result.site["parcels"]) == 4


@pytest.mark.parametrize("seed", (42, 73, 101))
def test_town_medium_fixed_cases(tmp_path, seed):
    source = tmp_path / "town.yaml"
    source.write_text(yaml.safe_dump(town_spec(scale="medium", seed=seed,
        config={"preview": {"enabled": False}})), encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / "town.schem")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, record["structuredDiagnostics"]
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


def test_frontage_split_and_narrow_rejection():
    assert split_frontage(0, 0, 49, 15, [12, 24], [12, 24]) == [
        (0, 0, 16, 15), (17, 0, 33, 15), (34, 0, 49, 15)]
    with pytest.raises(BlueprintError, match="狭小"):
        split_frontage(0, 0, 9, 15, [12, 24], [12, 24])


def test_astar_avoids_slope_water_protection_and_dead_end(tmp_path, monkeypatch):
    result = plan_buildspec(resolve_buildspec(town_spec(), project_dir=tmp_path))
    from mcblueprint.terrain_field import TerrainField

    field = TerrainField.from_dict(result.site["groundHeight"]["field"])
    heights = field.heights.copy()
    heights[5, 6:10] += 4
    heights[5, 10:14] = field.water_level - 1
    field = replace(field, heights=heights)
    blocked = {(8, 4), (8, 6)}
    route = street_route(field, (3, 5), (17, 5), (0, 0, 20, 10), blocked)
    assert not set(route) & blocked
    assert not any(z == 5 and 6 <= x < 14 for x, z in route)
    with pytest.raises(BlueprintError, match="行止まり"):
        street_route(field, (3, 5), (17, 5), (0, 0, 20, 10),
                     {(10, z) for z in range(11)})
    from mcblueprint import town_planning

    wet_heights = field.heights.copy()
    wet_heights[:] = field.water_level - 1
    monkeypatch.setattr(town_planning, "plan_terrain", lambda *args, **kwargs:
                        replace(field, heights=wet_heights))
    with pytest.raises(BlueprintError, match="区画.*水域"):
        plan_buildspec(resolve_buildspec(town_spec(), project_dir=tmp_path))


@pytest.mark.parametrize("changes, message", [
    ({"size": [32, 48, 32]}, "size"),
    ({"features": {"buildings": 10000}}, "size"),
    ({"elements": {"house#1": {"width": 30}}}, "区画"),
    ({"elements": {"unknown#1": {"width": 9}}}, "未知"),
])
def test_town_rejects_conflicts(tmp_path, changes, message):
    with pytest.raises(BlueprintError, match=message):
        plan_buildspec(resolve_buildspec(town_spec(**changes), project_dir=tmp_path))


@pytest.mark.parametrize("terrain, seed", (("hill", 73), ("valley", 3), ("hill", 0)))
def test_town_diverted_roads_preserve_buildings_and_reachability(tmp_path, terrain, seed):
    source = tmp_path / "town.yaml"
    source.write_text(yaml.safe_dump(town_spec(scale="large", seed=seed,
        site={"terrain": terrain}, config={"preview": {"enabled": False}})),
        encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / "town.schem")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, record["structuredDiagnostics"]
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


@pytest.mark.parametrize("feature", ("plaza", "well", "market"))
def test_town_rejects_water_in_public_grading(tmp_path, monkeypatch, feature):
    from mcblueprint import town_planning

    raw = town_spec(features={"buildings": 8, **{
        name: name == feature for name in ("plaza", "well", "market")}})
    resolved = resolve_buildspec(raw, project_dir=tmp_path)
    baseline = plan_buildspec(resolved)
    public = next(op for op in baseline.blueprint["operations"]
                  if op.get("element") == feature + "#1")
    x, _, z = public.get("position", public.get("from"))
    if feature == "plaza":
        x, z = 132, 118
    original = town_planning.plan_terrain

    def wet(*args, **kwargs):
        field = original(*args, **kwargs)
        heights = field.heights.copy()
        heights[z, x] = field.water_level - 1
        return replace(field, heights=heights)

    monkeypatch.setattr(town_planning, "plan_terrain", wet)
    with pytest.raises(BlueprintError, match="造成範囲.*水域"):
        plan_buildspec(resolved)


@pytest.mark.parametrize("high, low", (("house#1", "house#2"), ("house#3", "house#4")))
def test_town_mixed_floor_junction_support_is_visible_but_not_optional(tmp_path, high, low):
    result = plan_buildspec(resolve_buildspec(town_spec(scale="medium",
        features={"buildings": 4}, elements={high: {"floors": 2}, low: {"floors": 1}}),
        project_dir=tmp_path))
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    bp, errors = validate_and_load(concrete.blueprint)
    assert not errors, errors
    bp.operations = [op for op in bp.operations if op.type != "terrain_density"]
    volume = generate(bp)
    critic = plan_from_metadata(bp.metadata["planning"], massing=result.massing)
    measured = evaluate(critic, [GeneratedRegion(bp.bounds(), volume)])
    assert measured.metrics["facade_surface_missing"]["value"] == 0
    assert not any(d["code"] == "E101" for d in measured.diagnostics)
    from mcblueprint.junctions import junction_geometry

    supports = {p for j in junction_geometry(result.massing) for p in j.supports}
    point = next(c.pos for f in critic.facades for c in f.cells
                 if not c.opening and c.pos in supports)
    assert volume.attributes(point).owner == critic.junction_cells[point]
    volume.set(point, BlockState.of("air"), volume.attributes(point))
    damaged = evaluate(critic, [GeneratedRegion(bp.bounds(), volume)])
    assert damaged.metrics["facade_surface_missing"]["value"] > 0
    assert any(d["code"] == "E101" for d in damaged.diagnostics)


def test_town_emit_resume_matches_weather_output_quality_and_scene(tmp_path):
    source = tmp_path / "town.yaml"
    source.write_text(yaml.safe_dump(town_spec(scale="medium", features={"buildings": 4},
        site={"terrain": "hill"}, config={"preview": {"enabled": False}})),
        encoding="utf-8", newline="\n")
    first = make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    assert first.code == 0
    expected = json.loads(first.record.read_bytes())
    for stage in STAGES:
        resumed = make(source, output=tmp_path / f"{stage}.schem",
                       resume=tmp_path / f"first.{stage}.json")
        assert resumed.code == 0, stage
        record = json.loads(resumed.record.read_bytes())
        assert gzip.decompress(first.output.read_bytes()) == gzip.decompress(
            resumed.output.read_bytes()), stage
        assert expected["metrics"] == record["metrics"], stage
        assert expected["structuredDiagnostics"] == record["structuredDiagnostics"], stage
        assert gzip.decompress((tmp_path / "first.scene.json.gz").read_bytes()) == gzip.decompress(
            (tmp_path / f"{stage}.scene.json.gz").read_bytes()), stage
