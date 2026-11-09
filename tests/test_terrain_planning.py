"""地形単独の計画と完成coreの保証形状を検証する。"""

import copy
import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GenerationLimits, generate_regions
from mcblueprint.materialize import materialize_plan
from mcblueprint.pipeline import PipelineError, make
from mcblueprint.plan_contract import plan_buildings
from mcblueprint.planning import plan_buildspec
from mcblueprint.terrain_density import TerrainDensity, check_density_region
from mcblueprint.validator import validate_and_load

CASES = [c for c in json.loads(Path("evaluation/cases/large-scale.json").read_bytes())["cases"]
         if c["buildSpec"]["archetype"] == "terrain"]


def terrain_spec(**changes):
    return {"minecraftVersion": "1.21.11", "archetype": "terrain", "seed": 42,
            "size": [24, 24, 24], "site": {"terrain": "mountain"}, **changes}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
@pytest.mark.parametrize("scale", ("medium", "huge"))
@pytest.mark.parametrize("seed", (42, 73, 101))
def test_fixed_terrain_plans(tmp_path, case, scale, seed):
    resolved = resolve_buildspec({**case["buildSpec"], "scale": scale, "seed": seed},
                                 project_dir=tmp_path)
    before = copy.deepcopy(resolved)
    result = plan_buildspec(resolved)
    assert result == plan_buildspec(resolved)
    assert resolved == before
    assert not plan_buildings(result.massing)
    assert not result.massing["connections"]["nodes"]
    assert not result.detail["diagnostics"]
    assert result.layout is None and result.terrain_snapshot is None
    density = TerrainDensity.from_dict(result.site["density"])
    assert not density.main_cave and not density.chambers and not density.nodes
    assert density.field.band_top == resolved.buildspec["size"][1] - 1
    assert density.field.to_dict() == result.site["groundHeight"]["field"]
    assert result.site["offset"] == [0, 0, 0] and result.site["floorY"] == 0
    assert "role" not in result.blueprint["operations"][0]


@pytest.mark.parametrize("kind", ("none", "flat", "hill", "mountain", "plateau", "valley",
                                 "island"))
def test_supported_terrain_kinds(tmp_path, kind):
    result = plan_buildspec(resolve_buildspec(terrain_spec(site={"terrain": kind}),
                                            project_dir=tmp_path))
    assert result.site["terrain"] == ("flat" if kind == "none" else kind)


@pytest.mark.parametrize("changes,match", (({"site": {"terrain": "cave"}}, "cave/underground_city"),
    ({"site": {"terrain": "existing.schem"}}, "既存地形編集"),
    ({"elements": {"terrain#1": {"width": 12}}}, "elements"),
    ({"overrides": [{"id": "outside", "at": [24, 0, 0], "block": "air"}]}, "範囲外")))
def test_rejects_unsupported_terrain_edits(tmp_path, changes, match):
    with pytest.raises(BlueprintError, match=match):
        plan_buildspec(resolve_buildspec(terrain_spec(**changes), project_dir=tmp_path))


def test_zero_settings_and_partitioned_generation(tmp_path):
    result = plan_buildspec(resolve_buildspec(terrain_spec(config={"terrain": {
        "amplitude": 0, "erosion": 0, "caves": 0, "vegetation": 0}}), project_dir=tmp_path))
    density = TerrainDensity.from_dict(result.site["density"])
    assert np.ptp(density.field.heights) == 0
    assert not np.any(density.field.coarse_delta)
    concrete = materialize_plan(result.blueprint, palette=result.palette).blueprint
    bp, errors = validate_and_load(concrete)
    assert not errors, errors
    expected = None
    roles = set()
    for width, reverse in ((16, False), (32, False), (64, False), (7, True)):
        cells = {}
        for region in generate_regions(bp, limits=GenerationLimits((width, 8, width), 2),
                                       reverse=reverse):
            assert not check_density_region(density, region.volume, region.core)
            for pos, state in region.volume:
                if region.core.contains(pos):
                    attrs = region.volume.attributes(pos)
                    cells[tuple(pos)] = str(state), attrs
                    roles.add(attrs.role)
        if expected is None:
            expected = cells
        assert cells == expected
    assert {"terrain.support", "terrain.opening", "terrain.ground"} <= roles
    assert "terrain.vegetation" not in roles
    assert "ground" not in roles


@pytest.mark.parametrize("target", ("support", "right_support", "opening", "exterior_near",
                                   "exterior_far"))
