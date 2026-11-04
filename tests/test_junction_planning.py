"""内部計画から施工・評価・emit/resumeまでの接合確認。"""

import copy
import gzip
import json
import random
from dataclasses import replace

import pytest
import yaml

from mcblueprint import pipeline, planning
from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.critic import CriticAccumulator, evaluate, plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion, generate
from mcblueprint.materialize import materialize_plan
from mcblueprint.model import BlockState, Vec3
from mcblueprint.operations import ExecutionContext, Transform
from mcblueprint.plan_contract import hierarchy, plan_buildings
from mcblueprint.planning import _plan_contract, plan_buildspec
from mcblueprint.validator import validate_and_load
from mcblueprint.volume import BlockVolume, CellAttributes


def joined_plan(resolved):
    buildings, wrappers, plans = [], [], []
    for index, floors in enumerate((2, 1)):
        root = f"house#{index + 1}"
        result = plan_buildspec(resolve_buildspec({
            "minecraftVersion": "1.21.11", "seed": 42, "features": {"floors": floors},
            "elements": {"house#1": {"width": 9, "depth": 11, "floor_height": 4}},
            "config": {"preview": {"enabled": False}},
        }))
        data = json.loads(json.dumps(result.blueprint).replace("house#1", root))
        b = plan_buildings(data["metadata"]["planning"])[0]
        wrapper = data["operations"][0]
        old = Transform.rotation(0, Vec3(0, 0, 0)).then(
            Transform.translation(Vec3.from_seq(b["offset"])))
        transform = Transform.translation(Vec3(index * 8, 0, 0))
        wrapper["offset"] = transform.offset.to_list()
        body = b["operations"]
        detail = [op for op in wrapper["operations"] if op not in body]
        # 既存plannerのローカル形状・外壁投影を使い、配置だけを接触位置へ写す。
        _, _, layout = planning._house(b["params"], root)
        contract = _plan_contract(root, body, detail, transform, b["params"], layout)
        b.update(contract, offset=transform.offset.to_list(),
                 entrance=transform.apply(layout.entrance).to_list(),
                 arrivals=[transform.apply(p).to_list() for p in layout.arrivals])
        for decoration in b["decorations"]:
            decoration["operation"] = decoration["operation"].replace(
                f"@:{old.rows}:{old.offset}", f"@:{transform.rows}:{transform.offset}")
        buildings.append(b)
        wrappers.append(wrapper)
        plans.append(result)
    plan = hierarchy("town", buildings)
    plan["connections"]["nodes"].extend([
        {"id": "junction#1.a", "element": "house#1", "position": [7, 1, 7]},
        {"id": "junction#1.b", "element": "house#2", "position": [9, 1, 7]},
    ])
    plan["junctions"] = [{"element": "junction#1", "owner": "junction#1",
                          "parts": [{"element": b["element"], "shape": "rectangle"}
                                    for b in buildings],
                          "contacts": [{"first": "house#1", "second": "house#2",
                                        "from": [8, 0, 0], "to": [8, 4, 10]}],
                          "passages": [{"from": "junction#1.a", "to": "junction#1.b"}],
                          "roofPolicy": "envelope"}]
    blueprint = copy.deepcopy(plans[0].blueprint)
    blueprint["operations"] = wrappers
    blueprint["palettes"] = {key: value for result in plans
                              for key, value in result.blueprint["palettes"].items()}
    blueprint["metadata"]["planning"] = copy.deepcopy(plan)
    return replace(plans[0], blueprint=blueprint, massing={**plan, "stage": "massing"},
                   site={**plans[0].site, **plan, "stage": "site"},
                   detail={**plans[0].detail, **plan, "stage": "detail"})


def test_plan_transfer_exposed_wall_and_missing_roof_or_beam(tmp_path):
    resolved = resolve_buildspec({"minecraftVersion": "1.21.11"}, project_dir=tmp_path,
                                 user_config=tmp_path / "missing")
    result = joined_plan(resolved)
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    raw = concrete.blueprint["operations"][0]
    assert raw["type"] == "building_junction"
    assert raw["frame"] == concrete.building_results["house#1"].roles["frame"]["full"].to_string()
    bp, errors = validate_and_load(concrete.blueprint)
    assert not errors, errors
    volume = generate(bp)
    critic = plan_from_metadata(bp.metadata["planning"], massing=result.massing)
    assert all(c.pos != Vec3(8, 3, 7) for f in critic.facades for c in f.cells)
    assert any(c.pos == Vec3(8, 7, 2) for f in critic.facades for c in f.cells)
    assert any(c.pos == Vec3(8, 2, 0) for f in critic.facades for c in f.cells)
    baseline = evaluate(critic, [GeneratedRegion(bp.bounds(), volume)])
    assert baseline.metrics["facade_surface_missing"]["value"] == 0
    assert not any(d["code"] == "E101" for d in baseline.diagnostics), baseline.diagnostics
    assert sum(v["numerator"] for v in baseline.metrics["decoration_density"].values()) > 0
    assert volume.attributes(Vec3(9, 6, 0)).role == "wall_main"
    roof_point = next(iter(bp.operations[0].roof_cells(
        ExecutionContext(BlockVolume(), random.Random(0), bp.palettes))))
    for point in (Vec3(9, 6, 0), Vec3(8, 2, 0), Vec3(8, 4, 7), roof_point):
        old, attr = volume.get(point), volume.attributes(point)
        volume.set(point, BlockState.of("air"), attr)
        measured = evaluate(critic, [GeneratedRegion(bp.bounds(), volume)])
        assert any(d["code"] == "E101" for d in measured.diagnostics)
        volume.set(point, old, attr)


