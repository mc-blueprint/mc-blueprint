"""住宅・塔の固定課題、予約、寸法と計画の再現性。"""

import copy
import json
from pathlib import Path

import pytest

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters.schem import SchemExporter
from mcblueprint.generator import GenerationLimits, generate, generate_regions
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.materialize import materialize
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.operations.shapes import disc_offsets
from mcblueprint.planning import plan_buildspec
from mcblueprint.traversal import check_building, check_route
from mcblueprint.validator import validate
from mcblueprint.volume import BlockVolume

VERSIONS = ("1.21.11", "26.1.2", "26.2", "26.3")
STYLES = ("medieval", "japanese", "modern")


def plan(tmp_path, **values):
    return plan_buildspec(resolve_buildspec(
        {"minecraftVersion": "1.21.11", **values}, project_dir=tmp_path,
        user_config=tmp_path / "missing.yaml",
    ))


def built(result, style="medieval"):
    concrete = materialize(result.blueprint, style=style, palette=result.palette)
    assert validate(concrete.blueprint) == []
    bp = load_blueprint_dict(concrete.blueprint)
    if bp.terrain:
        return bp, generate(bp)
    volume = BlockVolume()
    for region in generate_regions(bp):
        for pos, state in region.volume:
            if region.core.contains(pos):
                volume.set(pos, state, region.volume.attributes(pos))
    return bp, volume


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("floors", (1, 2))
def test_house_fixed_case_all_versions(tmp_path, version, style, floors):
    result = plan(tmp_path, minecraftVersion=version, style=style, features={"floors": floors})
    bp, volume = built(result, style)
    assert all(r.status == "reachable" for r in check_building(volume, result.layout, version))
    layout = result.layout
    assert volume.get(layout.entrance).id.endswith("_door")
    assert volume.attributes(layout.entrance).reserved == "circulation"
    assert any(state.id == "minecraft:glass" for _, state in volume)
    assert any(state.id == "minecraft:sea_lantern" for _, state in volume)
    assert any(attrs.role == "roof" for p, _ in volume if (attrs := volume.attributes(p)))
    for face in layout.faces:
        assert face.bounds.max.y >= floors * 4
    assert bp.bounds().size.x <= 32 and bp.bounds().size.z <= 32
    # 各主要室の中央を屋根（陸屋根なら同じ天井）が覆う。
    for point in layout.arrivals:
        assert any(volume.attributes(Vec3(point.x, y, point.z)).role == "roof"
                   for y in range(layout.roof_bounds.min.y, layout.roof_bounds.max.y + 1))


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("shape", ("round", "square"))
def test_tower_fixed_case_all_versions(tmp_path, version, style, shape):
    result = plan(tmp_path, minecraftVersion=version, archetype="tower", style=style,
                  features={"shape": shape, "floors": 3})
    _, volume = built(result, style)
    start = Vec3.from_seq(result.massing["entrance"])
    for point in result.massing["arrivals"]:
        end = Vec3.from_seq(point)
        assert check_route(volume, start, end, version).status == "reachable"
        assert volume.attributes(end).reserved == "circulation"
        assert volume.get(end - Vec3(0, 1, 0)) is not None
    # 各階の開口が外壁を貫き、内部から外を見られる。
    offset = Vec3.from_seq(result.site["offset"])
    for y in (2, 6, 10):
        assert volume.get(offset + Vec3(4, y, 0)).id == "minecraft:glass"
        assert volume.get(offset + Vec3(3, y, 0)).id == "minecraft:air"
    assert any(volume.attributes(p).reserved == "structure" for p, _ in volume)


@pytest.mark.parametrize("archetype", ("house", "tower"))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_rotation_reservations_and_region_determinism(tmp_path, archetype, angle):
    root = f"{archetype}#1"
    result = plan(tmp_path, archetype=archetype, elements={root: {"angle": angle}}, scale="tiny")
    bp, volume = built(result)
    expected = {p: (state, volume.attributes(p)) for p, state in volume}
    for size, reverse in ((16, False), (7, True)):
        actual = {}
        for region in generate_regions(bp, limits=GenerationLimits((size, None, size), 0),
                                       reverse=reverse):
            actual.update({p: (state, region.volume.attributes(p))
                           for p, state in region.volume if region.core.contains(p)})
        assert actual == expected
    if result.layout:
        assert all(r.status == "reachable" for r in check_building(
            volume, result.layout, "1.21.11",
        ))
    else:
        for end in result.massing["arrivals"]:
            assert check_route(volume, Vec3.from_seq(result.massing["entrance"]),
                               Vec3.from_seq(end), "1.21.11").status == "reachable"


