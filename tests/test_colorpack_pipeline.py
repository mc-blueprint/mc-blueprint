"""同梱境界の人工パックで生成・評価・世代照合を確認する。"""

import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from mcblueprint import colorpack, pipeline
from mcblueprint.cli import main
from mcblueprint.critic import CriticAccumulator, CriticPlan, Facade, FacadeCell, StructuralRun
from mcblueprint.edits import apply_edits_file
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.importers import read_schematic
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.viewer_input import EditConflict, ViewerInput, read_scene
from mcblueprint.volume import CellAttributes, RegionVolume

DATA = Path(__file__).parent / "data"


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    root = tmp_path / "data"
    (root / "colorpack").mkdir(parents=True)
    raw = json.loads((DATA / "colorpack/selection.json").read_bytes())
    rule = raw["relationRule"][0]
    rule.update(dL=[-1, -0.5, 0, 0.5, 1], dC=[-0.56, -0.3, 0, 0.3, 0.56],
                dh={"origin": 0, "quantiles": [-math.pi, -1, 0, 1, math.pi]})
    raw["relationRule"].append({**rule, "pair": ["wall_main", "frame"]})
    paths = {
        "pack": root / "colorpack/colorpack.json",
        "license": root / "colorpack/DATA_LICENSE",
        "vocabulary": root / "colorpack-vocabulary.json",
    }
    paths["pack"].write_text(json.dumps(raw), encoding="utf-8")
    paths["license"].write_bytes(b"Synthetic fixture\n")
    paths["vocabulary"].write_bytes(
        (Path(colorpack.__file__).parent / "data/colorpack-vocabulary.json").read_bytes())

    def lock():
        entry = {key: raw[key] for key in (
            "packVersion", "taxonomyVersion", "vocabularyVersion")}
        entry["sha256"] = {key: hashlib.sha256(path.read_bytes()).hexdigest()
                           for key, path in paths.items()}
        (root / "lock.json").write_text(json.dumps({"colorpack": entry}), encoding="utf-8")

    lock()
    monkeypatch.setattr(colorpack, "_data_root", lambda: root)
    return raw, paths, lock


def source_file(tmp_path, archetype="house", config=None):
    name = "single-house" if archetype == "house" else "standalone-tower"
    raw = yaml.safe_load((DATA / f"buildspec/{name}.yaml").read_bytes())
    raw["config"] = {"preview": {"enabled": False}, **(config or {})}
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def cells(path):
    return dict(read_schematic(path).volume)


def record(result):
    return json.loads(result.record.read_bytes())


@pytest.mark.parametrize("archetype", ["house", "tower"])
@pytest.mark.parametrize("fmt", ["schem", "litematic"])
def test_pack_make_build_resume_and_edits(bundled, tmp_path, monkeypatch, archetype, fmt):
    source = source_file(tmp_path, archetype, {"output": {"format": fmt}})
    original_load = pipeline.load_colorpack
    calls = []

    def load():
        calls.append(1)
        return original_load()

    monkeypatch.setattr(pipeline, "load_colorpack", load)
    result = pipeline.make(source, output=tmp_path / f"first.{fmt}", emit=["blueprint"])
    assert len(calls) == 1
    assert result.code == 0
    initial = record(result)
    assert initial["colorpack"]["selectionSources"]["wall_main"]["color"]["source"] == "pack"
    assert initial["metrics"]["palette_relation"]["pack"]["denominator"] >= 2
    assert initial["metrics"]["accent_area_ratio"]
    for stage in ("detail", "blueprint"):
        resumed = pipeline.make(source, output=tmp_path / f"{stage}.{fmt}",
                                resume=tmp_path / f"first.{stage}.json")
        assert cells(resumed.output) == cells(result.output)
        assert record(resumed)["metrics"] == initial["metrics"]
        assert record(resumed)["colorpack"] == initial["colorpack"]
    rebuilt = tmp_path / f"rebuilt.{fmt}"
    assert main(["build", str(tmp_path / "first.blueprint.json"), "--format", fmt,
                 "-o", str(rebuilt)]) == 0
    assert cells(rebuilt) == cells(result.output)
    scene = tmp_path / "first.scene.json.gz"
    meta = json.loads(scene.with_suffix(".gz.meta.json").read_bytes())
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps({"formatVersion": 1, "scene": meta["sceneSha256"],
                                 "edits": []}), encoding="utf-8")
    edited = tmp_path / "edited.yaml"
    apply_edits_file(scene, edits, source, edited)
    regenerated = pipeline.make(edited, output=tmp_path / f"edited.{fmt}")
    assert cells(regenerated.output) == cells(result.output)
    assert record(regenerated)["colorpack"] == initial["colorpack"]


