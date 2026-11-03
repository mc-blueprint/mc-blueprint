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
@pytest.mark.parametrize("angle", (90, 180, 270))
def test_planning_exports_expected_structure_and_facade_contract(tmp_path, archetype, shape, angle):
    from mcblueprint.operations.transform import Transform

    features = {"shape": shape} if shape else {}
    first = plan(tmp_path, archetype=archetype, features=features)
    second = plan(tmp_path, archetype=archetype, features=features,
                  elements={f"{archetype}#1": {"angle": angle}})
    contract = first.massing
    assert contract["columns"] and contract["beams"] and contract["facades"]
    assert [column["id"] for column in contract["columns"]] == [
        column["id"] for column in second.massing["columns"]
    ]
    assert [beam["id"] for beam in contract["beams"]] == [
        beam["id"] for beam in second.massing["beams"]
    ]
    rotation = Transform.rotation(angle, Vec3(0, 0, 0))
    for before, after in zip(contract["facades"], second.massing["facades"], strict=True):
        assert after["id"] == before["id"]
        for key in ("normal", "uAxis"):
            assert after[key] == rotation.apply(Vec3.from_seq(before[key])).to_list()
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

    from mcblueprint.critic import plan_from_metadata

    for result in (first, second):
        critic = plan_from_metadata(result.blueprint["metadata"]["planning"],
                                    massing=result.massing, offset=result.site["offset"])
        assert critic.decoration_operations


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
    components = [op for op in first.detail["operations"]
                  if op.get("source", "").startswith("style.modules.")]
    assert components
    assert all(op["onConflict"] == "skip" for op in components)
    assert first.detail["decorations"] == first.massing["decorations"]
    assert first.detail["decorations"] == first.blueprint["metadata"]["planning"]["decorations"]
    _, volume = built(first, style)
    declared = {item["operation"] for item in first.detail["decorations"]}
    actual = {volume.attributes(pos).operation for pos, _ in volume
              if volume.attributes(pos).role in ("trim", "window_frame")
              and (volume.attributes(pos).source or "").startswith("style.modules.")}
    assert actual and actual <= declared
    assert not any(volume.attributes(pos).operation in declared for pos, _ in volume
                   if volume.attributes(pos).role in ("frame", "roof", "floor"))
    assert first.detail == second.detail
    built(first, style)


def test_detail_density_depth_and_symmetry_settings(tmp_path):
    def components(**decoration):
        result = plan(tmp_path, config={"decoration": decoration})
        return [op for op in result.detail["operations"]
                if op.get("source", "").startswith("style.modules.")]

    default = components()
    sparse = components(density=0.0, symmetry=0.0)
    dense = components(density=1.0, symmetry=0.0)
    shallow = components(density=1.0, depth=0.0, symmetry=0.0)
    deep = components(density=1.0, depth=1.0, symmetry=1.0)
    assert default
    assert len(dense) >= len(sparse)
    assert len(shallow) < len(deep)
    assert deep == components(density=1.0, depth=1.0, symmetry=1.0)
    assert deep != components(density=1.0, depth=1.0, symmetry=0.0)


def test_unselected_window_frame_does_not_require_its_material_states(tmp_path):
    result = plan(tmp_path, materials={"window_frame": "gold_block"},
                  config={"decoration": {"density": 0}})
    _, volume = built(result)
    assert not any(state.id == "minecraft:gold_block" for _, state in volume)


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


@pytest.mark.parametrize("style", STYLES)
@pytest.mark.parametrize("shape", ("square", "round"))
def test_tower_detail_keeps_modules_at_original_size(tmp_path, style, shape):
    result = plan(tmp_path, archetype="tower", style=style, scale="medium", seed=29,
                  features={"shape": shape, "floor_height": 5},
                  elements={"tower#1": {"radius": 12}},
                  config={"decoration": {"density": 1, "depth": 1}})
    sources = {op.get("source") for op in result.detail["operations"]}
    assert "style.modules.entrance" in sources
    assert "style.modules.roof_edge" in sources
    assert sources & {"style.modules.bay", "style.modules.window"}
    _, volume = built(result, style)
    assert all(check_route(volume, Vec3.from_seq(result.massing["entrance"]),
                           Vec3.from_seq(point), "1.21.11").status == "reachable"
               for point in result.massing["arrivals"])


def test_detail_missing_module_is_diagnosed(tmp_path, monkeypatch):
    from mcblueprint import detail

    load = detail.components.load_component

    def missing(name, **kwargs):
        if name == "medieval.entry_portico":
            raise BlueprintError("見つかりません")
        return load(name, **kwargs)

    monkeypatch.setattr(detail.components, "load_component", missing)
    with pytest.raises(BlueprintError, match="Detail: 必須部品 medieval.entry_portico"):
        plan(tmp_path)