@pytest.mark.parametrize("archetype", ("house", "tower"))
def test_seed_stability_and_columns(tmp_path, archetype):
    first = plan(tmp_path, archetype=archetype, seed=29)
    second = plan(tmp_path, archetype=archetype, seed=29)
    assert first.blueprint == second.blueprint
    assert first.massing == second.massing and first.detail == second.detail
    assert json.loads(json.dumps(first.site)) == first.site
    _, volume = built(first)
    root = f"{archetype}#1"
    columns = {}
    for pos, state in volume:
        attrs = volume.attributes(pos)
        if attrs.element == f"{root}.frame":
            assert attrs.reserved == "structure" and state.id != "minecraft:air"
            columns.setdefault((pos.x, pos.z), []).append(pos.y)
    assert sum(sorted(ys) == list(range(max(ys) + 1)) for ys in columns.values()) >= 4
    original = copy.deepcopy(first.blueprint)
    built(first)
    assert first.blueprint == original


@pytest.mark.parametrize("archetype,shape", (("house", None), ("tower", "round"),
                                               ("tower", "square")))
def test_planning_exports_expected_structure_and_facade_contract(tmp_path, archetype, shape):
    features = {"shape": shape} if shape else {}
    first = plan(tmp_path, archetype=archetype, features=features)
    second = plan(tmp_path, archetype=archetype, features=features,
                  elements={f"{archetype}#1": {"angle": 90}})
    contract = first.massing
    assert contract["columns"] and contract["beams"] and contract["facades"]
    assert [column["id"] for column in contract["columns"]] == [
        column["id"] for column in second.massing["columns"]
    ]
    assert [beam["id"] for beam in contract["beams"]] == [
        beam["id"] for beam in second.massing["beams"]
    ]
    assert all(facade["normal"] and facade["origin"] and facade["uAxis"]
               and facade["vAxis"] for facade in contract["facades"])
    assert all(opening["projection"]["u"] and opening["projection"]["v"]
               for facade in contract["facades"] for opening in facade["openings"])
    if shape == "round":
        radius = first.massing["params"]["radius"]
        outline = {(p.x, p.z) for p in disc_offsets(radius, "hollow", "y")}
        assert all((column["from"][0] - first.site["offset"][0],
                    column["from"][2] - first.site["offset"][2]) in outline
                   for column in contract["columns"])
    _, volume = built(first)
    for column in contract["columns"]:
        x, _, z = column["from"]
        y0, y1 = column["from"][1], column["to"][1]
        assert all(volume.attributes(Vec3(x, y, z)).reserved == "structure"
                   for y in range(y0, y1 + 1))
    assert first.blueprint["metadata"]["planning"]["columns"] == contract["columns"]


@pytest.mark.parametrize("style,roof", (("medieval", "hip"), ("japanese", "gable"),
                                       ("modern", "flat")))
def test_roof_styles_and_exact_size_limit(tmp_path, style, roof):
    features = {} if roof == "flat" else {"roof": roof}
    result = plan(tmp_path, style=style, features=features)
    bp, _ = built(result, style)
    size = bp.bounds().size.to_list()
    assert result.massing["params"]["roof"] == roof
    exact = plan(tmp_path, style=style, features=features, size=size)
    assert built(exact, style)[0].bounds().size.to_list() == size
    with pytest.raises(BlueprintError, match="size"):
        plan(tmp_path, style=style, features=features, size=[size[0] - 1, size[1], size[2]])


@pytest.mark.parametrize("style", STYLES)
def test_detail_uses_bundled_facade_modules_and_marks_decoration(tmp_path, style):
    spec = {"style": style, "config": {"decoration": {"density": 1.0}}}
    first = plan(tmp_path, **spec)
    second = plan(tmp_path, **spec)
    components = [op for op in first.detail["operations"] if op["type"] == "component"]
    assert components
    assert all(op["decoration"] is True and op["onConflict"] == "skip" for op in components)
    assert all(any(item["id"] == op["id"] and item["face"] == op["face"]
                   for item in first.detail["decorations"]) for op in components)
    assert first.detail == second.detail
    built(first, style)


