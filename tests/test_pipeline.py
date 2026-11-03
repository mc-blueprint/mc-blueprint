"""makeの固定課題、再開、旧build互換と段階失敗。"""

import json
from pathlib import Path

import pytest
import yaml

from mcblueprint import cli, pipeline
from mcblueprint.cli import main
from mcblueprint.errors import BlueprintError
from mcblueprint.importers import read_schematic
from mcblueprint.model import BlockState, Vec3

FIXTURES = Path(__file__).parent / "data/buildspec"
VERSIONS = ("1.21.11", "26.1.2", "26.2", "26.3")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))


def spec_file(tmp_path, archetype="house", version="1.21.11", **values):
    fixture = "single-house" if archetype == "house" else "standalone-tower"
    data = yaml.safe_load((FIXTURES / f"{fixture}.yaml").read_text(encoding="utf-8"))
    data.update(minecraftVersion=version, **values)
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def cells(path):
    imported = read_schematic(path)
    return {p + imported.offset: state.to_string() for p, state in imported.volume
            if state.id != "minecraft:air"}


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("format", ("schem", "litematic"))
@pytest.mark.parametrize("archetype", ("house", "tower"))
def test_fixed_cases_make_matrix(tmp_path, capsys, version, format, archetype):
    spec = spec_file(tmp_path, archetype, version)
    path = tmp_path / f"result.{format}"
    code = main(["make", str(spec), "--format", format, "-o", str(path)])
    assert code == 0, capsys.readouterr().out
    assert cells(path)
    record = json.loads(path.with_suffix(path.suffix + ".run.json").read_text(encoding="utf-8"))
    assert record["status"] == "passed"
    assert record["preview"] == "not-run"
    routes = [line for line in record["diagnostics"] if "walkability=" in line]
    assert len(routes) == (2 if archetype == "house" else 4)
    assert all("walkability=reachable" in line for line in routes)


@pytest.mark.parametrize("archetype", ("house", "tower"))
def test_emit_build_and_every_resume_generate_once(tmp_path, monkeypatch, archetype):
    source = spec_file(tmp_path, archetype, materials={"wall_main": "minecraft:stone_bricks"},
                       config={"weathering": {"age": 1, "moss": {"amount": 1},
                                               "cracks": {"amount": 0}}})
    calls = []
    original = pipeline.generate_regions

    def counted(*args, **kwargs):
        calls.append(1)
        yield from original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "generate_regions", counted)
    first = pipeline.make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    expected = cells(first.output)
    assert any("mossy_stone_bricks" in state for state in expected.values())
    concrete = tmp_path / "first.blueprint.json"
    assert main(["validate", str(concrete)]) == 0
    assert main(["build", str(concrete), "-o", str(tmp_path / "built.schem")]) == 0
    assert cells(tmp_path / "built.schem") == expected
    assert len(calls) == 2
    for stage in pipeline.STAGES:
        before = len(calls)
        result = pipeline.make(source, output=tmp_path / f"{stage}.schem",
                               resume=tmp_path / f"first.{stage}.json")
        assert len(calls) == before + 1
        assert cells(result.output) == expected
        assert result.code == first.code
    materialized = json.loads(concrete.read_text(encoding="utf-8"))
    assert materialized["metadata"]["planning"]["element"] == f"{archetype}#1"
    assert materialized["seed"] == 42


def test_preview_of_emitted_blueprint_matches_make_volume(tmp_path, monkeypatch):
    source = spec_file(tmp_path, materials={"wall_main": "minecraft:stone_bricks"},
                       config={"weathering": {"age": 1, "moss": {"amount": 1},
                                               "cracks": {"amount": 0}}})
    result = pipeline.make(source, output=tmp_path / "result.schem", emit=["blueprint"])
    expected = cells(result.output)
    captured = []

    def capture(volume, *_args, **_kwargs):
        captured.extend((pos, state.to_string()) for pos, state in volume
                        if state.id != "minecraft:air")
        return []

    monkeypatch.setattr(cli, "write_previews", capture)
    assert main(["preview", str(tmp_path / "result.blueprint.json"), "--views", "top"]) == 0
    assert {pos: state.split("[", 1)[0] for pos, state in captured} == {
        pos: state.split("[", 1)[0] for pos, state in expected.items()
    }


