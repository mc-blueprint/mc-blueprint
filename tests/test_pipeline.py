"""makeの固定課題、再開、旧build互換と段階失敗。"""

import base64
import gzip
import hashlib
import json
import multiprocessing
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml

from mcblueprint import cli, pipeline
from mcblueprint.cli import main
from mcblueprint.errors import BlueprintError
from mcblueprint.importers import read_schematic
from mcblueprint.model import BlockState, Vec3
from mcblueprint.volume import CellAttributes

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
    data.setdefault("config", {}).setdefault("preview", {})["enabled"] = False
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.mark.parametrize("resume_stage", [None, "detail", "blueprint"])
def test_opening_recess_diagnostics_survive_make_and_resume(
    tmp_path, monkeypatch, capsys, recwarn, resume_stage,
):
    source = spec_file(tmp_path)
    checkpoint = None
    if resume_stage:
        pipeline.make(source, output=tmp_path / "first.schem", emit=[resume_stage])
        checkpoint = tmp_path / f"first.{resume_stage}.json"

        def unexpected(*args, **kwargs):
            pytest.fail("保存済みのDetail計画を再生成した")

        monkeypatch.setattr(pipeline, "plan_buildspec", unexpected)
        capsys.readouterr()

    args = ["make", str(source), "-o", str(tmp_path / "result.schem")]
    if checkpoint:
        args.extend(["--resume", str(checkpoint)])
    assert main(args) == 0
    output = capsys.readouterr().out
    record = json.loads((tmp_path / "result.schem.run.json").read_text(encoding="utf-8"))
    blocked = [d for d in record["structuredDiagnostics"]
               if d["code"] == "opening_recess_blocked"]
    assert blocked
    for diagnostic in blocked:
        assert diagnostic["severity"] == "warn"
        assert diagnostic["metric"] == "opening_recess"
        assert diagnostic["actual"] == 0 and diagnostic["expected"] == 1
        assert diagnostic["source"] == "config.decoration.depth"
        assert diagnostic["repairPaths"] == ["config.decoration.depth"]
        assert "予約" in diagnostic["reason"]
        line = pipeline._diagnostic_line(diagnostic)
        assert line.startswith("WARN opening_recess_blocked ")
        assert record["diagnostics"].count(line) == 1
        assert output.count(line) == 1
    if checkpoint:
        first = json.loads((tmp_path / "first.schem.run.json").read_text(encoding="utf-8"))
        assert record["structuredDiagnostics"] == first["structuredDiagnostics"]
        assert record["diagnostics"] == first["diagnostics"]
    assert not recwarn


@pytest.mark.parametrize("resume", [False, True])
def test_make_scene_uses_completed_massing_without_replanning(tmp_path, monkeypatch, resume):
    from mcblueprint import viewer_input

    source = spec_file(tmp_path)
    checkpoint = None
    if resume:
        pipeline.make(source, output=tmp_path / "first.schem", emit=["blueprint"])
        checkpoint = tmp_path / "first.blueprint.json"

    def unexpected(*args, **kwargs):
        pytest.fail("完成済みの計画をscene出典のために再生成した")

    monkeypatch.setattr(viewer_input, "plan_buildspec", unexpected)
    if resume:
        monkeypatch.setattr(pipeline, "plan_buildspec", unexpected)
    result = pipeline.make(source, output=tmp_path / "result.schem", resume=checkpoint)
    scene = json.loads(gzip.decompress((tmp_path / "result.scene.json.gz").read_bytes()))

    assert result.code == 0
    root = next(entry for entry in scene["sources"] if entry.get("element") == "house#1")
    assert "width" in root["params"]
    if resume:
        assert (tmp_path / "result.scene.json.gz").read_bytes() == (
            tmp_path / "first.scene.json.gz").read_bytes()


