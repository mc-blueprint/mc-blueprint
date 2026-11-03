import json

import pytest
import yaml

from mcblueprint.cli import build_parser, main
from mcblueprint.edits import apply_edits_file
from mcblueprint.errors import BlueprintError
from mcblueprint.viewer_input import apply_input_edits, prepare_input


@pytest.fixture
def generation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    source = tmp_path / "tower.yaml"
    source.write_text('minecraftVersion: "1.21.11"\narchetype: tower\nscale: small\n'
                      'config:\n  preview: {enabled: false}\n', encoding="utf-8")
    assert main(["make", str(source), "-o", str(tmp_path / "tower.schem"),
                 "--set", "seed=7", "--seed", "42", "--set", "weathering.age=0"]) in (0, 3)
    scene = tmp_path / "tower.scene.json.gz"
    meta = json.loads(scene.with_suffix(".gz.meta.json").read_bytes())
    edits = tmp_path / "tower.edits.json"
    edits.write_text(json.dumps({"formatVersion": 1, "scene": meta["sceneSha256"], "edits": [
        {"id": "height", "kind": "param", "element": "tower#1", "param": "height", "value": 14},
    ]}), encoding="utf-8")
    return source, scene, edits, meta


def test_apply_edits_parser_accepts_generation_options():
    args = build_parser().parse_args([
        "apply-edits", "tower.edits.json", "--spec", "tower.yaml", "--scene", "tower.scene.json.gz",
        "--set", "seed=7", "--seed", "42", "--set", "weathering.age=0",
    ])
    assert args.seed == 42
    assert args.set == ["seed=7", "weathering.age=0"]


def test_make_records_normalized_cli_overrides(generation):
    assert generation[3]["cliOverrides"] == {"seed": 42, "weathering.age": 0}


def test_apply_edits_file_reproduces_generation_options(generation, tmp_path):
    source, scene, edits, _ = generation
    before = source.read_bytes(), scene.read_bytes(), edits.read_bytes()
    target = tmp_path / "updated.yaml"
    assert apply_edits_file(scene, edits, source, target,
                            overrides=["weathering.age=0x0", "seed=42"]) == target
    assert yaml.safe_load(target.read_bytes())["elements"]["tower#1"]["height"] == 14
    assert before == (source.read_bytes(), scene.read_bytes(), edits.read_bytes())


@pytest.mark.parametrize(("overrides", "difference"), [
    ([], "seed.*weathering.age"),
    (["seed=42"], "weathering.age"),
    (["seed=41", "weathering.age=0"], "seed"),
    (["seed=42", "weathering.age=0", "preview.enabled=false"], "preview.enabled"),
])
def test_apply_edits_file_rejects_different_cli_options(
    generation, tmp_path, overrides, difference,
):
    source, scene, edits, _ = generation
    before = source.read_bytes()
    target = tmp_path / "updated.yaml"
    target.write_bytes(b"existing")
    with pytest.raises(BlueprintError, match=f"CLI指定.*{difference}"):
        apply_edits_file(scene, edits, source, target, overrides=overrides)
    assert source.read_bytes() == before
    assert target.read_bytes() == b"existing"


def test_apply_edits_file_still_checks_composed_config(generation, tmp_path):
    source, scene, edits, _ = generation
    (tmp_path / "mc-blueprint.yaml").write_text("decoration: {depth: 0}\n", encoding="utf-8")
    with pytest.raises(BlueprintError, match="BuildSpecまたは設定"):
        apply_edits_file(scene, edits, source, tmp_path / "updated.yaml",
                         overrides=["seed=42", "weathering.age=0"])


def test_apply_edits_file_requires_current_sidecar(generation, tmp_path):
    source, scene, edits, meta = generation
    meta.pop("cliOverrides")
    scene.with_suffix(".gz.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(BlueprintError, match="sidecar.*CLI指定.*再生成"):
        apply_edits_file(scene, edits, source, tmp_path / "updated.yaml",
                         overrides=["seed=42", "weathering.age=0"])


def test_cli_apply_edits_reproduces_seed_and_settings(generation, tmp_path):
    source, scene, edits, _ = generation
    target = tmp_path / "updated.yaml"
    before = source.read_bytes(), scene.read_bytes(), edits.read_bytes()
    assert main(["apply-edits", str(edits), "--spec", str(source), "--scene", str(scene),
                 "-o", str(target), "--set", "seed=7", "--seed", "42",
                 "--set", "weathering.age=0"]) == 0
    assert yaml.safe_load(target.read_bytes())["elements"]["tower#1"]["height"] == 14
    assert before == (source.read_bytes(), scene.read_bytes(), edits.read_bytes())


@pytest.mark.parametrize("options", [[], ["--seed", "41", "--set", "weathering.age=0"]])
def test_cli_apply_edits_rejects_missing_or_changed_options(generation, capsys, options):
    source, scene, edits, _ = generation
    before = source.read_bytes()
    assert main(["apply-edits", str(edits), "--spec", str(source), "--scene", str(scene),
                 *options]) == 2
    assert "CLI指定" in capsys.readouterr().err
    assert source.read_bytes() == before


def test_regenerated_scene_keeps_cli_options_for_apply_edits(generation, tmp_path):
    source, _, _, _ = generation
    prepared = prepare_input(source, output=tmp_path / "view.schem",
                             overrides=["seed=42", "weathering.age=0"])
    prepared.save_edits({"formatVersion": 1, "scene": prepared.scene_hash, "edits": [
        {"id": "height", "kind": "param", "element": "tower#1", "param": "height", "value": 14},
    ]})
    assert prepared.regenerate()["code"] in (0, 3)
    prepared.check_generation()
    meta = json.loads(prepared.scene_path.with_suffix(".gz.meta.json").read_bytes())
    assert meta["cliOverrides"] == {"seed": 42, "weathering.age": 0}
    prepared.save_edits({"formatVersion": 1, "scene": prepared.scene_hash, "edits": [
        {"id": "note", "kind": "note", "element": "tower#1", "text": "再生成後を確認"},
    ]})
    assert main(["apply-edits", str(prepared.edits_path), "--spec", str(source),
                 "--scene", str(prepared.scene_path), "--seed", "42", "--set", "weathering.age=0",
                 "-o", str(tmp_path / "updated.yaml")]) == 0


def test_non_spec_rejects_generation_options(tmp_path):
    with pytest.raises(BlueprintError, match="--spec"):
        apply_input_edits(tmp_path / "edits.json", tmp_path / "input.json",
                          tmp_path / "scene.json.gz", overrides=["seed=42"])


def test_cli_format_option_can_be_reproduced_with_set(generation, tmp_path):
    source, _, edits, _ = generation
    assert main(["make", str(source), "--format", "litematic", "--seed", "0",
                 "-o", str(tmp_path / "formatted.litematic")]) in (0, 3)
    scene = tmp_path / "formatted.scene.json.gz"
    meta = json.loads(scene.with_suffix(".gz.meta.json").read_bytes())
    assert meta["cliOverrides"] == {"output.format": "litematic", "seed": 0}
    document = json.loads(edits.read_bytes())
    document["scene"] = meta["sceneSha256"]
    edits.write_text(json.dumps(document), encoding="utf-8")
    assert main(["apply-edits", str(edits), "--spec", str(source), "--scene", str(scene),
                 "--set", "output.format=litematic"]) == 2
    assert main(["apply-edits", str(edits), "--spec", str(source), "--scene", str(scene),
                 "--set", "output.format=litematic", "--seed", "0"]) == 0
