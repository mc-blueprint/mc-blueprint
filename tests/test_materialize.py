"""材質選択・形状表・既存Operationへの接続を検証する。"""

import copy
import json
from pathlib import Path

import pytest
import yaml

from mcblueprint.blockdata import load_block_data, supported_versions
from mcblueprint.colors import face_linear, linear_to_oklab, oklab_to_linear
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.materialize import materialize
from mcblueprint.model.vec import Vec3
from mcblueprint.validator import validate


@pytest.mark.parametrize("version", supported_versions())
@pytest.mark.parametrize("format", ("schem", "litematic"))
@pytest.mark.parametrize("fixture", ("single-house", "standalone-tower"))
def test_fixed_cases_make_palette_relation(tmp_path, version, format, fixture):
    from mcblueprint.pipeline import make

    source = Path(__file__).parent / "data/buildspec" / f"{fixture}.yaml"
    spec = yaml.safe_load(source.read_text(encoding="utf-8"))
    spec["minecraftVersion"] = version
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
    result = make(path, output=tmp_path / f"result.{format}",
                  overrides=[f"output.format={format}"], project_dir=tmp_path,
                  user_config=tmp_path / "missing.yaml")
    record = json.loads(result.record.read_text(encoding="utf-8"))
    assert record["metrics"]["palette_relation"]["value"], record["structuredDiagnostics"]
    assert result.code == 0, result.diagnostics


def test_fixed_house_retry_seed_reproduction_and_variation(tmp_path):
    from mcblueprint.buildspec import resolve_buildspec
    from mcblueprint.planning import plan_buildspec

    source = Path(__file__).parent / "data/buildspec/single-house.yaml"
    spec = yaml.safe_load(source.read_text(encoding="utf-8"))
    spec["minecraftVersion"] = "26.3"
    planned = plan_buildspec(resolve_buildspec(
        spec, project_dir=tmp_path, user_config=tmp_path / "missing.yaml",
    ))
    result = materialize(planned.blueprint, palette=planned.palette, seed=42)
    assert 1 < result.attempts <= 20
    assert not result.diagnostics
    assert result == materialize(planned.blueprint, palette=planned.palette, seed=42)
    other = materialize(planned.blueprint, palette=planned.palette, seed=99)
    assert result.roles != other.roles


def ref(role, shape="full", **state):
    return {"role": role, "shape": shape, "state": state}


def plan(*operations, version="1.21.11"):
    return {"formatVersion": 1, "minecraftVersion": version, "name": "materialize",
            "operations": list(operations)}


def cells(*roles):
    return plan(*({"type": "set", "position": [i, 0, 0], "block": ref(role, shape)}
                  for i, (role, shape) in enumerate(roles)))


@pytest.mark.parametrize("version", supported_versions())
def test_family_shapes_and_fixed_id(version):
    data = cells(("wall_main", "full"), ("wall_main", "stairs"),
                 ("wall_main", "slab"), ("wall_main", "wall"))
    data["minecraftVersion"] = version
    result = materialize(data, palette={"materials": {"wall_main": "minecraft:stone_bricks"}})
    registry = load_block_data(version)
    assert registry.families["stone_bricks"] == {
        shape: state.id for shape, state in result.roles["wall_main"].items()}
    assert not result.blueprint["palettes"]
    assert validate(result.blueprint) == []
    assert registry.get("minecraft:stone_bricks").weathering["crack"] == (
        "minecraft:cracked_stone_bricks")


