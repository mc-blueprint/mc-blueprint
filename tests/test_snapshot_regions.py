"""元地形spool、施工列計画、make再開と領域境界の回帰検証。"""

import json
from copy import deepcopy
from dataclasses import replace

import pytest
import yaml

from mcblueprint import pipeline
from mcblueprint.blockdata import load_block_data
from mcblueprint.cli import main
from mcblueprint.config import DEFAULTS
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.foundation import FoundationSpec, plan_foundation
from mcblueprint.generator import GenerationLimits, generate, generate_regions
from mcblueprint.importers import read_schematic, read_terrain, read_terrain_regions
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model import BlockState, Blueprint, Vec3
from mcblueprint.surface_path import SurfacePathSpec, plan_surface_path
from mcblueprint.terrain import ColumnWrites, TerrainPlacement
from mcblueprint.validator import validate_and_load
from test_foundation import blueprint_data, save_source, terrain
from test_foundation import raw_spec as foundation_spec
from test_pipeline import spec_file
from test_surface_path import raw_spec as path_spec
from test_surface_path import terrain as road_terrain
from test_terrain import litematic, save, schem


@pytest.mark.parametrize("fmt,size", [
    ("schem", (3, 2, 4)), ("litematic", (3, 2, 4)), ("litematic", (-3, -2, -4)),
])
def test_region_snapshot_matches_legacy(tmp_path, fmt, size):
    indices = tuple(i % 2 for i in range(24))
    root = schem(size, indices) if fmt == "schem" else litematic(size, indices)
    source = save(tmp_path, root, fmt)
    original = source.read_bytes()
    old, new = read_terrain(source), read_terrain_regions(source)
    assert (new.bounds, new.offset, new.data_version, new.minecraft_version, new.input_sha256) == (
        old.bounds, old.offset, old.data_version, old.minecraft_version, old.input_sha256,
    )
    for y in range(abs(size[1])):
        for z in range(abs(size[2])):
            for x in range(abs(size[0])):
                assert new.get(Vec3(x, y, z)) == old.get(Vec3(x, y, z))
    assert new.get(Vec3(-1, 0, 0)) is None
    assert not new.row(0, 0).flags.writeable
    assert source.read_bytes() == original


@pytest.mark.parametrize("fmt,indices", [
    ("schem", (0, 3, 0)), ("schem", (0, 1)), ("schem", (0, 1, 0, 0)),
    ("litematic", (0, 3, 0)),
])
def test_region_snapshot_rejects_invalid_arrays(tmp_path, fmt, indices):
    root = schem(indices=indices) if fmt == "schem" else litematic(indices=indices)
    with pytest.raises(BlueprintError):
        read_terrain_regions(save(tmp_path, root, fmt))


def collect(blueprint, *, reverse=False):
    cells = {}
    for region in generate_regions(
        blueprint, limits=GenerationLimits((2, 2, 2), 0, 64), reverse=reverse,
    ):
        for pos, state in region.volume:
            if region.core.contains(pos):
                cells[pos] = state
    return cells


@pytest.mark.parametrize("road", [False, True])
def test_column_plans_and_regions_match_old_construction(tmp_path, road):
    old = road_terrain() if road else terrain()
    source = save_source(tmp_path, old)
    snapshot = read_terrain_regions(source)
    new = TerrainPlacement(snapshot, old.region)
    foundation = None if road else plan_foundation(FoundationSpec.from_dict(foundation_spec()), new)
    path = plan_surface_path(SurfacePathSpec.from_dict(path_spec()), new) if road else None
    old_foundation = (None if road else
                      plan_foundation(FoundationSpec.from_dict(foundation_spec()), old))
    old_path = plan_surface_path(SurfacePathSpec.from_dict(path_spec()), old) if road else None
    plan, old_plan = (path, old_path) if road else (foundation, old_foundation)
    assert isinstance(plan.writes, ColumnWrites)
    assert dict(plan.writes) == dict(old_plan.writes)
    assert plan.result == old_plan.result
    blueprint = Blueprint(1, "1.21.11", "snapshot", [], terrain=new,
                          foundation=foundation, surface_path=path)
    expected = dict(generate(replace(blueprint, terrain=old, foundation=old_foundation,
                                     surface_path=old_path)))
    assert collect(blueprint) == expected
    assert collect(blueprint, reverse=True) == expected
    if road:
        config = deepcopy(DEFAULTS)
        config["weathering"].update(age=1, missing_blocks=1, vegetation=1)
        config["weathering"]["moss"]["amount"] = 1
        config["weathering"]["cracks"]["amount"] = 1
        config["limits"]["region"] = [5, 5, 5]
        blueprint.metadata = {"generation": {
            "mode": pipeline.GENERATION, "config": config,
            "groundHeight": {"type": "terrain", "inputHash": snapshot.input_sha256},
        }}
        for region in pipeline.generated_blueprint_regions(blueprint):
            for pos, state in path.surfaces:
                if region.core.contains(pos):
                    assert region.volume.get(pos) == state