def test_detail_density_depth_and_symmetry_settings(tmp_path, monkeypatch):
    def components(**decoration):
        result = plan(tmp_path, config={"decoration": decoration})
        return [op for op in result.detail["operations"] if op["type"] == "component"]

    default = components()
    sparse = components(density=0.0, symmetry=0.0)
    dense = components(density=1.0, symmetry=0.0)
    shallow = components(density=1.0, depth=0.0, symmetry=0.0)
    monkeypatch.setattr("mcblueprint.planning._overlaps", lambda first, second: False)
    deep = components(density=1.0, depth=1.0, symmetry=1.0)
    assert default
    assert len(dense) >= len(sparse)
    assert len(shallow) < len(deep)
    bays = {(op["face"], op["id"].rsplit("bay#", 1)[-1])
            for op in deep if ".bay#" in op["id"]}
    for face in ("north", "east"):
        opposite = "south" if face == "north" else "west"
        assert {bay for current, bay in bays if current == face} == {
            bay for current, bay in bays if current == opposite
        }


@pytest.mark.parametrize("floors,height", ((1, 4), (2, 5), (4, 6)))
def test_tower_floor_spacing_and_no_battlement(tmp_path, floors, height):
    result = plan(tmp_path, archetype="tower", features={"floors": floors,
                  "floor_height": height, "battlement": False})
    _, volume = built(result)
    points = result.massing["arrivals"]
    assert len(points) == floors + 1
    assert [p[1] for p in points] == [i * height + 1 for i in range(floors + 1)]
    assert all(check_route(volume, Vec3.from_seq(result.massing["entrance"]),
                           Vec3.from_seq(p), "1.21.11").status == "reachable" for p in points)


def test_elements_errors_and_furnished(tmp_path):
    for edits in ({"house#2": {}}, {"house#1": {"unknown": 3}},
                  {"house#1": {"width": True}}, {"house#1": {"angle": 45}},
                  {"house#1": {"floors": 3}}):
        with pytest.raises(BlueprintError):
            plan(tmp_path, elements=edits)
    with pytest.raises(BlueprintError, match="Detail.*未対応"):
        plan(tmp_path, interior="furnished")
    with pytest.raises(BlueprintError, match="size"):
        plan(tmp_path, size=[2, 4, 2])


def test_explicit_flat_roof_is_accepted_by_buildspec_schema(tmp_path):
    result = plan(tmp_path, style="modern", features={"roof": "flat"})
    assert result.massing["params"]["roof"] == "flat"


def test_override_scene_coordinates_idempotence_and_reservation_conflict(tmp_path):
    result = plan(tmp_path)
    bp, volume = built(result)
    free = next(p for p, state in volume if volume.attributes(p).reserved is None
                and volume.attributes(p).role == "wall_main")
    override = {"id": "edit-1", "at": (free - bp.bounds().min).to_list(), "block": "gold_block"}
    edited = plan(tmp_path, overrides=[override, override])
    assert len(edited.detail["overrides"]) == 1
    assert built(edited)[1].get(free).id == "minecraft:gold_block"
    reserved = next(p for p, _ in volume if volume.attributes(p).reserved == "structure")
    override["at"] = (reserved - bp.bounds().min).to_list()
    with pytest.raises(BlueprintError, match="予約セル"):
        built(plan(tmp_path, overrides=[override]))
    with pytest.raises(BlueprintError, match="IDが競合"):
        plan(tmp_path, overrides=[override, override | {"block": "stone"}])