@pytest.mark.parametrize("version", supported_versions())
def test_shape_state_and_walking_rejection(version):
    data = cells(("wall_main", "stairs"))
    data["minecraftVersion"] = version
    with pytest.raises(BlueprintError, match="必要形状"):
        materialize(data, palette={"materials": {"wall_main": "minecraft:glass"}})
    data["operations"][0]["block"]["state"] = {"half": "invalid"}
    with pytest.raises(BlueprintError, match="状態"):
        materialize(data)
    floor = cells(("floor", "full"))
    floor["minecraftVersion"] = version
    with pytest.raises(BlueprintError, match="歩行"):
        materialize(floor, palette={"materials": {"floor": "minecraft:white_concrete"}})
    walk = materialize(floor, palette={"materials": {"floor": "stone"}})
    assert validate(walk.blueprint) == []
    slab = cells(("floor", "slab"))
    with pytest.raises(BlueprintError, match="歩行"):
        materialize(slab)
    tower = plan({"type": "tower", "shape": "square", "position": [0, 0, 0],
                  "size": 5, "height": 3, "wall": ref("wall_main"), "stairs": False},
                 version=version)
    with pytest.raises(BlueprintError, match="歩行"):
        materialize(tower, palette={"materials": {"wall_main": "minecraft:white_concrete"}})


def test_seed_reproduction_variation_range_and_explicit_color():
    data = cells(("wall_main", "full"))
    before = copy.deepcopy(data)
    results = [materialize(data, seed=seed) for seed in (0, 1, 2, 42, 99)]
    assert results[0] == materialize(data, seed=0)
    assert results[3].blueprint["seed"] == 42
    assert len({r.roles["wall_main"]["full"].id for r in results}) > 1
    zero = [materialize(data, seed=seed, palette={"variation": 0}) for seed in range(5)]
    assert len({r.roles["wall_main"]["full"].id for r in zero}) == 1
    wide = [materialize(data, seed=seed, palette={"variation": 1}) for seed in range(12)]
    assert len({r.roles["wall_main"]["full"].id for r in wide}) > 1
    assert data == before
    bright = materialize(data, palette={"materials": {"wall_main": {
        "material": "concrete", "color": [0.95, 0, 0]}}})
    dark = materialize(data, palette={"materials": {"wall_main": {
        "material": "concrete", "color": [0.1, 0, 0]}}})
    assert bright.roles != dark.roles
    color_only = materialize(data, palette={"materials": {"wall_main": {"color": [0.6, 0, 0]}}})
    assert load_block_data("1.21.11").get(color_only.roles["wall_main"]["full"].id).material in (
        "brick", "stone")


def test_fixed_roles_remain_fixed_after_twenty_attempts():
    data = cells(("wall_main", "full"), ("roof", "full"))
    result = materialize(data, palette={"materials": {
        "wall_main": "minecraft:stone", "roof": "minecraft:stone"}})
    assert result.attempts == 20
    assert result.diagnostics[0]["code"] == "palette_relation"
    assert result.diagnostics[0]["candidates"] == {"wall_main": 1, "roof": 1}
    assert result.roles["roof"]["full"].id == "minecraft:stone"
    assert result.fixed_roles == frozenset({"wall_main", "roof"})


def test_fixed_id_without_face_colors_is_kept_and_diagnosed():
    result = materialize(cells(("wall_main", "full"), ("roof", "full")),
                         palette={"materials": {
                             "wall_main": "minecraft:air", "roof": "minecraft:stone"}})
    assert result.roles["wall_main"]["full"].id == "minecraft:air"
    assert result.diagnostics[0]["metric"] == "color_unavailable"
    assert validate(result.blueprint) == []


def test_retries_return_smallest_observed_violation(monkeypatch):
    import importlib

    module = importlib.import_module("mcblueprint.materialize")
    observed = []

    def impossible(selected, contrast, saturation):
        gap = abs(selected["wall_main"][2][2][0] - selected["roof"][2][2][0])
        error = {"code": "palette_relation", "pair": ["wall_main", "roof"],
                 "metric": "dL", "actual": gap, "minimum": 2, "violation": 2 - gap}
        observed.append(error)
        return [error]

    monkeypatch.setattr(module, "_violations", impossible)
    result = materialize(cells(("wall_main", "full"), ("roof", "full")), seed=42,
                         palette={"variation": 1, "materials": {
                             "wall_main": "minecraft:stone", "roof": "wood"}})
    assert result.attempts == 20
    assert len(observed) >= result.attempts
    assert len({row["actual"] for row in observed}) > 1
    assert result.diagnostics[0]["violation"] == min(row["violation"] for row in observed)


