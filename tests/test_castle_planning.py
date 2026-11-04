"""城郭の固定課題と、閉城壁・接合・造成の反証。"""

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
from mcblueprint.pipeline import make
from mcblueprint.plan_contract import plan_buildings
from mcblueprint.planning import plan_buildspec
from mcblueprint.traversal import check_route
from mcblueprint.validator import validate_and_load


def castle_spec(**changes):
    fixed = json.loads(Path("evaluation/cases/large-scale.json").read_text(encoding="utf-8"))
    return {**fixed["cases"][0]["buildSpec"], "scale": "medium", "seed": 42, **changes}


def castle_volume(tmp_path, **changes):
    raw = castle_spec(site={"terrain": "flat"}, **changes)
    raw["config"] = {"palette": {"materials": {
        "wall_main": "minecraft:stone_bricks", "foundation": "minecraft:stone_bricks",
        "path": "minecraft:stone_bricks", "frame": "minecraft:oak_log"}}}
    result = plan_buildspec(resolve_buildspec(raw, project_dir=tmp_path))
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    blueprint, errors = validate_and_load(concrete.blueprint)
    assert not errors, errors
    size = blueprint.bounds().size
    regions = generate_regions(blueprint, limits=GenerationLimits((size.x, None, size.z), 0))
    region = next(regions)
    regions.close()
    assert all(region.core.contains(op.bounds().min) and region.core.contains(op.bounds().max)
               for op in blueprint.operations if op.type != "terrain_density")
    return result, blueprint, region.volume


@pytest.mark.parametrize("scale, seed, shape", (("medium", 42, "square"),
    ("huge", 42, "square"), ("huge", 73, "square"), ("huge", 101, "square"),
    ("medium", 42, "round")))
def test_corner_approaches_do_not_cut_the_closed_wall(tmp_path, scale, seed, shape):
    result, blueprint, volume = castle_volume(tmp_path, scale=scale, seed=seed,
                                              elements={"tower#1": {"shape": shape}})
    floor = result.site["floorY"]
    west, north = result.site["wallPolygon"][0]
    for op in result.blueprint["operations"]:
        if op.get("element") == "wall#1":
            x, _, z = op["from"]
            for y in (floor + 1, floor + 2):
                point = Vec3(x, y, z)
                assert volume.get(point).id == "minecraft:stone_bricks", point
                assert volume.attributes(point).owner == "wall#1"
    buildings = plan_buildings(result.massing)
    nodes = {n["id"]: Vec3.from_seq(n["position"])
             for n in result.massing["connections"]["nodes"]}
    target = Vec3.from_seq(buildings[0]["entrance"])
    assert check_route(volume, nodes["gate#1.outside"], target,
                       blueprint.minecraft_version).status == "reachable"
    cx = nodes["gate#1.outside"].x
    for x in range(cx - 3, cx + 4):
        for y in range(floor + 1, floor + 8):
            for z in (north, north + 1):
                point = Vec3(x, y, z)
                volume.set(point, BlockState.of("stone"), volume.attributes(point))
    for start in (nodes["gate#1.outside"], Vec3(west - 1, floor + 1, north + 6)):
        assert check_route(volume, start, target,
                           blueprint.minecraft_version).status != "reachable"


@pytest.mark.parametrize("depths, floors", (((13, 13, 13), (2, 1, 1)),
                                            ((13, 11, 15), (2, 2, 2)),
                                            ((8, 8, 8), (2, 1, 1))))
def test_contacts_follow_actual_building_dimensions(tmp_path, depths, floors):
    result, blueprint, volume = castle_volume(tmp_path, elements={
        f"house#{i + 1}": {"depth": depth, "floors": count}
        for i, (depth, count) in enumerate(zip(depths, floors, strict=True))})
    houses = plan_buildings(result.massing)[:3]
    for contact, first, second in zip(result.massing["junctions"][0]["contacts"],
                                       houses, houses[1:], strict=False):
        assert contact["to"][1] == min(b["offset"][1] + b["params"]["floors"]
                                       * b["params"]["floor_height"] for b in (first, second))
        assert contact["to"][2] == min(b["offset"][2] + b["params"]["depth"] - 1
                                       for b in (first, second))
    critic = plan_from_metadata(blueprint.metadata["planning"], massing=result.massing)
    measured = evaluate(critic, [GeneratedRegion(blueprint.bounds(), volume)])
    assert not any(d["code"] in ("E101", "E203") for d in measured.diagnostics)
    for passage in result.massing["junctions"][0]["passages"]:
        nodes = {n["id"]: Vec3.from_seq(n["position"])
                 for n in result.massing["connections"]["nodes"]}
        assert check_route(volume, nodes[passage["from"]], nodes[passage["to"]],
                           blueprint.minecraft_version).status == "reachable"