@pytest.mark.parametrize("open_browser", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_cli_make_serves_completed_scene_once(tmp_path, monkeypatch, open_browser, failed):
    from mcblueprint import assets, viewer_server

    source = spec_file(tmp_path, config={"decoration": {"depth": 0}} if failed else {})
    calls = []
    generated = pipeline.generate_regions

    def counted(*args, **kwargs):
        calls.append("generate")
        yield from generated(*args, **kwargs)

    def serve(prepared, cache, **kwargs):
        calls.append("serve")
        prepared.check_generation()
        assert prepared.input == source.resolve()
        assert prepared.spec
        assert kwargs["open_browser"] is open_browser
        assert any(s.get("params") for s in prepared.scene["sources"])
        record = json.loads(prepared.output.with_suffix(".schem.run.json").read_bytes())
        assert record["preview"] == "3d"
        # 表示中の再生成が更新したrunを、終了時に古い記録で上書きしない。
        record["regenerated"] = True
        prepared.output.with_suffix(".schem.run.json").write_text(json.dumps(record))

    monkeypatch.setattr(pipeline, "generate_regions", counted)
    monkeypatch.setattr(assets, "ensure_assets", lambda *a: tmp_path)
    monkeypatch.setattr(viewer_server, "serve", serve)
    target = tmp_path / "result.schem"
    assert main(["make", str(source), "-o", str(target),
                 "--set", "preview.enabled=true", "--set",
                 f"preview.open_browser={str(open_browser).lower()}"]) == (3 if failed else 0)
    assert calls == ["generate", "serve"]
    record = json.loads(target.with_suffix(".schem.run.json").read_bytes())
    assert record["regenerated"]
    assert record["status"] == ("failed" if failed else "passed")
    if failed:
        assert any(d["code"] == "E201" for d in record["structuredDiagnostics"])


@pytest.mark.parametrize("fmt", ["schem", "litematic"])
@pytest.mark.parametrize("failed", [False, True])
def test_cli_make_2d_without_browser_or_assets(tmp_path, monkeypatch, fmt, failed):
    from mcblueprint import assets, viewer_server

    def unexpected(*args, **kwargs):
        pytest.fail("2D表示で3Dの準備を呼び出した")

    monkeypatch.setattr(assets, "ensure_assets", unexpected)
    monkeypatch.setattr(viewer_server, "serve", unexpected)
    source = spec_file(tmp_path, config={"decoration": {"depth": 0}} if failed else {})
    target = tmp_path / f"result.{fmt}"
    assert main(["make", str(source), "--format", fmt, "-o", str(target),
                 "--set", "preview.enabled=true", "--set",
                 "preview.mode=2d"]) == (3 if failed else 0)
    assert len(list((tmp_path / "preview").glob("*.png"))) == 4
    record = json.loads(target.with_suffix(f".{fmt}.run.json").read_bytes())
    assert record["preview"] == "2d"
    assert record["status"] == ("failed" if failed else "passed")


@pytest.mark.parametrize("failed", [False, True])
def test_cli_preview_failure_preserves_generation_status(tmp_path, monkeypatch, failed):
    from mcblueprint import assets

    def fail(*args):
        raise BlueprintError("アセットがありません")

    monkeypatch.setattr(assets, "ensure_assets", fail)
    source = spec_file(tmp_path, config={"decoration": {"depth": 0}} if failed else {})
    target = tmp_path / "result.schem"
    assert main(["make", str(source), "-o", str(target),
                 "--set", "preview.enabled=true"]) == 2
    record = json.loads(target.with_suffix(".schem.run.json").read_bytes())
    assert target.is_file()
    assert record["status"] == ("failed" if failed else "passed")
    assert record["preview"] == "failed"
    assert record["previewError"] == "アセットがありません"


def test_cli_make_conflict_then_release(tmp_path):
    from mcblueprint.output_lock import lock_paths

    source = spec_file(tmp_path)
    target = tmp_path / "result.schem"
    with lock_paths(target):
        assert main(["make", str(source), "-o", str(target)]) == 2
        assert not target.exists()
    assert main(["make", str(source), "-o", str(target)]) == 0


def _hold_generation_paths(source, output, ready, release):
    from mcblueprint.output_lock import lock_paths

    with lock_paths(Path(source), Path(output)):
        ready.set()
        if not release.wait(30):
            raise TimeoutError("ロック検証の解放待ちがタイムアウトしました")


def _update_generation_input(source, ready, result):
    from mcblueprint.output_lock import lock_paths

    if not ready.wait(30):
        raise TimeoutError("入力更新の開始待ちがタイムアウトしました")
    try:
        with lock_paths(Path(source)):
            data = yaml.safe_load(Path(source).read_bytes())
            data["seed"] = 42
            Path(source).write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")
        result.put("updated")
    except BlockingIOError:
        result.put("conflict")


def test_make_keeps_input_generation_across_process_update(tmp_path, monkeypatch):
    source = spec_file(tmp_path, seed=0)
    ctx = multiprocessing.get_context("spawn")
    ready, result = ctx.Event(), ctx.Queue()
    process = ctx.Process(target=_update_generation_input, args=(str(source), ready, result))
    resolve = pipeline.resolve_buildspec
    outcomes = []

    def resolve_then_update(*args, **kwargs):
        resolved = resolve(*args, **kwargs)
        ready.set()
        outcomes.append(result.get(timeout=10))
        return resolved

    monkeypatch.setattr(pipeline, "resolve_buildspec", resolve_then_update)
    process.start()
    try:
        made = pipeline.make(source, output=tmp_path / "result.schem")
        record = json.loads(made.record.read_bytes())
        meta = json.loads((tmp_path / "result.scene.json.gz.meta.json").read_bytes())
        scene = json.loads(gzip.decompress((tmp_path / "result.scene.json.gz").read_bytes()))
        assert record["seed"] == yaml.safe_load(source.read_bytes())["seed"]
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        assert record["input"]["sha256"] == meta["inputSha256"] == digest
        assert scene["source"]["sha256"] == digest
        assert outcomes == ["conflict"]
    finally:
        ready.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        result.close()
        result.join_thread()
    assert process.exitcode == 0


@pytest.mark.parametrize(("options", "expected"), [
    (["palette.materials.wall_main=minecraft:stone", "palette.materials={}"], {}),
    (["palette.materials.wall_main=minecraft:stone",
      "palette.materials={roof: minecraft:oak_planks}"], {"roof": "minecraft:oak_planks"}),
    (["palette.materials={}", "palette.materials.wall_main=minecraft:stone"],
     {"wall_main": "minecraft:stone"}),
])
def test_make_records_parent_cli_override_without_removed_child(tmp_path, options, expected):
    source = spec_file(tmp_path)
    made = pipeline.make(source, output=tmp_path / "result.schem", overrides=options)
    meta = json.loads((tmp_path / "result.scene.json.gz.meta.json").read_bytes())
    assert made.code in (0, 3)
    assert meta["cliOverrides"] == {"palette.materials": expected}


@pytest.mark.parametrize("timing", ["relock", "publish"])
def test_make_rejects_changed_input_without_replacing_outputs(tmp_path, monkeypatch, timing):
    from mcblueprint import output_lock

    source = spec_file(tmp_path, seed=0)
    target = tmp_path / "result.schem"
    outputs = [target, target.with_suffix(".schem.run.json"),
               tmp_path / "result.scene.json.gz", tmp_path / "result.scene.json.gz.meta.json"]
    for path in outputs:
        path.write_bytes(b"existing")
    lock_paths = output_lock.lock_paths

    def update():
        data = yaml.safe_load(source.read_bytes())
        data["seed"] = 42
        source.write_text(yaml.safe_dump(data), encoding="utf-8", newline="\n")

    @contextmanager
    def update_before_lock(*paths):
        if timing == "relock" and len(paths) == 2:
            with lock_paths(source):
                update()
        with lock_paths(*paths):
            yield

    def hook(stage):
        if timing == "publish" and stage == "Export":
            update()

    monkeypatch.setattr(output_lock, "lock_paths", update_before_lock)
    with pytest.raises(pipeline.PipelineError, match="入力が変更") as exc:
        pipeline.make(source, output=target, stage_hook=hook)
    assert exc.value.code == 2
    assert all(path.read_bytes() == b"existing" for path in outputs)
    with lock_paths(source, target):
        pass


@pytest.mark.parametrize("entry", ["python", "cli"])
@pytest.mark.parametrize("resource", ["input", "output"])
def test_make_rejects_other_process_generation(tmp_path, entry, resource):
    source = spec_file(tmp_path)
    other = tmp_path / "other.yaml"
    other.write_bytes(source.read_bytes())
    target = tmp_path / "same.litematic"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold_generation_paths, args=(
        str(source if resource == "input" else other), str(tmp_path / "same.schem"),
        ready, release,
    ))
    process.start()
    try:
        assert ready.wait(10)
        if entry == "python":
            with pytest.raises(pipeline.PipelineError) as exc:
                pipeline.make(source, output=target, overrides=["output.format=litematic"])
            assert exc.value.code == 2
        else:
            assert main(["make", str(source), "-o", str(target),
                         "--format", "litematic"]) == 2
        assert not target.exists()
        assert not (tmp_path / "same.scene.json.gz").exists()
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0
    assert pipeline.make(source, output=target,
                         overrides=["output.format=litematic"]).code == 0