def test_small_facade_reports_density_that_has_no_feasible_integer_count():
    import math

    import yaml

    from mcblueprint.critic import CriticAccumulator, plan_from_metadata
    from mcblueprint.detail import _facade_selection
    from mcblueprint.operations.transform import Transform

    root = "house#1"
    element = f"{root}.facade.north.floor#1"
    contract = {"columns": [], "beams": [], "facades": [{
        "id": element, "normal": [0, 0, -1], "origin": [0, 3, 0],
        "uAxis": [1, 0, 0], "vAxis": [0, 1, 0],
        "from": [0, 3, 0], "to": [0, 3, 0], "openings": [],
    }]}
    params = {"width": 5, "depth": 5, "floors": 1, "floor_height": 4, "angle": 0}
    data = Path("src/mcblueprint/data/patterns")
    style = yaml.safe_load((data / "styles/medieval.yaml").read_text(encoding="utf-8"))
    modules = {"medieval.stone_cornice": json.loads(
        (data / "modules/medieval/stone_cornice.json").read_text(encoding="utf-8"))}
    config = {"seed": 0, "decoration": {"density": 0.6, "depth": 0.5, "symmetry": 1}}
    operations, decorations = _facade_selection(
        [], params, style, config, root, [], contract, modules, 0.32,
    )
    transform = Transform.identity()
    for item in decorations:
        item["operation"] = f"/{root}@:{transform.rows}:{transform.offset}/{item['operation']}"
    metadata = {"element": root, "params": params, **contract, "decorations": decorations}
    metadata["columns"] = [{"id": "test.column", "owner": "test.frame",
                            "from": [2, 0, 0], "to": [2, 1, 0]}]
    metadata["beams"] = [{"id": "test.beam", "owner": "test.frame",
                          "from": [2, 1, 0], "to": [3, 1, 0]}]
    frame = {"type": "translate", "offset": [0, 0, 0], "role": "frame",
             "element": "test.frame", "owner": "test.frame", "reserved": "structure",
             "operations": [
                 {"type": "fill", "from": [2, 0, 0], "to": [2, 1, 0], "block": "stone"},
                 {"type": "fill", "from": [2, 1, 0], "to": [3, 1, 0], "block": "stone"},
             ]}
    concrete = materialize({
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": root, "seed": 0,
        "operations": [{"type": "translate", "id": root, "offset": [0, 0, 0],
                        "operations": [frame, *operations]}],
    })
    accumulator = CriticAccumulator(
        plan_from_metadata(metadata, decoration_range=(0.2, 0.4)), max_memory_mb=16,
    )
    for region in generate_regions(load_blueprint_dict(concrete.blueprint)):
        accumulator.consume(region)
    result = accumulator.finalize()
    measured = result.metrics["decoration_density"][f"{element}:north"]
    assert measured == {"value": 100.0, "numerator": 1, "denominator": 1}
    assert math.ceil(0.2 * measured["denominator"]) > math.floor(0.4 * measured["denominator"])
    assert any(d["code"] == "E202" and d["actual"] == 100.0 for d in result.diagnostics)


def test_detail_keeps_opening_projections_and_explicit_materials(tmp_path):
    from mcblueprint.operations.registry import build_operation
    from mcblueprint.operations.transform import Transform

    result = plan(tmp_path, features={"floors": 2}, materials={
        "frame": "oak_log", "trim": "stone_bricks", "window_frame": "birch_log",
    }, config={"decoration": {"density": 1, "depth": 1}})
    _, volume = built(result)
    transform = Transform.translation(Vec3.from_seq(result.site["offset"]))
    for op in result.detail["operations"]:
        if op["type"] not in ("window", "doorway"):
            continue
        mask = transform.bounds(build_operation(op, "test").bounds())
        axis = 0 if op["face"] in ("north", "south") else 2
        for pos, _ in volume:
            attrs = volume.attributes(pos)
            if attrs.face == op["face"] and (attrs.source or "").startswith("style.modules."):
                assert not (mask.min[axis] <= pos[axis] <= mask.max[axis]
                            and mask.min.y <= pos.y <= mask.max.y)
    seen = set()
    for pos, state in volume:
        attrs = volume.attributes(pos)
        if not (attrs.source or "").startswith("style.modules."):
            continue
        if attrs.role == "frame":
            assert state.id == "minecraft:oak_log"
            seen.add("frame")
        elif attrs.role == "trim":
            assert state.id in ("minecraft:stone_bricks", "minecraft:stone_brick_stairs",
                                "minecraft:stone_brick_slab")
            seen.add("trim")
        elif attrs.role == "window_frame":
            assert state.id == "minecraft:birch_log"
            seen.add("window_frame")
    assert seen == {"frame", "trim", "window_frame"}


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