@pytest.mark.parametrize("count", (11, 12, 14))
def test_towers_use_all_four_sides_when_they_fit(tmp_path, count):
    result, blueprint, volume = castle_volume(tmp_path, scale="huge", features={"towers": count})
    towers = [b for b in plan_buildings(result.massing) if b["archetype"] == "tower"]
    assert len(towers) == count
    west, north = result.site["wallPolygon"][0]
    east, south = result.site["wallPolygon"][2]
    assert all(any(b["offset"][axis] == value for b in towers)
               for axis, value in ((0, west), (0, east), (2, north), (2, south)))
    for i, first in enumerate(towers):
        for second in towers[i + 1:]:
            assert max(abs(first["offset"][axis] - second["offset"][axis])
                       for axis in (0, 2)) >= 16
    if count == 12:
        for axis, value, along in ((0, west, 2), (0, east, 2), (2, north, 0), (2, south, 0)):
            positions = sorted(b["offset"][along] for b in towers
                               if b["offset"][axis] == value)
            assert all(16 <= b - a <= 32 for a, b in zip(positions, positions[1:], strict=False))
    start = next(Vec3.from_seq(n["position"]) for n in result.massing["connections"]["nodes"]
                 if n["id"] == "gate#1.outside")
    for tower in towers:
        assert check_route(volume, start, Vec3.from_seq(tower["entrance"]),
                           blueprint.minecraft_version).status == "reachable"


@pytest.mark.parametrize("scale", ("medium", "huge"))
@pytest.mark.parametrize("width", (5, 7))
def test_common_road_uses_layout_width(tmp_path, monkeypatch, scale, width):
    from mcblueprint import castle_planning

    original = castle_planning.load_layout("medieval.castle")
    original["rules"]["roadWidth"] = [width, width]
    monkeypatch.setattr(castle_planning, "load_layout", lambda name: copy.deepcopy(original))
    result, _, volume = castle_volume(tmp_path, scale=scale)
    road = next(op for op in result.blueprint["operations"]
                if op.get("element") == "site#1.road#1")
    gate = next(op for op in result.blueprint["operations"] if op.get("element") == "gate#1")
    assert road["width"] == gate["width"] == width
    cx, floor, start = road["points"][0]
    end = road["points"][-1][2]
    for z in range(start, end + 1):
        for x in range(cx - (width - 1) // 2, cx + width // 2 + 1):
            assert volume.get(Vec3(x, floor, z)).id == "minecraft:stone_bricks"
            assert all(volume.get(Vec3(x, floor + dy, z)).id == "minecraft:air" for dy in (1, 2))


@pytest.mark.parametrize("scale", ("medium", "huge"))
@pytest.mark.parametrize("seed", (42, 73, 101))
def test_castle_plan_reproducible_and_required_elements(tmp_path, scale, seed):
    resolved = resolve_buildspec(castle_spec(scale=scale, seed=seed), project_dir=tmp_path)
    original = copy.deepcopy(resolved.buildspec)
    first, second = plan_buildspec(resolved), plan_buildspec(resolved)
    assert first == second
    assert resolved.buildspec == original
    buildings = plan_buildings(first.massing)
    assert sum(b["archetype"] == "house" for b in buildings) == 3
    assert sum(b["archetype"] == "tower" for b in buildings) == 6
    assert [b["params"]["floors"] for b in buildings[:3]] == [2, 1, 1]
    assert first.site["wallPolygon"][0] == first.site["wallPolygon"][-1]
    assert first.site["groundHeight"]["type"] == "heightfield"
    assert first.massing["junctions"][0]["roofPolicy"] == "envelope"
    for name in ("wall#1", "gate#1", "moat#1", "foundation#1"):
        assert any(op.get("element") == name for op in first.blueprint["operations"])
    assert all(op["element"].startswith("site#1.") for op in first.blueprint["operations"]
               if op.get("role") == "path")


@pytest.mark.parametrize("change, message", [
    ({"size": [32, 48, 32]}, "size"),
    ({"features": {"towers": 1000000}}, "塔数"),
    ({"elements": {"house#1": {"width": 30}}}, "接合|干渉"),
    ({"elements": {"unknown#1": {"width": 9}}}, "未知"),
    ({"elements": {"tower#1": {"radius": 8}}}, "干渉"),
])
def test_castle_rejects_conflicts_without_dropping_towers(tmp_path, change, message):
    with pytest.raises(BlueprintError, match=message):
        plan_buildspec(resolve_buildspec(castle_spec(**change), project_dir=tmp_path))


def test_relocated_detail_diagnostics_belong_to_the_building(tmp_path):
    from mcblueprint.castle_planning import _place

    resolved = resolve_buildspec(castle_spec(), project_dir=tmp_path)
    _, _, _, diagnostics = _place(resolved, "house", "house#7", [20, 10, 30], 0,
                                  {"floors": 2, "floor_height": 4, "roof": "gable"},
                                  {"width": 9, "depth": 11})
    assert diagnostics
    assert all(d["element"].startswith("house#7.") for d in diagnostics)


@pytest.mark.parametrize("version", ("1.21.11", "26.1.2", "26.2", "26.3"))
@pytest.mark.parametrize("fmt", ("schem", "litematic"))
def test_castle_fixed_all_versions_formats(tmp_path, version, fmt):
    source = tmp_path / "castle.yaml"
    raw = castle_spec(minecraftVersion=version, config={"preview": {"enabled": False},
                                                      "output": {"format": fmt}})
    source.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / f"castle.{fmt}")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, [d for d in record["structuredDiagnostics"]
                              if d["severity"] == "fail"]
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])
    assert result.output.is_file()
    assert result.output.suffix == "." + fmt