def test_passage_cannot_cut_an_existing_structural_column():
    result = joined_plan(None)
    for plan in (result.blueprint["metadata"]["planning"], result.massing):
        nodes = {node["id"]: node for node in plan["connections"]["nodes"]}
        nodes["junction#1.a"]["position"] = [4, 1, 9]
        nodes["junction#1.b"]["position"] = [10, 1, 9]
    with pytest.raises(BlueprintError, match="構造柱・梁"):
        materialize_plan(result.blueprint, palette=result.palette)
    with pytest.raises(BlueprintError, match="構造柱・梁"):
        plan_from_metadata(result.blueprint["metadata"]["planning"], massing=result.massing)


def test_roof_role_is_preserved_for_structural_and_area_checks():
    result = joined_plan(None)
    concrete = materialize_plan(result.blueprint, palette=result.palette)
    bp, errors = validate_and_load(concrete.blueprint)
    assert not errors
    volume = generate(bp)
    operation = bp.operations[0]
    roofs = operation.roof_cells(ExecutionContext(BlockVolume(), random.Random(0), bp.palettes))
    expected = {p for p in roofs if operation.roof_roles[p] == "roof"}
    assert expected
    for part in operation.parts:
        original = BlockVolume()
        ctx = ExecutionContext(original, random.Random(0), bp.palettes, part.transform,
                               attributes=CellAttributes(role="roof"))
        for roof in part.roofs:
            ctx.execute(roof)
        for point, _ in original:
            if point in roofs and operation.roof_owners[point] == part.element:
                assert volume.attributes(point).role == original.attributes(point).role
    for point in expected:
        attr = volume.attributes(point)
        assert attr.role == "roof"
        assert attr.owner == "junction#1" and attr.reserved == "structure"
    critic = plan_from_metadata(bp.metadata["planning"], massing=result.massing)
    accumulator = CriticAccumulator(critic, max_area_ratios={}, building_area_ratios={
        "house#1": {"roof": 0.10}, "house#2": {"roof": 0.10}})
    accumulator.consume(GeneratedRegion(bp.bounds(), volume))
    measured = accumulator.finalize()
    assert not any(d["code"] == "E101" for d in measured.diagnostics)
    assert {d["element"] for d in measured.diagnostics if d["code"] == "accent_area_ratio"
            and d.get("role") == "roof"} == {"house#1", "house#2"}
    for root, counts in accumulator.role_counts.items():
        actual = sum(volume.attributes(p).role == "roof" for p, _ in volume
                     if (volume.attributes(p).element or "").startswith(root + "."))
        assert counts["roof"] == actual
        assert counts["roof"] / sum(counts.values()) > 0.10


@pytest.mark.parametrize("stage", pipeline.STAGES)
def test_junction_emit_resume_and_walkability(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(pipeline, "plan_buildspec", joined_plan)
    monkeypatch.setattr(planning, "plan_buildspec", joined_plan)
    source = tmp_path / "input.yaml"
    source.write_text(yaml.safe_dump({"minecraftVersion": "1.21.11", "seed": 42,
                                      "config": {"preview": {"enabled": False}}}),
                      encoding="utf-8", newline="\n")
    first = pipeline.make(source, output=tmp_path / "first.schem", emit=[stage])
    second = pipeline.make(source, output=tmp_path / "second.schem",
                           resume=tmp_path / f"first.{stage}.json")
    assert first.code == second.code
    assert gzip.decompress(first.output.read_bytes()) == gzip.decompress(second.output.read_bytes())
    a, b = (json.loads(r.record.read_bytes()) for r in (first, second))
    assert first.code == 0, a["structuredDiagnostics"]
    assert a["metrics"] == b["metrics"]
    assert a["structuredDiagnostics"] == b["structuredDiagnostics"]
    assert all(route["status"] == "reachable" for route in a["metrics"]["walkability"]["routes"])
    assert any(route["from"] == [7, 1, 7] and route["to"] == [9, 1, 7]
               for route in a["metrics"]["walkability"]["routes"])
    assert gzip.decompress((tmp_path / "first.scene.json.gz").read_bytes()) == gzip.decompress(
        (tmp_path / "second.scene.json.gz").read_bytes())
    if stage == "blueprint":
        from mcblueprint.cli import main

        output = tmp_path / "build.schem"
        assert main(["build", str(tmp_path / "first.blueprint.json"), "-o", str(output)]) == 0
        assert gzip.decompress(output.read_bytes()) == gzip.decompress(first.output.read_bytes())