@pytest.mark.parametrize("change", ("payload", "sidecar", "engine", "lock", "parent", "input"))
def test_resume_rejects_mismatch(tmp_path, monkeypatch, change):
    source = spec_file(tmp_path)
    pipeline.make(source, output=tmp_path / "result.schem", emit=["blueprint"])
    path = tmp_path / "result.blueprint.json"
    if change == "payload":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["seed"] += 1
        path.write_text(json.dumps(data), encoding="utf-8")
    elif change == "sidecar":
        path.with_suffix(".json.meta.json").write_text("{}", encoding="utf-8")
    elif change in ("engine", "lock"):
        environment = pipeline._environment()
        environment["engineHash" if change == "engine" else "lockHash"] = "wrong"
        monkeypatch.setattr(pipeline, "_environment", lambda: environment)
    elif change == "parent":
        (tmp_path / "result.detail.json").write_text("{}", encoding="utf-8")
    else:
        source.write_text(source.read_text(encoding="utf-8") + "\n# 変更\n", encoding="utf-8")
    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.make(source, output=tmp_path / "resumed.schem", resume=path)
    assert exc.value.stage == "Resume"
    assert exc.value.code == 1
    assert not (tmp_path / "resumed.schem").exists()


def test_seed_variation_fixed_ids_and_region_invariance(tmp_path):
    source = spec_file(tmp_path)
    first = pipeline.make(source, output=tmp_path / "first.schem", emit=["blueprint"])
    changed = pipeline.make(source, output=tmp_path / "changed.schem", overrides=["seed=73"])
    assert cells(first.output) != cells(changed.output)
    split = pipeline.make(source, output=tmp_path / "split.schem", overrides=[
        "limits.region=[3,3,3]", "limits.halo=0",
    ])
    assert cells(first.output) == cells(split.output)
    fixed = {"wall_main": "minecraft:stone_bricks", "frame": "minecraft:oak_log",
             "roof": "minecraft:oak_planks", "floor": "minecraft:oak_planks"}
    source = spec_file(tmp_path, materials=fixed,
                       config={"weathering": {"age": 0, "moss": {"amount": 0},
                                               "cracks": {"amount": 0}}})
    a = pipeline.make(source, output=tmp_path / "a.schem")
    b = pipeline.make(source, output=tmp_path / "b.schem", overrides=["seed=73"])
    assert cells(a.output) == cells(b.output)


def test_core_16_32_64_produce_identical_cells(tmp_path):
    source = spec_file(tmp_path)
    outputs = []
    for core in (16, 32, 64):
        result = pipeline.make(
            source, output=tmp_path / f"core-{core}.schem",
            overrides=[f"limits.region=[{core},{core},{core}]", "limits.halo=0"],
        )
        assert result.code == 0
        outputs.append(cells(result.output))
    assert outputs[0] == outputs[1] == outputs[2]


def test_cli_error_codes_and_existing_output(tmp_path, capsys):
    missing = tmp_path / "missing.yaml"
    assert main(["make", str(missing)]) == 2
    source = spec_file(tmp_path, features={"unknown": True})
    assert main(["make", str(source)]) == 1
    assert "FAIL Resolve" in capsys.readouterr().out
    source = spec_file(tmp_path)
    assert main(["make", str(source), "--set", "limits.max_memory_mb=0"]) == 1
    output = tmp_path / "keep.schem"
    output.write_bytes(b"existing")
    source = spec_file(tmp_path, overrides=[{
        "id": "break", "at": [1, 0, 1], "block": "air",
    }])
    assert main(["make", str(source), "-o", str(output)]) == 1
    assert output.read_bytes() == b"existing"
    source = spec_file(tmp_path)
    blocked = tmp_path / "file"
    blocked.write_text("file", encoding="utf-8")
    assert main(["make", str(source), "-o", str(blocked / "result.schem")]) == 2
    source = spec_file(tmp_path, size=[64, 32, 64],
                       elements={"house#1": {"width": 31, "depth": 31}})
    assert main(["make", str(source), "--set", "limits.max_memory_mb=1"]) == 2