@pytest.mark.parametrize("change", ["pack", "license", "vocabulary"])
def test_content_changes_reject_resume_edits_and_viewer(bundled, tmp_path, change):
    _, paths, lock = bundled
    source = source_file(tmp_path)
    result = pipeline.make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    scene_path = tmp_path / "first.scene.json.gz"
    raw_scene, scene = read_scene(scene_path)
    meta = json.loads(scene_path.with_suffix(".gz.meta.json").read_bytes())
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps({"formatVersion": 1, "scene": meta["sceneSha256"],
                                 "edits": []}), encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.glob("first*") if path.is_file()}
    paths[change].write_bytes(paths[change].read_bytes() + b"\n")
    lock()
    with pytest.raises(pipeline.PipelineError, match="hash"):
        pipeline.make(source, output=result.output, resume=tmp_path / "first.blueprint.json")
    target = tmp_path / "edited.yaml"
    target.write_bytes(b"existing")
    with pytest.raises(BlueprintError, match="engine.*data"):
        apply_edits_file(scene_path, edits, source, target)
    prepared = ViewerInput(source, result.output, scene_path, edits, target, {}, [],
                           True, source.read_bytes(), raw_scene, scene, meta)
    with pytest.raises(EditConflict, match="世代"):
        prepared.check_generation()
    assert target.read_bytes() == b"existing"
    assert all(path.read_bytes() == content for path, content in before.items())


def test_viewer_regeneration_keeps_pack_palette(bundled, tmp_path):
    source = source_file(tmp_path)
    result = pipeline.make(source, output=tmp_path / "viewer.schem")
    before = cells(result.output), record(result)["colorpack"]
    scene_path = tmp_path / "viewer.scene.json.gz"
    raw, scene = read_scene(scene_path)
    meta = json.loads(scene_path.with_suffix(".gz.meta.json").read_bytes())
    edits = tmp_path / "viewer.edits.json"
    prepared = ViewerInput(source, result.output, scene_path, edits, tmp_path / "edited.json",
                           {}, [], True, source.read_bytes(), raw, scene, meta)
    prepared.save_edits({"formatVersion": 1, "scene": prepared.scene_hash, "edits": []})
    response = prepared.regenerate()
    assert response["ok"] and response["code"] == result.code
    assert (cells(result.output), record(result)["colorpack"]) == before
    prepared.check_generation()


def test_broken_pack_stops_before_planning_and_preserves_output(bundled, tmp_path, monkeypatch):
    _, paths, _ = bundled
    source = source_file(tmp_path)
    output = tmp_path / "result.schem"
    output.write_bytes(b"existing")
    paths["license"].unlink()

    def unexpected(*args, **kwargs):
        pytest.fail("破損パックで計画を開始した")

    monkeypatch.setattr(pipeline, "plan_buildspec", unexpected)
    with pytest.raises(pipeline.PipelineError, match="同梱配色"):
        pipeline.make(source, output=output, emit=["blueprint"])
    assert output.read_bytes() == b"existing"
    assert not (tmp_path / "result.resolve.json").exists()


def test_missing_relations_are_not_pack_success(bundled, tmp_path):
    raw, paths, lock = bundled
    raw["relationRule"] = []
    paths["pack"].write_text(json.dumps(raw), encoding="utf-8")
    lock()
    result = pipeline.make(source_file(tmp_path), output=tmp_path / "result.schem")
    metric = record(result)["metrics"]["palette_relation"]
    assert metric["pack"]["value"] is None
    assert metric["pack"]["status"] == "n/a"
    assert metric["pack"]["denominator"] == 0
    assert metric["d28"]["denominator"] > 0


def test_no_pack_keeps_d28_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)
    result = pipeline.make(source_file(tmp_path), output=tmp_path / "result.schem")
    saved = record(result)
    assert result.code == 0
    assert "colorpack" not in saved
    assert "colorpack" not in saved["environment"]
    assert "accent_area_ratio" not in saved["metrics"]
    assert saved["metrics"]["palette_relation"] == {"value": True}