def test_accent_mapping_and_walking_slot_with_custom_role():
    data = cells(("accent", "full"))
    low = materialize(data, style={"roles": {"accent": "concrete"}}, palette={"accent": 0})
    high = materialize(data, style={"roles": {"accent": "concrete"}}, palette={"accent": 1})
    assert low.max_area_ratios == {"accent": 0}
    assert high.max_area_ratios == {"accent": 0.08}
    assert low.blueprint != high.blueprint
    floor = plan({"type": "floor", "from": [0, 0, 0], "to": [2, 0, 2],
                  "block": ref("custom")})
    with pytest.raises(BlueprintError, match="歩行"):
        materialize(floor, style={"roles": {"custom": "minecraft:white_concrete"}})
    floor["operations"][0].pop("block")
    floor["operations"][0]["palette"] = "walking"
    floor["palettes"] = {"walking": [{"block": ref("custom"), "weight": 1}]}
    with pytest.raises(BlueprintError, match="歩行"):
        materialize(floor, style={"roles": {"custom": "minecraft:white_concrete"}})


@pytest.mark.parametrize("mix", [0, 0.4, 1])
def test_mix_boundaries_and_cell_role(mix):
    result = materialize(cells(("wall_main", "full")), palette={"mix": mix})
    assert validate(result.blueprint) == []
    volume = generate(load_blueprint_dict(result.blueprint))
    assert volume.attributes(Vec3(0, 0, 0)).role == "wall_main"


def test_resolve_color_only_and_roof_full_shape(tmp_path):
    from mcblueprint.buildspec import resolve_buildspec

    resolved = resolve_buildspec(
        {"minecraftVersion": "1.21.11", "materials": {"roof": {"color": [0.7, 0, 0]}}},
        project_dir=tmp_path, user_config=tmp_path / "absent.yaml",
    )
    data = plan({"type": "roof", "from": [0, 0, 0], "to": [4, 0, 4],
                 "block": ref("roof")})
    result = materialize(data, palette=resolved.config["palette"])
    assert set(result.roles["roof"]) == {"full"}
    assert validate(result.blueprint) == []


@pytest.mark.parametrize("version", supported_versions())
def test_building_mixing_and_oriented_roof(version):
    data = plan({"type": "building", "position": [3, 0, 5], "angle": 90,
                 "width": 7, "depth": 9, "floors": 2,
                 "wall": ref("wall_main"), "floor": ref("floor"),
                 "roof": ref("roof", "stairs"), "stairs": ref("floor", "stairs"),
                 "window": "minecraft:glass", "door": "minecraft:oak_door"}, version=version)
    result = materialize(data, seed=42, palette={"mix": 0.4})
    assert {"stairs", "slab"} <= result.roles["roof"].keys()
    assert validate(result.blueprint) == []
    volume = generate(load_blueprint_dict(result.blueprint))
    ids = {state.id for _, state in volume}
    assert len([name for name in ids if name.endswith("_stairs")]) >= 2
    assert all(state.get("half") == "bottom" for _, state in volume
               if state.id.endswith("_stairs"))
    for role in ("wall_main", "roof"):
        entries = [entries for name, entries in result.blueprint["palettes"].items()
                   if name.startswith(f"materialize_{role}_")]
        assert entries and all(len(e) == 2 for e in entries)


def test_unknown_role_and_state_rejected():
    with pytest.raises(BlueprintError, match="未知のrole"):
        materialize(cells(("unknown", "full")))
    data = cells(("wall_main", "full"))
    data["operations"][0]["block"]["state"] = {"nonexistent_property": "north"}
    with pytest.raises(BlueprintError, match="状態"):
        materialize(data)