def test_ground_field_keeps_original_uneven_heights(tmp_path, monkeypatch):
    old = terrain()
    snapshot = read_terrain_regions(save_source(tmp_path, old))
    heights = snapshot.ground_heights(snapshot.source_bounds)
    assert heights[-2, 4] == -1
    assert heights[-1, 4] == 0
    assert heights[0, 4] == 1
    foundation = plan_foundation(FoundationSpec.from_dict(foundation_spec()),
                                 TerrainPlacement(snapshot, snapshot.source_bounds))
    config = deepcopy(DEFAULTS)
    config["limits"].update(region=[2, 2, 2], halo=0)
    generation = {"mode": pipeline.GENERATION, "config": config,
                  "groundHeight": {"type": "terrain", "inputHash": snapshot.input_sha256}}
    blueprint = Blueprint(1, "1.21.11", "weather-ground", [],
                          metadata={"generation": generation},
                          terrain=TerrainPlacement(snapshot, snapshot.source_bounds),
                          foundation=foundation)
    seen = []

    def weather(region, blocks, **kwargs):
        for key, value in kwargs["ground_height"].items():
            assert value == heights[key]
        seen.append(region.core)
        return ()

    monkeypatch.setattr(pipeline, "apply_weather", weather)
    for region in pipeline.generated_blueprint_regions(blueprint):
        del region
    assert seen


@pytest.mark.parametrize("case,expected", [
    ("outside", "取得範囲外"), ("protected", "保護範囲"),
    ("cut", "上限"), ("fill", "上限"), ("version", "一致"),
])
def test_compact_loader_keeps_construction_rejections(tmp_path, case, expected):
    save_source(tmp_path, terrain())
    raw = blueprint_data()
    raw["metadata"] = {"generation": {
        "mode": pipeline.GENERATION, "config": deepcopy(DEFAULTS),
    }}
    if case == "outside":
        raw["terrain"]["region"]["from"][0] -= 1
    elif case == "protected":
        raw["terrain"]["protected"] = [deepcopy(raw["terrain"]["region"])]
    elif case == "cut":
        raw["foundation"]["maxCut"] = 0
    elif case == "fill":
        raw["foundation"]["maxFill"] = 0
    else:
        raw["minecraftVersion"] = "26.1.2"
    model, errors = validate_and_load(raw, base_dir=tmp_path)
    assert model is None
    assert expected in " ".join(str(e) for e in errors)


def test_schem_multibyte_varints_share_palette_interpretation(tmp_path):
    from nbtlib import Compound, Int

    root = schem((2, 1, 1), (0, -127, 1))
    root["Palette"] = Compound({"minecraft:air": Int(0), "minecraft:stone": Int(129)})
    source = save(tmp_path, root, "schem")
    old, new = read_terrain(source), read_terrain_regions(source)
    assert new.get(Vec3(0, 0, 0)) == old.get(Vec3(0, 0, 0))
    assert new.get(Vec3(1, 0, 0)) == old.get(Vec3(1, 0, 0))


def test_decode_budget_rejected_before_nbt_arrays(tmp_path, monkeypatch):
    import nbtlib

    source = save(tmp_path, schem(), "schem")

    def unexpected(*args, **kwargs):
        pytest.fail("予算超過後にNBTを展開した")

    monkeypatch.setattr(nbtlib.File, "from_fileobj", unexpected)
    with pytest.raises(BlueprintError, match="入力配列"):
        read_terrain_regions(source, max_memory_mb=0.5)