def test_evaluation_failed_keeps_artifacts_and_diagnostics(tmp_path, capsys):
    # 色を固定して全役割の明度差をなくし、Materializeの診断を残す。
    source = spec_file(tmp_path, materials={
        "wall_main": "minecraft:oak_planks", "roof": "minecraft:oak_planks",
        "frame": "minecraft:oak_planks", "floor": "minecraft:oak_planks",
    })
    output = tmp_path / "failed.schem"
    assert main(["make", str(source), "-o", str(output)]) == 3
    assert cells(output)
    assert "FAIL palette_relation" in capsys.readouterr().out
    record = json.loads(output.with_suffix(".schem.run.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert all("not-implemented" in line for line in record["diagnostics"]
               if any(metric in line for metric in ("flat_ratio", "column_continuity")))


def test_spool_traversal_limits_and_status(tmp_path):
    from mcblueprint.traversal import check_route
    from mcblueprint.volume import BlockVolume

    volume = BlockVolume()
    for x in range(8):
        volume.set(Vec3(x, 0, 0), BlockState.of("stone"))
    with pytest.raises(BlueprintError, match="max_memory_mb"):
        check_route(volume, Vec3(0, 1, 0), Vec3(7, 1, 0), "1.21.11", max_nodes=1)


def test_failed_export_keeps_completed_file(tmp_path, monkeypatch):
    from mcblueprint.exporters.schem import SchemExporter

    source = spec_file(tmp_path)
    output = tmp_path / "keep.schem"
    output.write_bytes(b"completed")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(SchemExporter, "export_rows", fail)
    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.make(source, output=output)
    assert exc.value.code == 2
    assert exc.value.stage == "Export"
    assert output.read_bytes() == b"completed"
    assert not list(tmp_path.glob(".keep.schem-*-*"))


@pytest.mark.parametrize("failure", ("record", "output", "record_publish"))
def test_failed_publish_keeps_existing_output_and_record(tmp_path, monkeypatch, failure):
    source = spec_file(tmp_path)
    output = tmp_path / "keep.schem"
    record = output.with_suffix(".schem.run.json")
    output.write_bytes(b"old output")
    record.write_bytes(b"old record")

    if failure == "record":
        original = pipeline._atomic_json

        def fail_record(path, value):
            if path.name == record.name:
                raise OSError("disk full")
            original(path, value)

        monkeypatch.setattr(pipeline, "_atomic_json", fail_record)
    else:
        original = pipeline.os.replace
        failed = False

        def fail_output_replace(src, dst):
            nonlocal failed
            target = record if failure == "record_publish" else output
            if Path(dst) == target and not failed:
                failed = True
                raise OSError("disk full")
            original(src, dst)

        monkeypatch.setattr(pipeline.os, "replace", fail_output_replace)

    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.make(source, output=output)

    assert exc.value.code == 2
    assert output.read_bytes() == b"old output"
    assert record.read_bytes() == b"old record"


def test_emit_survives_later_failure(tmp_path):
    source = spec_file(tmp_path, materials={"wall_main": "not-a-material"})
    with pytest.raises(pipeline.PipelineError):
        pipeline.make(source, output=tmp_path / "result.schem", emit=["blueprint"])
    assert (tmp_path / "result.resolve.json").is_file()
    assert (tmp_path / "result.detail.json.meta.json").is_file()
    assert not (tmp_path / "result.blueprint.json").exists()


def test_edited_emit_build_preserves_strict_support_check(tmp_path):
    source = spec_file(tmp_path)
    pipeline.make(source, output=tmp_path / "result.schem", emit=["blueprint"])
    path = tmp_path / "result.blueprint.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["operations"].append({"type": "set", "position": [0, 20, 0], "block": "torch"})
    path.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "strict.schem"
    assert main(["build", str(path), "--strict", "-o", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize("block,status", (("stone", "blocked"), ("oak_fence", "unsupported")))
def test_critic_distinguishes_blocked_and_unsupported(tmp_path, monkeypatch, block, status):
    source = spec_file(tmp_path)
    original = pipeline.generated_blueprint_regions

    def obstructed(blueprint, **kwargs):
        point = Vec3.from_seq(blueprint.metadata["planning"]["arrivals"][-1])
        for region in original(blueprint, **kwargs):
            if region.core.contains(point):
                region.volume.set(point, BlockState.of(block), region.volume.attributes(point))
            yield region

    monkeypatch.setattr(pipeline, "generated_blueprint_regions", obstructed)
    result = pipeline.make(source, output=tmp_path / "obstructed.schem")
    assert result.code == 3
    assert any(f"walkability={status}" in line for line in result.diagnostics)
    assert result.output.exists()


def test_templates_and_help_match_resolve(tmp_path, capsys):
    from mcblueprint.buildspec import resolve_buildspec

    assert main(["archetypes"]) == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()] == ["house", "tower"]
    for archetype in ("house", "tower"):
        assert main(["spec-template", archetype, "--minecraft-version", "26.3"]) == 0
        text = capsys.readouterr().out
        assert 28 <= len(text.splitlines()) <= 34
        resolved = resolve_buildspec(yaml.safe_load(text), project_dir=tmp_path)
        assert resolved.buildspec["archetype"] == archetype
    for topic in ("buildspec", "operations", "materials", "resume"):
        assert main(["help", topic]) == 0
        assert "docs/" in capsys.readouterr().out