def test_tint_linear_multiplication_and_quantiles():
    registry = load_block_data("1.21.11")
    grass = registry.get("minecraft:grass_block")
    face = grass.faces["top"]
    for quantile in range(5):
        source = oklab_to_linear(face["tintedOklab"][axis][quantile] for axis in ("L", "a", "b"))
        tint = oklab_to_linear(registry.tints["grass"]["swamp"])
        expected = tuple(a * b for a, b in zip(source, tint, strict=True))
        assert face_linear(grass, face, registry, "swamp", quantile) == pytest.approx(expected)
    assert face_linear(grass, face, registry, "swamp") != face_linear(grass, face, registry)
    rgb = (0.2, 0.4, 0.7)
    assert oklab_to_linear(linear_to_oklab(rgb)) == pytest.approx(rgb, abs=1e-6)
    fixed = next(info for info in registry.blocks() if info.tint == "fixed" and info.faces)
    assert face_linear(fixed, next(iter(fixed.faces.values())), registry)


def test_concrete_examples_are_unchanged_and_role_components_are_materialized():
    for path in Path("examples").glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if "minecraftVersion" in raw:
            result = materialize(raw)
            if path.stem == "gatehouse":
                assert result.roles
                assert validate(result.blueprint) == []
            else:
                assert result.blueprint == raw


@pytest.mark.parametrize("version", supported_versions())
def test_tower_and_pattern_slots(version):
    data = plan(
        {"type": "tower", "shape": "square", "position": [0, 0, 0], "size": 9,
         "height": 9, "wall": ref("wall_main"), "floor": ref("floor"),
         "stairs": {"block": ref("floor", "stairs"), "column": ref("frame")},
         "battlement": {"block": ref("wall_main", "wall")}},
        {"type": "arch", "position": [12, 0, 0], "axis": "x", "width": 3, "height": 4,
         "block": ref("wall_main"), "trim": ref("wall_main", "stairs")},
        {"type": "pillar", "position": [20, 0, 0], "height": 5, "block": ref("frame"),
         "base": ref("wall_main"), "cap": ref("wall_main", "slab", type="top")},
        version=version,
    )
    result = materialize(data)
    assert validate(result.blueprint) == []
    assert generate(load_blueprint_dict(result.blueprint)).bounds() is not None


def test_role_module_palette_and_nested_component(tmp_path):
    from mcblueprint import components
    from mcblueprint.pattern_checking import check_patterns

    root = tmp_path / "patterns"
    modules = root / "modules" / "custom"
    modules.mkdir(parents=True)
    (root / "styles").mkdir()
    (root / "styles" / "custom.yaml").write_text(
        "formatVersion: 1\nname: custom\ngrid:\n  bay: [3, 5]\n  story_height: [4, 5]\n"
        "modules: {}\nroles: {frame: wood}\n", encoding="utf-8",
    )
    (root / "sources.yaml").write_text("formatVersion: 1\nsources: []\n", encoding="utf-8")
    child = {"formatVersion": 1, "name": "custom.child",
             "palettes": {"local": [{"block": ref("frame"), "weight": 1}]},
             "operations": [{"type": "fill", "from": [0, 0, 0], "to": [2, 0, 2],
                             "palette": "local"}]}
    parent = {"formatVersion": 1, "name": "custom.parent", "operations": [
        {"type": "component", "name": "custom.child", "position": [0, 0, 0], "rotation": 90}]}
    for name, raw in (("child", child), ("parent", parent)):
        (modules / f"{name}.json").write_text(json.dumps(raw), encoding="utf-8")
    results = check_patterns(root)
    for row in results:
        assert row.errors == []
    with components.component_search_paths((), module_paths=(root / "modules",)):
        result = materialize(plan(parent["operations"][0]), style={"roles": {"frame": "wood"}})
        assert validate(result.blueprint) == []
        assert len(generate(load_blueprint_dict(result.blueprint))) == 9
    child["palettes"]["local"][0]["block"]["role"] = "unknown"
    (modules / "child.json").write_text(json.dumps(child), encoding="utf-8")
    assert any(not row.ok and row.kind == "module" for row in check_patterns(root))


