"""固定課題以外の様式・面積に対するDetailの品質回帰。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.critic import plan_from_metadata
from mcblueprint.detail import _recess_openings
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import generate, generate_regions
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.materialize import materialize
from mcblueprint.model import Vec3
from mcblueprint.operations.transform import Transform
from mcblueprint.pipeline import PipelineError, make
from mcblueprint.plan_contract import plan_buildings
from mcblueprint.planning import plan_buildspec


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_square_tower_normals_and_decoration_ids(tmp_path, angle):
    plan = plan_buildspec(resolve_buildspec({
        "minecraftVersion": "1.21.11", "archetype": "tower", "style": "japanese",
        "features": {"shape": "square"}, "elements": {"tower#1": {"angle": angle}},
    }, project_dir=tmp_path, user_config=tmp_path / "missing.yaml"))
    center = plan.site["offset"]
    for facade in plan_buildings(plan.massing)[0]["facades"]:
        normal = facade["normal"]
        assert sum(normal[i] * (facade["origin"][i] - center[i]) for i in (0, 2)) > 0
    operations = [item["operation"] for item in plan.detail["decorations"]]
    assert len(operations) == len(set(operations))


@pytest.mark.parametrize("radius", (5, 29, 61))
def test_square_facades_follow_corners_between_columns(tmp_path, radius):
    planned = plan_buildspec(resolve_buildspec({
        "minecraftVersion": "1.21.11", "archetype": "tower",
        "size": [160, 32, 160], "features": {"shape": "square"},
        "elements": {"tower#1": {"radius": radius}},
    }, project_dir=tmp_path, user_config=tmp_path / "missing.yaml"))
    critic = plan_from_metadata(planned.blueprint["metadata"]["planning"],
                                massing=planned.massing, offset=planned.site["offset"])
    offset = planned.site["offset"]
    assert all(max(abs(cell.pos.x - offset[0]), abs(cell.pos.z - offset[2])) == radius
               for face in critic.facades for cell in face.cells)


@pytest.fixture
def density_capacity():
    from mcblueprint import detail

    data = Path(detail.__file__).parent / "data/patterns"
    style = yaml.safe_load((data / "styles/modern.yaml").read_text(encoding="utf-8"))
    params = {"radius": 5, "shape": "round", "floors": 1, "floor_height": 5,
              "height": 4, "angle": 0}
    root = "tower#1"
    contract = {"columns": [], "beams": [], "facades": [{
        "id": f"{root}.facade.north", "normal": [0, 0, -1],
        "origin": [0, 1, -5], "uAxis": [1, 0, 0], "vAxis": [0, 1, 0],
        "from": [0, 1, -5], "to": [0, 4, -5], "openings": [],
    }]}
    modules = {name: detail._module(name) for name in style["modules"]["cornice"]}
    config = {"seed": 42, "decoration": {"density": 0.6, "depth": 0.5, "symmetry": 1}}
    low, high = style["decoration"]["density"]
    return ([], params, style, config, root, [], contract, modules,
            low + config["decoration"]["density"] * (high - low))


def test_round_modern_tower_reports_integer_density_capacity(density_capacity):
    from mcblueprint import detail

    with pytest.raises(BlueprintError, match="装飾の収容不足.*非開口面積=4") as error:
        detail._facade_selection(*density_capacity)
    diagnostic = error.value.diagnostic
    assert diagnostic["code"] == "detail_capacity"
    assert diagnostic["element"].startswith("tower#1.facade.")
    assert diagnostic["actual"]["area"] == 4
    assert diagnostic["expected"] == {"density": [0.05, 0.2]}
    assert diagnostic["repairPaths"] == ["elements.tower#1.radius",
                                          "elements.tower#1.floor_height", "size"]
    assert json.dumps(diagnostic, ensure_ascii=False) in str(error.value)


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
@pytest.mark.parametrize("archetype", ("house", "tower"))
def test_functional_openings_recede_and_keep_reservations(tmp_path, recwarn, archetype, angle):
    root = f"{archetype}#1"
    cells = []
    for depth in (0, 1):
        planned = plan_buildspec(resolve_buildspec({
            "minecraftVersion": "1.21.11", "archetype": archetype, "style": "japanese",
            "elements": {root: {"angle": angle}},
            "config": {"decoration": {"depth": depth}},
        }, project_dir=tmp_path, user_config=tmp_path / "missing.yaml"))
        if depth == 0:
            assert planned.diagnostics == ()
            assert planned.detail["diagnostics"] == []
        bp = load_blueprint_dict(materialize(
            planned.blueprint, style="japanese", palette=planned.palette,
        ).blueprint)
        offset = Vec3.from_seq(planned.site["offset"])
        reverse = Transform.rotation(-angle, Vec3(0, 0, 0))
        local = {}
        for region in generate_regions(bp):
            for pos, state in region.volume:
                if region.core.contains(pos):
                    local[reverse.apply(pos - offset)] = state, region.volume.attributes(pos)
        cells.append(local)
    before, after = cells
    params = plan_buildings(planned.massing)[0]["params"]
    blocked = {op["element"]: json.loads(op["comment"]) for op in planned.detail["operations"]
               if op.get("comment", "").startswith('{"code": "opening_recess_blocked"')}
    moved = 0
    for pos, (state, attrs) in before.items():
        if state.id != "minecraft:glass" and not state.id.endswith("_door"):
            continue
        if attrs.element in blocked:
            assert after[pos][0] == state
            assert replace(after[pos][1], operation=None) == replace(attrs, operation=None)
            diagnostic = blocked[attrs.element]
            assert diagnostic["actual"] == 0 and diagnostic["expected"] == 1
            assert "予約" in diagnostic["reason"]
            continue
        if archetype == "house":
            normal = (Vec3(0, 0, -1) if pos.z == 0 else
                      Vec3(0, 0, 1) if pos.z == params["depth"] - 1 else
                      Vec3(1, 0, 0) if pos.x == params["width"] - 1 else Vec3(-1, 0, 0))
        else:
            normal = (Vec3(0, 0, 1 if pos.z > 0 else -1) if abs(pos.z) > abs(pos.x)
                      else Vec3(1 if pos.x > 0 else -1, 0, 0))
        assert after[pos - normal][0] == state
        assert after[pos][0].id == "minecraft:air"
        moved += 1
        if state.id.endswith("_door"):
            assert after[pos - normal][1].reserved == "circulation"
            assert after[pos][1].reserved == "circulation"
    for pos, (state, attrs) in before.items():
        if attrs.reserved:
            assert (after[pos][0].id == "minecraft:air") == (state.id == "minecraft:air")
            assert after[pos][0].properties == state.properties
            assert replace(after[pos][1], operation=None) == replace(attrs, operation=None)
    assert moved > 0
    assert blocked
    assert {d["element"]: d for d in planned.diagnostics} == blocked
    assert list(planned.diagnostics) == planned.detail["diagnostics"]
    assert not recwarn


@pytest.mark.parametrize("face,position,facing", (
    ("north", [0, 1, -4], "south"), ("south", [0, 1, 4], "north"),
    ("east", [4, 1, 0], "west"), ("west", [-4, 1, 0], "east"),
))
@pytest.mark.parametrize("strength,depth", ((0, 0), (0.24, 0), (0.25, 1), (0.75, 2), (1, 2)))
def test_unreserved_door_recess_uses_style_layers(face, position, facing, strength, depth):
    original = {"type": "doorway", "position": position, "facing": facing,
                "door": "oak_door", "face": face, "id": "house#1.door",
                "element": "house#1.door", "reserved": "circulation",
                "owner": "house#1.door", "role": "window_frame", "source": "features.floors"}
    detail = [original.copy()]
    _recess_openings({"depth_layers": {"opening": [1, 2]}}, strength,
                     "house#1", [], detail, {"columns": [], "beams": []})
    if not depth:
        assert detail == [original]
        return
    assert "door" not in detail[0]
    sweep, door = detail[1]["operations"]
    inward = {"north": Vec3(0, 0, 1), "south": Vec3(0, 0, -1),
              "east": Vec3(-1, 0, 0), "west": Vec3(1, 0, 0)}[face]
    assert door["position"] == (Vec3.from_seq(position) + inward * depth).to_list()
    assert door["facing"] == facing and door["door"] == "oak_door"
    assert sweep["block"] == "air"
    assert detail[1]["owner"] == original["owner"]
    volume = generate(load_blueprint_dict({
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "recessed-door",
        "operations": detail,
    }))
    assert volume.get(Vec3.from_seq(position)).id == "minecraft:air"
    for y, half in ((0, "lower"), (1, "upper")):
        point = Vec3.from_seq(door["position"]) + Vec3(0, y, 0)
        assert volume.get(point).id == "minecraft:oak_door"
        assert volume.get(point).get("half") == half
        assert volume.attributes(point).owner == original["owner"]


def test_density_capacity_make_keeps_validation_exit_code(tmp_path, monkeypatch, density_capacity):
    from mcblueprint import detail, pipeline

    def reject_capacity(_resolved):
        return detail._facade_selection(*density_capacity)

    monkeypatch.setattr(pipeline, "plan_buildspec", reject_capacity)
    source = tmp_path / "capacity.yaml"
    source.write_text(yaml.safe_dump({
        "minecraftVersion": "1.21.11", "archetype": "tower", "style": "modern",
        "scale": "medium", "features": {"shape": "round", "floor_height": 5},
        "elements": {"tower#1": {"radius": 12}},
    }), encoding="utf-8")
    with pytest.raises(PipelineError) as error:
        make(source, output=tmp_path / "capacity.schem")
    assert error.value.code == 1
    assert '"code": "detail_capacity"' in str(error.value)
    assert "elements.tower#1.radius" in str(error.value)


@pytest.mark.parametrize("source", sorted(Path("evaluation/cases/structure-detail").glob("*.yaml")),
                         ids=lambda path: path.stem)
def test_frozen_structure_detail_cases_quality(tmp_path, source):
    result = make(source, output=tmp_path / f"result.{source.stem.rsplit('-', 1)[1]}")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, record["structuredDiagnostics"]
    assert record["metrics"]["column_continuity"]["value"] == 1.0
    assert record["metrics"]["flat_ratio"]["value"] <= 0.35
    assert record["metrics"]["facade_surface_missing"]["value"] == 0


@pytest.mark.parametrize("style", ("medieval", "japanese", "modern"))
@pytest.mark.parametrize("archetype,shape", (("house", None), ("tower", "round"),
                                            ("tower", "square")))
def test_styles_and_shapes_quality(tmp_path, style, archetype, shape):
    name = "single-house" if archetype == "house" else "standalone-tower"
    spec = yaml.safe_load(Path(f"tests/data/buildspec/{name}.yaml").read_text(encoding="utf-8"))
    spec["style"] = style
    if shape:
        spec["features"]["shape"] = shape
    source = tmp_path / "spec.yaml"
    source.write_text(yaml.safe_dump(spec), encoding="utf-8")
    result = make(source, output=tmp_path / "result.schem")
    record = json.loads(result.record.read_bytes())
    failures = [d for d in record["structuredDiagnostics"] if d["severity"] == "fail"]
    assert not failures
    assert result.code == 0


@pytest.mark.parametrize("archetype", ("house", "tower"))
@pytest.mark.parametrize("scale,width", (("medium", 21), ("large", 33)))
def test_wide_five_high_facades_quality(tmp_path, archetype, scale, width):
    # 性能課題の広いベイと階高5を、少ない階数で再現する。
    name = "single-house" if archetype == "house" else "standalone-tower"
    spec = yaml.safe_load(Path(f"tests/data/buildspec/{name}.yaml").read_text(encoding="utf-8"))
    spec.update(scale=scale, size=[64, 48, 64])
    spec["features"].update(floors=2, floor_height=5)
    params = {"width": width, "depth": width}
    if archetype == "tower":
        spec["features"]["shape"] = "square"
        params = {"radius": width // 2}
    spec["elements"] = {f"{archetype}#1": params}
    source = tmp_path / "spec.yaml"
    source.write_text(yaml.safe_dump(spec), encoding="utf-8")
    result = make(source, output=tmp_path / "result.schem")
    record = json.loads(result.record.read_bytes())
    assert result.code == 0, record["structuredDiagnostics"]
