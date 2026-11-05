"""H07共有契約の複数棟・版・敷地規則の固定ケース。"""

import copy
import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from mcblueprint import pipeline, planning
from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.critic import CriticAccumulator, plan_from_metadata
from mcblueprint.edits import apply_edits
from mcblueprint.errors import BlueprintError
from mcblueprint.layouts import load_layout, validate_layout
from mcblueprint.materialize import materialize_plan
from mcblueprint.model import BlockState, Vec3
from mcblueprint.pattern_checking import check_patterns
from mcblueprint.plan_contract import hierarchy, plan_buildings
from mcblueprint.planning import plan_buildspec as single_plan
from mcblueprint.validator import validate_and_load
from mcblueprint.viewer_input import _spec_sources


def composite(resolved, kinds=("house", "tower")):
    """後続plannerの代わりに既存の単独計画を、重ならない2敷地で合成する。"""
    plans, buildings = [], []
    counts = {}
    for index, kind in enumerate(kinds):
        counts[kind] = counts.get(kind, 0) + 1
        root = f"{kind}#{counts[kind]}"
        raw = {"minecraftVersion": "1.21.11", "archetype": kind,
               "size": [24 if index == 0 else 64, 32, 24 if index == 0 else 64],
               "seed": resolved.buildspec["seed"], "config": resolved.config,
               "elements": {f"{kind}#1": resolved.buildspec.get("elements", {}).get(root, {})}}
        plan = single_plan(resolve_buildspec(raw))
        if root != f"{kind}#1":
            def rename(value, kind=kind, root=root):
                return json.loads(json.dumps(value).replace(f"{kind}#1", root))

            plan = replace(plan, blueprint=rename(plan.blueprint), massing=rename(plan.massing))
        plans.append(plan)
        buildings.extend(plan_buildings(plan.massing))
    contract = hierarchy("town", buildings)
    blueprint = copy.deepcopy(plans[0].blueprint)
    blueprint["operations"] = [op for p in plans for op in p.blueprint["operations"]]
    blueprint["palettes"] = {name: value for p in plans
                              for name, value in p.blueprint.get("palettes", {}).items()}
    blueprint["metadata"]["planning"] = copy.deepcopy(contract)
    return replace(plans[0], blueprint=blueprint, massing={**contract, "stage": "massing"},
                   site={**plans[0].site, **contract, "stage": "site"},
                   detail={**plans[0].detail, **contract, "stage": "detail",
                           "diagnostics": [d for p in plans for d in p.detail["diagnostics"]]})


@pytest.fixture
def resolved(tmp_path):
    return resolve_buildspec({"minecraftVersion": "1.21.11", "seed": 42,
                              "config": {"preview": {"enabled": False}}},
                             project_dir=tmp_path, user_config=tmp_path / "missing")


@pytest.mark.parametrize("kinds", [("house", "house"), ("house", "tower"), ("tower", "tower")])
def test_composite_ids_sources_routes_and_missing_building(resolved, kinds, monkeypatch):
    plan = composite(resolved, kinds)
    again = composite(resolved, kinds)
    assert plan.massing == again.massing
    buildings = plan_buildings(plan.massing)
    assert len({b["element"] for b in buildings}) == 2
    concrete = materialize_plan(plan.blueprint, palette=plan.palette)
    assert materialize_plan(plan.blueprint, palette=plan.palette, seed=123).blueprint["seed"] == 123
    assert set(concrete.building_results) == {b["element"] for b in buildings}
    assert concrete.selection_sources == materialize_plan(
        again.blueprint, palette=again.palette).selection_sources
    bp, errors = validate_and_load(concrete.blueprint)
    assert not errors
    critic = plan_from_metadata(bp.metadata["planning"], massing=plan.massing)
    accumulator = CriticAccumulator(critic)
    from mcblueprint.generator import generate_regions

    missing_root = buildings[1]["element"]
    for region in generate_regions(bp):
        # 2棟目の全セルを欠損させ、1棟目の成功で相殺されないことを確認する。
        for point, _ in list(region.volume):
            attr = region.volume.attributes(point)
            if (attr.element or "").startswith(missing_root + "."):
                region.volume.set(point, BlockState.of("air"), attr)
        accumulator.consume(region)
    result = accumulator.finalize()
    assert any(d["code"] == "E101" and any(
        v["owner"].startswith(missing_root + ".") for v in d["violations"])
               for d in result.diagnostics)
    assert any(d["code"] == "E203" and d["element"].startswith(missing_root + ".")
               for d in result.diagnostics)
    scene = {"sources": []}
    _spec_sources(scene, resolved, massing=plan.massing)
    assert {s["element"] for s in scene["sources"]} == {b["element"] for b in buildings}