@pytest.mark.parametrize("version", supported_versions())
@pytest.mark.parametrize("parent_role", [True, False])
@pytest.mark.parametrize("override", [None, "concrete", "role"])
def test_component_palette_inheritance_and_shadowing(tmp_path, version, parent_role, override):
    from mcblueprint import components

    parent = {
        "formatVersion": 1, "name": "parent",
        "palettes": {"local": [{"block": ref("frame") if parent_role else "minecraft:oak_log"}]},
        "operations": [
            {"type": "component", "name": "leaf", "position": [0, 0, 0]},
            {"type": "component", "name": "child", "position": [3, 0, 0]},
        ],
    }
    child = {"formatVersion": 1, "name": "child", "operations": [
        {"type": "component", "name": "leaf", "position": [0, 0, 0]}]}
    if override:
        child["palettes"] = {"local": [{
            "block": ref("wall_main") if override == "role" else "minecraft:stone"}]}
    leaf = {"formatVersion": 1, "name": "leaf", "operations": [
        {"type": "set", "position": [0, 0, 0], "palette": "local"}]}
    for name, raw in (("parent", parent), ("child", child), ("leaf", leaf)):
        (tmp_path / f"{name}.json").write_text(json.dumps(raw), encoding="utf-8")
    data = plan({"type": "component", "name": "parent", "position": [10, 0, 5]},
                version=version)
    with components.component_search_paths((tmp_path,)):
        result = materialize(data, palette={"materials": {
            "frame": "minecraft:oak_log", "wall_main": "minecraft:stone_bricks"}})
        assert validate(result.blueprint) == []
        volume = generate(load_blueprint_dict(result.blueprint))
    assert volume.get(Vec3(10, 0, 5)).id == "minecraft:oak_log"
    expected = {None: "minecraft:oak_log", "concrete": "minecraft:stone",
                "role": "minecraft:stone_bricks"}[override]
    assert volume.get(Vec3(13, 0, 5)).id == expected
    assert volume.attributes(Vec3(10, 0, 5)).role == ("frame" if parent_role else None)
    role = "wall_main" if override == "role" else (
        "frame" if override is None and parent_role else None)
    assert volume.attributes(Vec3(13, 0, 5)).role == role


@pytest.mark.parametrize("version", supported_versions())
@pytest.mark.parametrize("shape", ["round", "square"])
@pytest.mark.parametrize("explicit_step", [True, False])
def test_tower_column_is_not_walking_but_rooftop_is(version, shape, explicit_step):
    tower = {
        "type": "tower", "shape": shape, "position": [0, 0, 0], "height": 9,
        "wall": ref("wall_main"), "floor": ref("floor"),
        "stairs": {"column": ref("frame")},
    }
    tower["radius" if shape == "round" else "size"] = 4 if shape == "round" else 9
    if explicit_step:
        tower["stairs"]["block"] = ref("floor", "stairs")
    materials = {"wall_main": "minecraft:stone_bricks", "floor": "minecraft:oak_planks",
                 "frame": "minecraft:oak_log"}
    result = materialize(plan(tower, version=version), palette={"materials": materials})
    assert validate(result.blueprint) == []
    volume = generate(load_blueprint_dict(result.blueprint))
    assert volume.get(Vec3(0, 1, 0)).id == "minecraft:oak_log"
    assert volume.attributes(Vec3(0, 1, 0)).role == "frame"
    assert volume.get(Vec3(3, 10, 0)).id == "minecraft:stone_bricks"
    materials["wall_main"] = "minecraft:white_concrete"
    with pytest.raises(BlueprintError, match="materials.wall_main.*歩行"):
        materialize(plan(tower, version=version), palette={"materials": materials})


@pytest.mark.parametrize("second", [ref("frame"), "minecraft:stone"])
def test_mixed_role_palette_is_explicitly_rejected(second):
    from mcblueprint.pattern_checking import _module_registry, _schema, _validate

    module = {"formatVersion": 1, "name": "custom.mixed",
              "palettes": {"mixed": [{"block": ref("wall_main"), "weight": 1},
                                      {"block": second, "weight": 2}]},
              "operations": [{"type": "fill", "from": [0, 0, 0], "to": [4, 0, 4],
                              "palette": "mixed"}]}
    schema = _schema("pattern-module.schema.json")
    assert _validate(module, schema, _module_registry(schema)) == []
    with pytest.raises(BlueprintError, match="palettes.mixed.*未対応"):
        materialize({**module, "minecraftVersion": "1.21.11"})