@pytest.mark.parametrize("archetype,floors", (("house", 2), ("tower", 3)))
@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_arrival_and_landing_support_cannot_be_removed(tmp_path, archetype, floors, angle):
    values = {"archetype": archetype, "features": {"floors": floors},
              "elements": {f"{archetype}#1": {"angle": angle}}}
    result = plan(tmp_path, **values)
    bp, volume = built(result)
    supports = set()
    for point in result.massing["arrivals"]:
        feet = Vec3.from_seq(point)
        support = feet - Vec3(0, 1, 0)
        attrs = volume.attributes(support)
        assert attrs.reserved == "circulation"
        assert attrs.owner == volume.attributes(feet).owner
        supports.add(support)
    supports.update(p for p, _ in volume
                    if (volume.attributes(p).element or "").endswith(".landing"))
    for support in supports:
        edited = plan(tmp_path, **values, overrides=[{
            "id": "remove-support", "at": (support - bp.bounds().min).to_list(), "block": "air",
        }])
        concrete = materialize(edited.blueprint, palette=edited.palette)
        edited_bp = load_blueprint_dict(concrete.blueprint)
        with pytest.raises(BlueprintError, match="予約セル"):
            list(generate_regions(edited_bp))
        with pytest.raises(BlueprintError, match="予約セル"):
            generate(edited_bp)


@pytest.mark.parametrize("style", STYLES)
def test_planning_uses_resolved_style_roles_without_supplements(tmp_path, style):
    resolved = resolve_buildspec({"minecraftVersion": "1.21.11", "style": style},
                                 project_dir=tmp_path, user_config=tmp_path / "missing.yaml")
    result = plan_buildspec(resolved)
    assert result.palette == resolved.config["palette"]
    assert result.palette is not resolved.config["palette"]
    assert len(result.palette["materials"]) == 9


def test_fixed_case_fixtures_preserve_requirements(tmp_path):
    cases = json.loads(Path("evaluation/cases/fixed-cases.json").read_text(encoding="utf-8"))
    for case in cases["cases"][:2]:
        fixture = Path("tests/data/buildspec") / f"{case['id']}.yaml"
        text = fixture.read_text(encoding="utf-8")
        assert all(line in text for line in [case["task"], *case["requirements"]])
        result = plan_buildspec(resolve_buildspec(fixture, project_dir=tmp_path,
                                                 user_config=tmp_path / "missing.yaml"))
        assert result.blueprint["metadata"]["planning"]["element"] in ("house#1", "tower#1")
        _, volume = built(result)
        if result.layout:
            assert all(r.status == "reachable" for r in check_building(
                volume, result.layout, "1.21.11",
            ))
        else:
            assert all(check_route(volume, Vec3.from_seq(result.massing["entrance"]),
                                   Vec3.from_seq(p), "1.21.11").status == "reachable"
                       for p in result.massing["arrivals"])


def test_snapshot_foundation_and_scene_origin(tmp_path):
    volume = BlockVolume()
    # 元ファイルの貼付座標を保つ、地上と十分なairを含む取得領域。
    for x in range(40):
        for z in range(40):
            for y in range(3):
                volume.set(Vec3(x + 10, y - 3, z + 20), BlockState.of("stone"))
    volume.set(Vec3(49, 27, 59), BlockState.of("air"))
    source = tmp_path / "terrain.schem"
    SchemExporter().export(volume, Blueprint(1, "1.21.11", "地形", []), source)
    result = plan(tmp_path, site={"terrain": str(source)})
    bp, output = built(result)
    assert bp.foundation is not None
    assert bp.foundation.result.floor_y == result.site["floorY"]
    assert bp.terrain.snapshot.source_bounds == AABB(Vec3(10, -3, 20), Vec3(49, 27, 59))
    assert all(r.status == "reachable" for r in check_building(
        output, result.layout, "1.21.11",
    ))
    assert bp.foundation.result.fill_cells == 0
    edited = plan(tmp_path, site={"terrain": str(source)}, overrides=[
        {"id": "ground", "at": [0, 0, 0], "block": "dirt"},
    ])
    assert built(edited)[1].get(Vec3(10, -3, 20)).id == "minecraft:dirt"


def test_height_edit_keeps_floor_count_and_headroom(tmp_path):
    result = plan(tmp_path, archetype="tower", elements={"tower#1": {"height": 17}})
    assert result.massing["params"]["floor_height"] == 6
    _, volume = built(result)
    assert all(check_route(volume, Vec3.from_seq(result.massing["entrance"]),
                           Vec3.from_seq(p), "1.21.11").status == "reachable"
               for p in result.massing["arrivals"])