def test_make_releases_input_and_output_after_failure(tmp_path):
    from mcblueprint.output_lock import lock_paths

    source = spec_file(tmp_path)
    target = tmp_path / "result.schem"

    def fail(stage):
        if stage == "Site/Massing/Detail":
            with pytest.raises(BlockingIOError):
                with lock_paths(source):
                    pass
            with pytest.raises(BlockingIOError):
                with lock_paths(target.with_suffix(".litematic")):
                    pass
            raise OSError("生成失敗")

    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.make(source, output=target, stage_hook=fail)
    assert exc.value.code == 2
    assert pipeline.make(source, output=target).code == 0


def cells(path):
    imported = read_schematic(path)
    return {p + imported.offset: state.to_string() for p, state in imported.volume
            if state.id != "minecraft:air"}


def scene_cells(path):
    scene_path = path.with_name(f"{path.stem}.scene.json.gz")
    scene = json.loads(gzip.decompress(scene_path.read_bytes()))
    result = {}
    for chunk in scene["chunks"]:
        x0, y0, z0 = chunk["at"]
        sx, sy, sz = (min(16, scene["size"][i] - chunk["at"][i]) for i in range(3))
        dtype = "<u2" if scene["indexWidth"] == 16 else "<u4"
        indexes = np.frombuffer(base64.b64decode(chunk["blocks"]), dtype=dtype)
        for index, palette_index in enumerate(indexes):
            if palette_index:
                x = index % sx
                z = index // sx % sz
                y = index // (sx * sz)
                pos = tuple(scene["origin"][i] + (x, y, z)[i] + (x0, y0, z0)[i]
                            for i in range(3))
                state = scene["palette"][int(palette_index)]["state"]
                if state != "minecraft:air":
                    result[pos] = state
    return result


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("format", ("schem", "litematic"))
@pytest.mark.parametrize("archetype", ("house", "tower"))
def test_fixed_cases_make_matrix(tmp_path, capsys, version, format, archetype):
    spec = spec_file(tmp_path, archetype, version)
    path = tmp_path / f"result.{format}"
    code = main(["make", str(spec), "--format", format, "-o", str(path)])
    assert code in (0, 3), capsys.readouterr().out
    assert cells(path)
    expected = {tuple(pos): state for pos, state in cells(path).items()}
    scene_path = path.with_name(f"{path.stem}.scene.json.gz")
    scene_bytes = gzip.decompress(scene_path.read_bytes())
    assert scene_cells(path) == expected
    scene_meta = json.loads(Path(f"{scene_path}.meta.json").read_text(encoding="utf-8"))
    assert scene_meta["sceneSha256"] == hashlib.sha256(scene_bytes).hexdigest()
    assert scene_meta["inputSha256"] == hashlib.sha256(spec.read_bytes()).hexdigest()
    record = json.loads(path.with_suffix(path.suffix + ".run.json").read_text(encoding="utf-8"))
    # 版ごとの材質候補で配色関係が解けない場合も、外壁品質は独立して検証する。
    expected_code = 0 if record["metrics"]["palette_relation"]["value"] else 3
    assert code == expected_code
    assert record["status"] == ("passed" if expected_code == 0 else "failed")
    assert all(d["code"] == "palette_relation" for d in record["structuredDiagnostics"]
               if d["severity"] in ("error", "fail"))
    assert record["metrics"]["column_continuity"]["value"] == 1
    assert record["metrics"]["beam_continuity"]["value"] == 1
    assert record["metrics"]["decoration_density"]
    assert "status" not in record["metrics"]["decoration_density"]
    densities = record["metrics"]["decoration_density"]
    assert len(densities) == (8 if archetype == "house" else 18)
    assert all(20 <= item["value"] <= 40 for item in densities.values())
    assert record["metrics"]["flat_ratio"]["value"] <= 0.35
    assert record["metrics"]["flat_ratio"]["denominator"] == (16 if archetype == "house" else 30)
    assert record["metrics"]["facade_surface_missing"]["denominator"] == (
        212 if archetype == "house" else 295
    )
    assert not any(d["code"] in ("E201", "E202") for d in record["structuredDiagnostics"])
    assert not any(d["code"] in ("E101", "E203") for d in record["structuredDiagnostics"])
    assert record["preview"] == "disabled"
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
    expected_scene = (tmp_path / "first.scene.json.gz").read_bytes()
    expected_metrics = json.loads(first.record.read_text(encoding="utf-8"))["metrics"]
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
        assert (tmp_path / f"{stage}.scene.json.gz").read_bytes() == expected_scene
        assert result.code == first.code
        assert json.loads(result.record.read_text(encoding="utf-8"))["metrics"] == expected_metrics
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
    assert (tmp_path / "first.scene.json.gz").read_bytes() == (
        tmp_path / "split.scene.json.gz"
    ).read_bytes()
    assert json.loads(first.record.read_text(encoding="utf-8"))["metrics"] == json.loads(
        split.record.read_text(encoding="utf-8"))["metrics"]
    fixed = {"wall_main": "minecraft:stone_bricks", "frame": "minecraft:oak_log",
             "roof": "minecraft:oak_planks", "floor": "minecraft:oak_planks"}
    source = spec_file(tmp_path, materials=fixed,
                       config={"decoration": {"depth": 0},
                               "weathering": {"age": 0, "moss": {"amount": 0},
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
    source = spec_file(tmp_path, config={"decoration": {"depth": 0}}, overrides=[{
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
    assert all("not-implemented" not in line for line in record["diagnostics"])
    assert record["metrics"]["flat_ratio"]["denominator"] > 0
    assert record["metrics"]["column_continuity"]["value"] == 1


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


@pytest.mark.parametrize("fixed", (False, True))
def test_measured_success_and_warning_only(tmp_path, monkeypatch, fixed):
    source = spec_file(tmp_path)
    convert = pipeline.plan_from_metadata
    generate = pipeline.generated_blueprint_regions
    selected = []

    def planned(*args, **kwargs):
        plan = convert(*args, **kwargs)
        facade = plan.facades[0]
        cells = tuple(c for c in facade.cells if c.v == 0 and not c.opening and c.u < 4)
        selected.extend(cells)
        # 密度下限0の計画で、装飾なしでも材質混合のWARNだけを検証する。
        return replace(plan, facades=(replace(facade, cells=cells),),
                       decoration_operations={},
                       decoration_range=(0.0, plan.decoration_range[1]),
                       fixed_roles=frozenset({"wall_main"}) if fixed else frozenset())

    def sculpted(*args, **kwargs):
        for region in generate(*args, **kwargs):
            for cell in selected:
                if region.core.contains(cell.pos):
                    attr = region.volume.attributes(cell.pos)
                    if attr.reserved is None:
                        region.volume.set(cell.pos, BlockState.of("stone"), attr)
                pos = cell.pos + Vec3(0, 0, -1)
                if cell.u % 2 == 0 and region.core.contains(pos):
                    region.volume.set(pos, BlockState.of("stone"), CellAttributes(
                        role="wall_main", face="north", element="house#1.facade.north.floor#1"))
            yield region

    monkeypatch.setattr(pipeline, "plan_from_metadata", planned)
    monkeypatch.setattr(pipeline, "generated_blueprint_regions", sculpted)
    result = pipeline.make(source, output=tmp_path / "passed.schem")
    assert result.code == 0
    record = json.loads(result.record.read_text(encoding="utf-8"))
    assert record["status"] == "passed"
    assert record["metrics"]["flat_ratio"]["value"] == 0
    assert any(d["code"] == "E305" for d in record["structuredDiagnostics"]) is not fixed
    assert all(d["severity"] in ("ok", "warn") for d in record["structuredDiagnostics"])


@pytest.mark.parametrize("failure", ("structure", "attributes", "memory", "measurement", "plan"))
def test_critic_failure_preserves_existing_artifacts(tmp_path, monkeypatch, failure):
    source = spec_file(tmp_path)
    output = tmp_path / "keep.schem"
    targets = (output, output.with_suffix(".schem.run.json"),
               tmp_path / "keep.scene.json.gz", tmp_path / "keep.scene.json.gz.meta.json")
    for target in targets:
        target.write_bytes(b"existing")
    original = pipeline.generated_blueprint_regions

    def broken(blueprint, **kwargs):
        point = Vec3.from_seq(blueprint.metadata["planning"]["columns"][0]["from"])
        for region in original(blueprint, **kwargs):
            if region.core.contains(point):
                attr = region.volume.attributes(point)
                if failure == "structure":
                    region.volume.set(point, BlockState.of("air"), attr)
                elif failure == "attributes":
                    # 予約ownerを保ったまま、frame属性だけを破壊する。
                    region.volume.set(point, region.volume.get(point), replace(attr, role=None))
            yield region

    if failure in ("structure", "attributes"):
        monkeypatch.setattr(pipeline, "generated_blueprint_regions", broken)
    elif failure == "plan":
        def absent(*args, **kwargs):
            raise BlueprintError("Critic計画に期待柱がありません")
        monkeypatch.setattr(pipeline, "plan_from_metadata", absent)
    else:
        def fail(self, region):
            if failure == "memory":
                raise MemoryError("Criticメモリ不足")
            raise ValueError("計測失敗")
        monkeypatch.setattr(pipeline.CriticAccumulator, "consume", fail)
    with pytest.raises(pipeline.PipelineError) as exc:
        pipeline.make(source, output=output)
    assert exc.value.code == (2 if failure == "memory" else 1)
    if failure in ("structure", "attributes"):
        assert "E101" in str(exc.value)
    assert all(target.read_bytes() == b"existing" for target in targets)
    assert not list(tmp_path.glob(".keep.schem-*"))


@pytest.mark.parametrize("archetype,shape", (("house", None), ("tower", "round"),
                                            ("tower", "square")))
@pytest.mark.parametrize("angle", (0, 90))
def test_plan_conversion_invariants(tmp_path, archetype, shape, angle):
    values = {"elements": {f"{archetype}#1": {"angle": angle}}}
    if shape:
        values["features"] = {"shape": shape, "floors": 3, "floor_height": 4}
    source = spec_file(tmp_path, archetype, **values)
    result = pipeline.make(source, output=tmp_path / "shape.schem")
    record = json.loads(result.record.read_text(encoding="utf-8"))
    assert record["metrics"]["column_continuity"]["value"] == 1
    assert record["metrics"]["beam_continuity"]["value"] == 1
    assert record["metrics"]["facade_surface_missing"]["value"] == 0
    assert record["metrics"]["flat_ratio"]["denominator"] > 0


@pytest.mark.parametrize("archetype", ("house", "tower"))
@pytest.mark.parametrize("angle", (90, 180, 270))
def test_rotated_fixed_cases_preserve_local_quality(tmp_path, archetype, angle):
    source = spec_file(tmp_path, archetype, elements={f"{archetype}#1": {"angle": angle}})
    result = pipeline.make(source, output=tmp_path / "rotated.schem")
    record = json.loads(result.record.read_text(encoding="utf-8"))
    failures = [d for d in record["structuredDiagnostics"] if d["severity"] in ("fail", "error")]
    assert result.code == 0, failures
    assert not any(d["code"] in ("E201", "E202") for d in record["structuredDiagnostics"])
    assert record["metrics"]["flat_ratio"]["value"] <= 0.35
    assert record["metrics"]["column_continuity"]["value"] == 1
    assert record["metrics"]["facade_surface_missing"]["value"] == 0
    densities = record["metrics"]["decoration_density"]
    assert len(densities) == (8 if archetype == "house" else 18)
    assert all(20 <= value["value"] <= 40 for value in densities.values())


def test_structured_repair_paths_resolve(tmp_path):
    from mcblueprint.buildspec import resolve_buildspec

    source = spec_file(tmp_path, materials={
        "wall_main": "minecraft:oak_planks", "roof": "minecraft:oak_planks",
        "frame": "minecraft:oak_planks", "floor": "minecraft:oak_planks",
    })
    result = pipeline.make(source, output=tmp_path / "paths.schem")
    record = json.loads(result.record.read_text(encoding="utf-8"))
    fields = {"code", "metric", "severity", "element", "source", "actual", "expected",
              "repairPaths"}
    for diagnostic in record["structuredDiagnostics"]:
        assert fields <= diagnostic.keys()
        assert pipeline._diagnostic_line(diagnostic) in record["diagnostics"]
        for path in diagnostic["repairPaths"]:
            assert path.startswith("config.")
            key = path.removeprefix("config.")
            value = record["config"]
            for part in key.split("."):
                value = value[part]
            override = f"{key}={yaml.safe_dump(value).splitlines()[0]}"
            resolve_buildspec(source, overrides=[override])


def test_missing_facade_attributes_are_quality_failure(tmp_path, monkeypatch):
    source = spec_file(tmp_path)
    generate = pipeline.generated_blueprint_regions
    removed = []

    def broken(*args, **kwargs):
        for region in generate(*args, **kwargs):
            if not removed:
                for pos, state in region.volume:
                    attr = region.volume.attributes(pos)
                    if (region.core.contains(pos) and attr.face == "north"
                            and attr.role == "wall_main" and attr.reserved is None):
                        region.volume.set(pos, state, CellAttributes())
                        removed.append(pos)
                        break
            yield region

    monkeypatch.setattr(pipeline, "generated_blueprint_regions", broken)
    result = pipeline.make(source, output=tmp_path / "missing.schem")
    assert removed and result.code == 3
    record = json.loads(result.record.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    missing = [d for d in record["structuredDiagnostics"] if d["code"] == "E203"]
    assert missing and missing[0]["face"] == "north"
    assert missing[0]["repairPaths"] == ["config.weathering.missing_blocks"]


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