def test_same_role_palette_preserves_cell_role():
    data = plan({"type": "fill", "from": [0, 0, 0], "to": [4, 0, 4], "palette": "same"})
    data["palettes"] = {"same": [{"block": ref("frame"), "weight": 1},
                                 {"block": ref("frame"), "weight": 2}]}
    result = materialize(data)
    assert validate(result.blueprint) == []
    volume = generate(load_blueprint_dict(result.blueprint))
    assert all(volume.attributes(pos).role == "frame" for pos, _ in volume)


@pytest.mark.parametrize("version", supported_versions())
@pytest.mark.parametrize("state", ["half=invalid", "unknown=invalid"])
def test_fixed_state_is_validated_before_family_shape_lookup(version, state):
    data = cells(("wall_main", "full"))
    data["minecraftVersion"] = version
    with pytest.raises(BlueprintError, match="materials.wall_main.*固定IDの状態"):
        materialize(data, palette={"materials": {
            "wall_main": f"minecraft:oak_stairs[{state}]"}})
    result = materialize(data, palette={"materials": {
        "wall_main": "minecraft:oak_stairs[half=top]"}})
    assert result.roles["wall_main"]["full"].id == "minecraft:oak_planks"
    assert validate(result.blueprint) == []


@pytest.mark.parametrize("version", supported_versions())
@pytest.mark.parametrize("source", ["root", "component"])
@pytest.mark.parametrize("palette_use", ["none", "inherited", "shadowed"])
def test_unrelated_rotated_components_keep_four_level_nesting(
    tmp_path, version, source, palette_use,
):
    from mcblueprint import components

    block = "minecraft:oak_log[axis=x]"
    for level in range(4):
        operation = {"type": "component", "name": f"chain{level + 1}",
                     "position": [1, 2, 3], "rotation": 90} if level < 3 else {
                         "type": "set", "position": [1, 0, 0], "block": block}
        raw = {"formatVersion": 1, "name": f"chain{level}", "operations": [operation]}
        if level == 3 and palette_use != "none":
            operation.pop("block")
            operation["palette"] = "local" if palette_use == "shadowed" else "concrete"
            if palette_use == "shadowed":
                raw["palettes"] = {"local": [{"block": block}]}
        (tmp_path / f"chain{level}.json").write_text(json.dumps(raw), encoding="utf-8")
    chain = {"type": "component", "name": "chain0", "position": [3, 0, 1], "rotation": 90}
    role_name = "local" if palette_use == "shadowed" else "unused"
    palettes = {role_name: [{"block": block}]}
    if palette_use == "inherited":
        palettes["concrete"] = [{"block": block}]
    container = {"formatVersion": 1, "name": "container", "palettes": palettes,
                 "operations": [chain]}
    data = plan(chain, version=version)
    if source == "component":
        (tmp_path / "container.json").write_text(json.dumps(container), encoding="utf-8")
        data["operations"] = [{"type": "component", "name": "container",
                               "position": [0, 0, 0]}]
    else:
        data["palettes"] = palettes
    with components.component_search_paths((tmp_path,)):
        assert validate(data) == []
        before = generate(load_blueprint_dict(data))
        palettes[role_name][0]["block"] = ref("frame")
        if source == "component":
            (tmp_path / "container.json").write_text(json.dumps(container), encoding="utf-8")
        result = materialize(data, palette={"materials": {"frame": block}})
        assert validate(result.blueprint) == []
        after = generate(load_blueprint_dict(result.blueprint))
    assert dict(after) == dict(before)
    retained = result.blueprint["operations"][0]
    if source == "component":
        retained = retained["operations"][0]
    if source == "component" and palette_use == "inherited":
        assert retained["type"] == "translate"
        assert retained["rotation"] == 90
    else:
        assert retained == chain