def test_terrain_make_rejects_destroyed_shape(tmp_path, target):
    raw = terrain_spec()
    result = plan_buildspec(resolve_buildspec(raw, project_dir=tmp_path))
    density = TerrainDensity.from_dict(result.site["density"])
    box = density.field.bounds
    tile = density.sample(box)
    support = "support" in target
    mask = tile.support if support else tile.opening
    points = np.argwhere(mask)
    if target == "right_support":
        points = points[points[:, 2] == density.arch.max.x]
    y, z, x = points[0]
    if not support:
        z = {"opening": density.arch.min.z, "exterior_near": box.min.z,
             "exterior_far": box.max.z}[target]
        assert tile.opening[y, z, x]
    raw["overrides"] = [{"id": "broken", "at": [int(x), int(y), int(z)],
                         "block": "air" if support else "stone"}]
    source = tmp_path / "terrain.yaml"
    source.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")
    made = make(source, output=tmp_path / "broken.schem")
    assert made.code == 3, made.diagnostics
    record = json.loads(made.record.read_bytes())
    assert record["status"] == "failed"
    assert record["metrics"]["terrain_shape"]["value"] is False
    assert any(d["code"] == "terrain_shape" and d["severity"] == "fail"
               for d in record["structuredDiagnostics"])


@pytest.mark.parametrize("seed", (42, 73, 101))
def test_vegetation_does_not_grow_on_arch_stone(tmp_path, seed):
    case = next(c for c in CASES if c["id"] == "terrain-mountain-relief-no-erosion")
    source = tmp_path / "terrain.yaml"
    source.write_text(yaml.safe_dump({**case["buildSpec"], "scale": "medium", "seed": seed}),
                      encoding="utf-8", newline="\n")
    made = make(source, output=tmp_path / "terrain.schem")
    assert made.code == 0, made.diagnostics
    assert json.loads(made.record.read_bytes())["metrics"]["support"]["value"] == 0


def test_terrain_shape_checks_weathered_core(tmp_path, monkeypatch):
    from mcblueprint import pipeline
    from mcblueprint.model import BlockState

    generate = pipeline.generated_blueprint_regions

    def damaged(*args, **kwargs):
        for region in generate(*args, **kwargs):
            for point, _ in region.volume:
                if (region.core.contains(point)
                        and region.volume.attributes(point).role == "terrain.support"):
                    region.volume.set(point, BlockState.of("air"))
                    break
            yield region

    monkeypatch.setattr(pipeline, "generated_blueprint_regions", damaged)
    source = tmp_path / "terrain.yaml"
    source.write_text(yaml.safe_dump(terrain_spec()), encoding="utf-8", newline="\n")
    made = make(source, output=tmp_path / "weathered.schem")
    assert made.code == 3, made.diagnostics


def test_terrain_without_arch_has_reasoned_na(tmp_path):
    source = tmp_path / "terrain.yaml"
    source.write_text(yaml.safe_dump(terrain_spec(features={"arches": False})),
                      encoding="utf-8", newline="\n")
    made = make(source, output=tmp_path / "terrain.schem")
    assert made.code == 0, made.diagnostics
    metrics = json.loads(made.record.read_bytes())["metrics"]
    for metric in ("terrain_shape", "column_continuity", "beam_continuity", "flat_ratio",
                   "decoration_density", "facade_mix", "walkability", "palette_relation"):
        assert metrics[metric]["status"] == "n/a"
        assert metrics[metric]["reason"]


def test_building_missing_plan_still_fails(tmp_path):
    plan = plan_buildspec(resolve_buildspec(terrain_spec(), project_dir=tmp_path)).massing
    plan["archetype"] = "house"
    with pytest.raises(BlueprintError, match="棟がありません"):
        plan_buildings(plan)


def test_terrain_site_and_blueprint_density_must_match(tmp_path, monkeypatch):
    from dataclasses import replace

    from mcblueprint import pipeline
    from mcblueprint.terrain_density import plan_density

    original = pipeline.plan_buildspec

    def mismatched(resolved):
        result = original(resolved)
        density = TerrainDensity.from_dict(result.site["density"])
        other = plan_density(density.field, arches=False)
        return replace(result, site={**result.site, "density": other.to_dict()})

    monkeypatch.setattr(pipeline, "plan_buildspec", mismatched)
    source = tmp_path / "terrain.yaml"
    source.write_text(yaml.safe_dump(terrain_spec()), encoding="utf-8", newline="\n")
    with pytest.raises(PipelineError, match="密度計画が一致しません") as caught:
        make(source, output=tmp_path / "mismatched.schem")
    assert caught.value.code == 1