@pytest.mark.parametrize("fmt", ["schem", "litematic"])
@pytest.mark.parametrize("normalized", [False, True])
def test_make_rejects_snapshot_as_output_before_planning(tmp_path, monkeypatch, fmt, normalized):
    root = schem() if fmt == "schem" else litematic()
    source = save(tmp_path, root, fmt)
    original = source.read_bytes()
    spec = spec_file(tmp_path, site={"terrain": str(source)},
                     config={"output": {"format": fmt}})
    output = source.parent / "unused" / ".." / source.name if normalized else source

    def unexpected(*args, **kwargs):
        pytest.fail("元地形への出力を拒否する前に計画を開始した")

    monkeypatch.setattr(pipeline, "plan_buildspec", unexpected)
    with pytest.raises(pipeline.PipelineError, match="元地形の入力と出力") as error:
        pipeline.make(spec, output=output, emit=["blueprint"])
    assert error.value.code == 2
    assert source.read_bytes() == original
    assert not source.with_name(f"{source.stem}.resolve.json").exists()
    assert not source.parent.joinpath("unused").exists()


@pytest.mark.parametrize("generation", [None, [], ["value"], "value", 1, 0, True])
def test_nonobject_generation_preserves_legacy_terrain_validate_and_build(tmp_path, generation):
    source = save_source(tmp_path, terrain())
    original = source.read_bytes()
    raw = blueprint_data()
    raw["metadata"] = {"generation": generation}
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(raw), encoding="utf-8", newline="\n")
    assert main(["validate", str(path)]) == 0
    assert main(["build", str(path), "-o", str(tmp_path / "legacy.schem")]) == 0
    assert source.read_bytes() == original


@pytest.mark.parametrize("protection", [None, "structure", "circulation", "path", "operation"])
def test_snapshot_air_vegetation_matches_empty_cells_and_keeps_protection(tmp_path, protection):
    from nbtlib import Compound, Int

    from mcblueprint.generator import GeneratedRegion
    from mcblueprint.operations.registry import build_operations
    from mcblueprint.volume import CellAttributes, RegionVolume
    from mcblueprint.weather import apply_weather

    root = schem((3, 3, 3), (1,) * 9 + (0,) * 18)
    root["Palette"] = Compound({"minecraft:air": Int(0), "minecraft:dirt": Int(1)})
    snapshot = read_terrain_regions(save(tmp_path, root, "schem"))
    bounds = snapshot.source_bounds
    control = RegionVolume(bounds)
    for x in range(bounds.min.x, bounds.max.x + 1):
        for z in range(bounds.min.z, bounds.max.z + 1):
            control.set(Vec3(x, bounds.min.y, z), BlockState.parse("dirt"))
    operations = []
    protected = bounds.min + Vec3(1, 1, 1)
    if protection is not None:
        if protection == "path":
            protected = protected - Vec3(0, 1, 0)
            state, attributes = "dirt", {"role": "path"}
        elif protection == "operation":
            state, attributes = "air", {}
        else:
            state, attributes = "air", {"reserved": protection, "owner": "guard"}
        control.set(protected, BlockState.parse(state), CellAttributes(
            **attributes, operation="placed" if protection == "operation" else None,
        ))
        operations.append({"type": "set", "position": protected.to_list(),
                           "block": state, **attributes})
    config = deepcopy(DEFAULTS)
    config["weathering"].update(age=1, vegetation=1)
    config["weathering"]["moss"]["amount"] = 0
    config["weathering"]["cracks"]["amount"] = 0
    config["randomness"] = 0
    config["limits"]["region"] = [2, 2, 2]
    apply_weather(GeneratedRegion(bounds, control), load_block_data("1.21.11"), seed=42,
                  config=config["weathering"], ground_height=bounds.min.y)
    blueprint = Blueprint(1, "1.21.11", "vegetation", build_operations(operations, "operations"),
                          seed=42, terrain=TerrainPlacement(snapshot, bounds),
                          metadata={"generation": {
                              "mode": pipeline.GENERATION, "config": config,
                              "groundHeight": {"type": "terrain",
                                               "inputHash": snapshot.input_sha256},
                          }})
    plants = {}
    for region in pipeline.generated_blueprint_regions(blueprint):
        for pos, state in region.volume:
            if (region.core.contains(pos)
                    and state.id in ("minecraft:short_grass", "minecraft:fern")):
                plants[pos] = state
    expected = {pos: state for pos, state in control
                if state.id in ("minecraft:short_grass", "minecraft:fern")}
    assert len(expected) == (9 if protection is None else 8)
    assert plants == expected


