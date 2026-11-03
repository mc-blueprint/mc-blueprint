"""固定課題以外の様式・面積に対するDetailの品質回帰。"""

import json
from pathlib import Path

import pytest
import yaml

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.critic import plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.pipeline import make
from mcblueprint.planning import plan_buildspec


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_square_tower_normals_and_decoration_ids(tmp_path, angle):
    plan = plan_buildspec(resolve_buildspec({
        "minecraftVersion": "1.21.11", "archetype": "tower", "style": "japanese",
        "features": {"shape": "square"}, "elements": {"tower#1": {"angle": angle}},
    }, project_dir=tmp_path, user_config=tmp_path / "missing.yaml"))
    center = plan.site["offset"]
    for facade in plan.massing["facades"]:
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


def test_round_modern_tower_reports_integer_density_capacity():
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
    with pytest.raises(BlueprintError, match="装飾の収容不足.*非開口面積=4"):
        detail._facade_selection([], params, style, config, root, [], contract, modules,
                                 low + config["decoration"]["density"] * (high - low))


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