@pytest.mark.parametrize("scale, seed", (("medium", 73), ("medium", 101),
                                         ("huge", 42), ("huge", 73), ("huge", 101)))
def test_castle_fixed_scales_and_seeds(tmp_path, scale, seed):
    source = tmp_path / "castle.yaml"
    source.write_text(yaml.safe_dump(castle_spec(
        scale=scale, seed=seed, config={"preview": {"enabled": False}})),
        encoding="utf-8", newline="\n")
    result = make(source, output=tmp_path / "castle.schem")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, [d for d in record["structuredDiagnostics"]
                              if d["severity"] == "fail"]
    assert all(r["status"] == "reachable" for r in record["metrics"]["walkability"]["routes"])


def test_castle_gate_closure_blocks_outside_routes(tmp_path):
    result = plan_buildspec(resolve_buildspec(castle_spec(), project_dir=tmp_path))
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    blueprint, errors = validate_and_load(concrete.blueprint)
    assert not errors, errors
    volume = generate(blueprint)
    moat = [p for p, state in volume if state.id == "minecraft:water"
            and volume.attributes(p).element == "moat#1"]
    assert moat
    for point in moat:
        for delta in (Vec3(0, -1, 0), Vec3(1, 0, 0), Vec3(-1, 0, 0),
                      Vec3(0, 0, 1), Vec3(0, 0, -1)):
            support = volume.get(point + delta)
            assert support is not None and support.id != "minecraft:air", point
    plan = result.massing
    start = Vec3.from_seq(next(n["position"] for n in plan["connections"]["nodes"]
                              if n["id"] == "gate#1.outside"))
    target = Vec3.from_seq(plan_buildings(plan)[0]["entrance"])
    assert check_route(volume, start, target, "1.21.11").status == "reachable"
    north = result.site["wallPolygon"][0][1]
    for x in range(start.x - 3, start.x + 4):
        for y in range(start.y, start.y + 5):
            for z in (north, north + 1):
                point = Vec3(x, y, z)
                volume.set(point, BlockState.of("stone"), volume.attributes(point))
    assert check_route(volume, start, target, "1.21.11").status != "reachable"


def test_castle_rejects_steep_terrain(tmp_path, monkeypatch):
    from mcblueprint import castle_planning

    original = castle_planning.plan_terrain

    def steep(*args, **kwargs):
        field = original(*args, **kwargs)
        heights = field.heights.copy()
        heights[:, :32] = 0
        heights[:, 32:] = 27
        heights.flags.writeable = False
        return replace(field, heights=heights)

    monkeypatch.setattr(castle_planning, "plan_terrain", steep)
    with pytest.raises(BlueprintError, match="急勾配"):
        plan_buildspec(resolve_buildspec(castle_spec(), project_dir=tmp_path))


def test_castle_blueprint_resume_preserves_terrain_quality_and_scene(tmp_path):
    source = tmp_path / "castle.yaml"
    source.write_text(yaml.safe_dump(castle_spec(config={"preview": {"enabled": False}})),
                      encoding="utf-8", newline="\n")
    first = make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    second = make(source, output=tmp_path / "second.schem",
                  resume=tmp_path / "first.blueprint.json")
    assert first.code == second.code == 0
    assert gzip.decompress(first.output.read_bytes()) == gzip.decompress(second.output.read_bytes())
    records = [json.loads(r.record.read_bytes()) for r in (first, second)]
    assert records[0]["metrics"] == records[1]["metrics"]
    assert records[0]["structuredDiagnostics"] == records[1]["structuredDiagnostics"]
    assert gzip.decompress((tmp_path / "first.scene.json.gz").read_bytes()) == gzip.decompress(
        (tmp_path / "second.scene.json.gz").read_bytes())