@pytest.mark.parametrize("resume_stage,fmt", [
    ("detail", "schem"), ("blueprint", "schem"), ("blueprint", "litematic"),
])
def test_make_build_resume_snapshot_and_change_rejection(tmp_path, monkeypatch, resume_stage, fmt):
    # 地盤は負座標、建物と屋根を含むair余白を保持する。
    from mcblueprint.volume import BlockVolume

    volume = BlockVolume()
    for x in range(-16, 16):
        for z in range(-16, 16):
            volume.set(Vec3(x, -2, z), BlockState.parse("stone"))
    volume.set(Vec3(15, 25, 15), BlockState.parse("air"))
    source = tmp_path / f"source.{fmt}"
    EXPORTERS[fmt]().export(volume, Blueprint(1, "1.21.11", "source", []), source)
    original = source.read_bytes()
    spec = spec_file(tmp_path, site={"terrain": str(source)})
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    data["config"]["weathering"] = {"age": 0}
    data["config"]["palette"] = {"mix": 0}
    spec.write_text(yaml.safe_dump(data), encoding="utf-8")
    calls = []
    reader = pipeline.plan_buildspec.__globals__["read_terrain_regions"]

    def read_once(*args, **kwargs):
        calls.append(args[0])
        return reader(*args, **kwargs)

    monkeypatch.setitem(pipeline.plan_buildspec.__globals__, "read_terrain_regions", read_once)
    first = pipeline.make(spec, output=tmp_path / "first.schem", emit=["blueprint"])
    assert first.code == 0
    assert len(calls) == 1
    checkpoint = tmp_path / "first.blueprint.json"
    concrete = json.loads(checkpoint.read_text(encoding="utf-8"))
    model = load_blueprint_dict(concrete)
    assert model.terrain.snapshot.input_sha256 == concrete["metadata"]["generation"]["terrainHash"]
    # 劣化0なら旧施工との全セル一致も確認する。
    blocks = load_block_data("1.21.11")
    assert {p: blocks.complete(s) for p, s in read_schematic(first.output).volume} == {
        pos - model.bounds().min: blocks.complete(state) for pos, state in generate(model)
        if state.id != "minecraft:air"
    }
    assert main(["build", str(checkpoint), "-o", str(tmp_path / "built.schem")]) == 0
    resume = tmp_path / f"first.{resume_stage}.json"
    pipeline.make(spec, output=tmp_path / "resumed.schem", resume=resume)
    expected = dict(read_schematic(first.output).volume)
    assert dict(read_schematic(tmp_path / "built.schem").volume) == expected
    assert dict(read_schematic(tmp_path / "resumed.schem").volume) == expected
    assert source.read_bytes() == original
    changed = bytearray(original)
    changed[4] ^= 1  # gzipのmtimeだけを変え、NBTの内容は保つ。
    previous = first.output.read_bytes()

    def change_during_generation(stage):
        if stage == "Generate":
            source.write_bytes(changed)

    with pytest.raises(pipeline.PipelineError, match="公開直前") as error:
        pipeline.make(spec, output=first.output, stage_hook=change_during_generation)
    assert error.value.code == 2
    assert first.output.read_bytes() == previous
    with pytest.raises(pipeline.PipelineError, match="入力|一致"):
        pipeline.make(spec, output=tmp_path / "rejected.schem", resume=checkpoint)
    with pytest.raises(BlueprintError, match="hash"):
        load_blueprint_dict(concrete)