@pytest.mark.parametrize("stage", pipeline.STAGES)
def test_composite_all_resume_and_param_edits(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(pipeline, "plan_buildspec", composite)
    monkeypatch.setattr(planning, "plan_buildspec", composite)
    source = tmp_path / "input.yaml"
    raw = {"minecraftVersion": "1.21.11", "seed": 42,
           "config": {"preview": {"enabled": False},
                      "palette": {"materials": {"wall_main": "minecraft:stone"}}}}
    source.write_text(yaml.safe_dump(raw), encoding="utf-8")
    first = pipeline.make(source, output=tmp_path / "first.schem", emit=[stage])
    second = pipeline.make(source, output=tmp_path / "second.schem",
                           resume=tmp_path / f"first.{stage}.json")
    assert first.code == second.code
    assert gzip.decompress(first.output.read_bytes()) == gzip.decompress(second.output.read_bytes())
    record = json.loads(second.record.read_bytes())
    original_record = json.loads(first.record.read_bytes())
    assert record["metrics"] == original_record["metrics"]
    assert record["structuredDiagnostics"] == original_record["structuredDiagnostics"]
    assert not any(d["code"] == "E305" and d.get("role") == "wall_main"
                   for d in record["structuredDiagnostics"])
    assert {r["element"] for r in record["metrics"]["walkability"]["routes"]} == {
        "house#1", "tower#1"}
    scene_bytes = gzip.decompress((tmp_path / "second.scene.json.gz").read_bytes())
    assert scene_bytes == gzip.decompress((tmp_path / "first.scene.json.gz").read_bytes())
    scene = json.loads(scene_bytes)
    for root in ("house#1", "tower#1"):
        assert any(s.get("element") == root and "floor_height" in s.get("params", [])
                   for s in scene["sources"])
    edits = {"formatVersion": 1, "scene": "0" * 64, "edits": [
        {"id": "height", "kind": "param", "element": "tower#1",
         "param": "floor_height", "value": 5}]}
    candidate = apply_edits(scene, edits, {"formatVersion": 1, "archetype": "house", **raw})
    assert candidate["elements"] == {"tower#1": {"floor_height": 5}}
    assert "elements" not in raw
    # sceneが許可しても、現在の計画に存在しないIDは適用しない。
    scene["sources"].append({"id": 9999, "element": "tower#99", "params": ["height"]})
    edits["edits"][0].update(element="tower#99", param="height")
    with pytest.raises(BlueprintError):
        apply_edits(scene, edits, {"formatVersion": 1, "archetype": "house", **raw})


def test_old_checkpoint_is_rejected_with_regeneration_guide(tmp_path):
    source = tmp_path / "input.yaml"
    source.write_text('minecraftVersion: "1.21.11"\n', encoding="utf-8")
    pipeline.make(source, output=tmp_path / "first.schem", emit=["resolve"])
    path = tmp_path / "first.resolve.json.meta.json"
    sidecar = json.loads(path.read_bytes())
    sidecar.pop("planVersion")
    path.write_text(json.dumps(sidecar), encoding="utf-8")
    with pytest.raises(BlueprintError, match="再生成"):
        pipeline.make(source, output=tmp_path / "second.schem",
                      resume=tmp_path / "first.resolve.json")


@pytest.mark.parametrize("archetype", ["terrain", "cave"])
def test_terrain_only_na_and_missing_building_plan(resolved, archetype):
    terrain = hierarchy(archetype, [])
    result = CriticAccumulator(plan_from_metadata(terrain)).finalize()
    assert all(m["status"] == "n/a" for m in result.metrics.values())
    terrain["archetype"] = "town"
    with pytest.raises(BlueprintError, match="棟がありません"):
        plan_from_metadata(terrain)
    plan = composite(resolved).massing
    plan_buildings(plan)[1]["columns"] = []
    with pytest.raises(BlueprintError, match="期待柱"):
        plan_from_metadata(plan)


@pytest.mark.parametrize("archetype", ["terrain", "cave"])
def test_pipeline_preserves_terrain_only_na(tmp_path, monkeypatch, archetype):
    from types import SimpleNamespace

    terrain = hierarchy(archetype, [])
    critic = CriticAccumulator(plan_from_metadata(terrain)).finalize()
    monkeypatch.setattr(pipeline, "_support", lambda *args: (0, None))
    context = {"massing": terrain, "resolved": {"config": {"limits": {}}}}
    failed, lines, metrics, diagnostics = pipeline._evaluate(
        None, SimpleNamespace(minecraft_version="1.21.11"), context,
        tmp_path / "terrain.yaml", 100, critic,
    )
    assert not failed and not any(line.startswith("FAIL") for line in lines)
    assert not any(d["severity"] in ("fail", "error") for d in diagnostics)
    assert all(value["status"] == "n/a" for name, value in metrics.items() if name != "support")


def layout():
    return {"formatVersion": 1, "name": "medieval.town", "style": "medieval",
            "archetype": "town", "rules": {"plotWidth": [12, 24], "roadWidth": [3, 5]},
            "modules": ["medieval.window"]}


@pytest.mark.parametrize("change", [
    {"unknown": True}, {"formatVersion": 2}, {"style": "missing"},
    {"rules": {"plotWidth": [24, 12]}}, {"rules": {"roadWidth": [0, 4]}},
    {"rules": {"roadWidth": [True, 4]}}, {"rules": {"ceilingClearance": [1, 257]}},
    {"rules": {"unknown": [1, 2]}}, {"modules": ["missing.module"]},
])
def test_layout_rejects_unknown_ranges_and_references(change):
    with pytest.raises(BlueprintError):
        validate_layout({**layout(), **change}, styles={"medieval"}, modules={"medieval.window"})


def test_layout_loader_check_and_schema_copies(tmp_path):
    import shutil

    from mcblueprint.layouts import PATTERNS

    root = tmp_path / "patterns"
    shutil.copytree(PATTERNS, root)
    directory = root / "layouts/medieval"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "town.yaml"
    path.write_text(yaml.safe_dump(layout()), encoding="utf-8")
    assert load_layout("medieval.town", root=root) == layout()
    assert not next(r for r in check_patterns(root) if r.path == path).errors
    path.write_text(yaml.safe_dump({**layout(), "modules": ["missing.module"]}), encoding="utf-8")
    assert next(r for r in check_patterns(root) if r.path == path).errors
    package = Path("src/mcblueprint/schema/pattern-layout.schema.json")
    assert package.read_bytes() == Path("schema/pattern-layout.schema.json").read_bytes()


def test_layout_bundled_lock_and_duplicate_keys(tmp_path, monkeypatch):
    import hashlib
    import shutil

    from mcblueprint import layouts

    root = tmp_path / "data/patterns"
    shutil.copytree(layouts.PATTERNS, root)
    directory = root / "layouts/medieval"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "town.yaml"
    path.write_text(yaml.safe_dump(layout()), encoding="utf-8")
    digest = hashlib.sha256()
    for file in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(file.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(file.read_bytes() + b"\0")
    lock_path = root.parent / "lock.json"
    lock = {"patterns": {"commit": "a" * 40, "sha256": digest.hexdigest()}}
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setattr(layouts, "PATTERNS", root)
    assert load_layout("medieval.town") == layout()
    path.write_text(path.read_text(encoding="utf-8") + "formatVersion: 1\n", encoding="utf-8")
    with pytest.raises(BlueprintError, match="lock"):
        load_layout("medieval.town")
    with pytest.raises(BlueprintError, match="重複"):
        load_layout("medieval.town", root=root)
    path.write_text("[invalid", encoding="utf-8")
    with pytest.raises(BlueprintError, match="YAML"):
        load_layout("medieval.town", root=root)


@pytest.mark.parametrize("archetype", ["castle", "town", "city", "terrain", "cave",
                                     "underground_city"])
def test_feature_templates_roundtrip(tmp_path, capsys, archetype):
    from mcblueprint.cli import main

    assert main(["spec-template", archetype, "--minecraft-version", "1.21.11"]) == 0
    raw = yaml.safe_load(capsys.readouterr().out)
    resolved = resolve_buildspec(raw, project_dir=tmp_path)
    assert resolved.buildspec["archetype"] == archetype


@pytest.mark.parametrize("archetype", ["castle", "town", "city", "terrain", "cave",
                                     "underground_city"])
def test_closed_features_and_unimplemented_planner(tmp_path, archetype):
    resolved = resolve_buildspec({"minecraftVersion": "1.21.11", "archetype": archetype,
                                  "scale": "huge"}, project_dir=tmp_path)
    if "buildings" in resolved.buildspec["features"]:
        assert resolved.buildspec["features"]["buildings"] == 12
    if archetype == "castle":
        assert len(plan_buildings(single_plan(resolved).massing)) == 9
    elif archetype in ("town", "city"):
        assert len(single_plan(resolved).massing["parcels"]) == 12
    elif archetype == "underground_city":
        assert len(plan_buildings(single_plan(resolved).massing)) == 12
    elif archetype == "cave":
        assert not plan_buildings(single_plan(resolved).massing)
    else:
        with pytest.raises(BlueprintError, match="Site/Massing.*未対応"):
            single_plan(resolved)
    with pytest.raises(BlueprintError, match="未知"):
        resolve_buildspec({"minecraftVersion": "1.21.11", "archetype": archetype,
                           "features": {"unknown": True}}, project_dir=tmp_path)


def test_connection_and_junction_references(resolved):
    plan = composite(resolved).massing
    plan["connections"]["edges"][0]["to"] = "missing#1"
    with pytest.raises(BlueprintError, match="接続辺"):
        plan_buildings(plan)
    plan = composite(resolved).massing
    plan["junctions"] = [{"element": "junction#1", "owner": "junction#1",
                          "parts": [{"element": "house#1", "shape": "rectangle"},
                                    {"element": "tower#1", "shape": "round"}],
                          "contacts": [{"first": "house#1", "second": "tower#1",
                                        "from": [0, 0, 0], "to": [0, 4, 0]}],
                          "passages": [{"from": "house#1.walk#1", "to": "tower#1.walk#1"}],
                          "roofPolicy": "envelope"}]
    assert len(plan_buildings(plan)) == 2
    plan["junctions"][0]["parts"][0]["element"] = "missing#1"
    with pytest.raises(BlueprintError, match="接合参照"):
        plan_buildings(plan)


def test_critic_requires_matching_massing_generation(resolved):
    plan = composite(resolved)
    old = copy.deepcopy(plan.massing)
    old["formatVersion"] = 1
    with pytest.raises(BlueprintError, match="再生成"):
        plan_from_metadata(plan.blueprint["metadata"]["planning"], massing=old)


@pytest.mark.parametrize("terrain", ["hill", "mountain", "plateau", "valley", "island", "cave"])
def test_known_terrain_names_precede_path_resolution(tmp_path, terrain):
    resolved = resolve_buildspec({"minecraftVersion": "1.21.11", "site": {"terrain": terrain}},
                                 project_dir=tmp_path)
    assert resolved.buildspec["site"]["terrain"] == terrain
    with pytest.raises(BlueprintError, match="Site:.*未対応"):
        single_plan(resolved)


@pytest.mark.parametrize("scale,count", [("tiny", 1), ("small", 3), ("medium", 4),
                                       ("large", 8), ("huge", 12)])
def test_building_counts_by_scale(tmp_path, scale, count):
    for archetype in ("town", "city", "underground_city"):
        resolved = resolve_buildspec({"minecraftVersion": "1.21.11", "archetype": archetype,
                                      "scale": scale}, project_dir=tmp_path)
        assert resolved.buildspec["features"]["buildings"] == count


@pytest.mark.parametrize("index,expected", [(0, "furnace"), (1, "diamond_block"),
                                          (2, "diamond_block")])
def test_materialize_preserves_site_operation_order(resolved, index, expected):
    from mcblueprint.generator import generate

    plan = composite(resolved)
    ref = {"role": "wall_main", "shape": "full", "state": {}}
    operations = [
        {"type": "translate", "offset": [0, 0, 0], "element": "house#1", "operations": [
            {"type": "set", "position": [0, 0, 0], "block": "minecraft:furnace"},
            {"type": "set", "position": [1, 0, 0], "block": ref}]},
        {"type": "set", "position": [3, 0, 0], "block": ref, "element": "tower#1"},
    ]
    operations.insert(index, {"type": "set", "position": [0, 0, 0],
                              "block": "minecraft:diamond_block", "element": "site#1"})
    plan.blueprint["operations"] = operations
    result = materialize_plan(plan.blueprint, palette=plan.palette)
    assert [op["element"] for op in result.blueprint["operations"]] == [
        op["element"] for op in operations]
    blueprint, errors = validate_and_load(result.blueprint)
    assert not errors
    assert generate(blueprint).get(Vec3(0, 0, 0)).id == f"minecraft:{expected}"


def test_fixed_roles_are_evaluated_per_building(resolved):
    from mcblueprint.generator import generate_regions

    plan = composite(resolved)
    plan.blueprint["operations"].insert(0, {
        "type": "set", "position": [-1, 0, -1], "element": "site#1",
        "block": {"role": "floor", "shape": "full", "state": {}},
    })
    result = materialize_plan(plan.blueprint, palette={
        **plan.palette, "materials": {**plan.palette["materials"], "wall_main": "minecraft:stone"},
    })
    fixed = {root: r.fixed_roles for root, r in result.building_results.items()}
    assert "wall_main" not in fixed["site#1"]
    assert all("wall_main" in fixed[root] for root in ("house#1", "tower#1"))
    blueprint, errors = validate_and_load(result.blueprint)
    assert not errors
    regions = list(generate_regions(blueprint))

    def measure(roles):
        critic = plan_from_metadata(blueprint.metadata["planning"], massing=plan.massing,
                                    building_fixed_roles=roles)
        accumulator = CriticAccumulator(critic)
        for region in regions:
            accumulator.consume(region)
        return [d for d in accumulator.finalize().diagnostics
                if d["code"] == "E305" and d.get("role") == "wall_main"]

    assert {d["element"].split(".")[0] for d in measure({})} == {"house#1", "tower#1"}
    assert measure(fixed) == []
    assert {d["element"].split(".")[0] for d in measure({**fixed, "tower#1": ()})} == {"tower#1"}


@pytest.mark.parametrize("kind", ["columns", "beams", "facades", "roof"])
def test_junction_accepts_existing_parts_only(resolved, kind):
    plan = composite(resolved).massing
    building = plan_buildings(plan)[0]
    part = "house#1.roof" if kind == "roof" else building[kind][0]["id"]
    junction = {"element": "junction#1", "owner": "junction#1",
                "parts": [{"element": part, "shape": "rectangle"},
                          {"element": "tower#1", "shape": "round"}],
                "contacts": [{"first": part, "second": "tower#1", "from": [0, 0, 0],
                              "to": [0, 4, 0]}],
                "passages": [], "roofPolicy": "envelope"}
    plan["junctions"] = [junction]
    assert len(plan_buildings(plan)) == 2
    junction["parts"][0]["element"] = "house#1.column#9999"
    with pytest.raises(BlueprintError, match="接合参照"):
        plan_buildings(plan)
    junction["parts"][0]["element"] = part
    junction["contacts"][0]["first"] = "house#1.column#9999"
    with pytest.raises(BlueprintError, match="接合参照"):
        plan_buildings(plan)