@pytest.mark.parametrize("region_size", [16, 32, 64])
@pytest.mark.parametrize("reverse", [False, True])
def test_partition_and_order_keep_cells_and_metrics(bundled, tmp_path, monkeypatch,
                                                   region_size, reverse):
    source = source_file(tmp_path, "tower")
    baseline = pipeline.make(source, output=tmp_path / "first.schem")
    generate = pipeline.generate_regions
    if reverse:
        monkeypatch.setattr(pipeline, "generate_regions",
                            lambda *a, **kw: iter(reversed(list(generate(*a, **kw)))))
    result = pipeline.make(source, output=tmp_path / "other.schem", overrides=[
        f"limits.region=[{region_size},{region_size},{region_size}]",
    ])
    assert cells(result.output) == cells(baseline.output)
    assert record(result)["metrics"] == record(baseline)["metrics"]


def test_fixed_ids_without_relation_solution_preserve_failed_artifacts(bundled, tmp_path):
    raw, paths, lock = bundled
    raw["relationRule"][0]["dL"] = [0.9] * 5
    paths["pack"].write_text(json.dumps(raw), encoding="utf-8")
    lock()
    source = source_file(tmp_path, config={"palette": {"materials": {
        "wall_main": "minecraft:stone_bricks", "roof": "minecraft:stone_bricks",
    }}})
    result = pipeline.make(source, output=tmp_path / "failed.schem", emit=["blueprint"])
    saved = record(result)
    assert result.code == 3
    assert result.output.exists() and (tmp_path / "failed.scene.json.gz").exists()
    assert saved["status"] == "failed"
    assert any(d["metric"] == "palette_relation" for d in saved["structuredDiagnostics"])
    relation = saved["metrics"]["palette_relation"]["pack"]
    assert relation["numerator"] < relation["denominator"]
    assert relation["excluded"] > 0
    assert saved["colorpack"]["selectionSources"]["roof"]["color"]["source"] == "fixed_id"


def test_area_violation_preserves_failed_artifacts(bundled, tmp_path):
    raw, paths, lock = bundled
    raw["accentRule"] = [{"when": {}, "role": "wall_main", "maxAreaRatio": 0}]
    paths["pack"].write_text(json.dumps(raw), encoding="utf-8")
    lock()
    result = pipeline.make(source_file(tmp_path), output=tmp_path / "failed.schem")
    assert result.code == 3 and result.output.exists()
    assert any(d["metric"] == "accent_area_ratio"
               for d in record(result)["structuredDiagnostics"])


@pytest.mark.parametrize("maximum,failed", [(0.5, False), (0.499, True)])
def test_final_roles_count_core_once_per_building(maximum, failed):
    bounds = AABB(Vec3(0, 0, 0), Vec3(4, 0, 0))
    volume = RegionVolume(bounds)
    stone = BlockState("minecraft:stone")
    for x, root, role in [(0, "house#1", "wall_main"), (1, "house#1", "wall_main"),
                           (2, "house#2", "wall_main"), (3, "house#2", "wall_main"),
                           (4, "house#1", "accent")]:
        volume.set(Vec3(x, 0, 0), stone, CellAttributes(role=role, element=root + ".wall"))
    # 最終Weather属性の役割で数える。haloのaccentは分子に入れない。
    volume.set(Vec3(0, 0, 0), stone,
               replace(volume.attributes(Vec3(0, 0, 0)), role="accent"))
    facades = tuple(Facade(root + ".facade.north", "north",
                          (FacadeCell(Vec3(x, 0, 0), 0, 0, 0),), building=root)
                    for x, root in [(0, "house#1"), (2, "house#2")])
    plan = CriticPlan((StructuralRun("frame", (Vec3(1, 0, 0),)),), (), facades, {}, (0, 1))
    accumulator = CriticAccumulator(plan, max_area_ratios={"accent": maximum})
    region = GeneratedRegion(AABB(Vec3(0, 0, 0), Vec3(3, 0, 0)), volume)
    accumulator.consume(region)
    accumulator.consume(region)
    result = accumulator.finalize()
    metric = result.metrics["accent_area_ratio"]
    assert metric["house#1:accent"]["value"] == 0.5
    assert metric["house#1:accent"]["denominator"] == 2
    assert metric["house#2:accent"]["value"] == 0
    assert any(d["metric"] == "accent_area_ratio" for d in result.diagnostics) == failed
