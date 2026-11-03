import json

import pytest
import yaml

from mcblueprint.cli import build_parser, main
from mcblueprint.edits import apply_edits_file
from mcblueprint.errors import BlueprintError


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
