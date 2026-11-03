import base64
import gzip
import hashlib
import json

import nbtlib
import numpy as np
import pytest
import yaml

from mcblueprint import blockdata
from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.cli import main
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.generator import generate
from mcblueprint.importers import read_schematic
from mcblueprint.pipeline import make
from mcblueprint.planning import plan_buildspec
from mcblueprint.viewer_input import (
    EditConflict,
    _scene_blueprint,
    _validated,
    apply_input_edits,
    prepare_input,
    read_scene,
)


def blueprint(path):
    data = {"formatVersion": 1, "minecraftVersion": "1.21.11", "name": "試験",
            "operations": [{"type": "fill", "from": [-2, 0, 0], "to": [2, 1, 1],
                            "block": "minecraft:stone"}]}
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def edits(prepared, values):
    return {"formatVersion": 1, "scene": prepared.scene_hash, "edits": values}


@pytest.mark.parametrize("archetype", ["house", "tower"])
def test_selected_child_edits_root_params(tmp_path, archetype):
    source = tmp_path / f"{archetype}.yaml"
    source.write_text(yaml.safe_dump({
        "minecraftVersion": "1.21.11", "archetype": archetype, "scale": "small",
        "seed": 42, "config": {"weathering": {"age": 0}},
    }), encoding="utf-8")
    massing = plan_buildspec(resolve_buildspec(source)).massing
    root = massing["element"]
    result = make(source, output=tmp_path / "made.schem")
    _, scene = read_scene(result.output.with_name("made.scene.json.gz"))
    used = set()
    for chunk in scene["chunks"]:
        blocks = np.frombuffer(base64.b64decode(chunk["blocks"]), dtype="<u2")
        ids = np.frombuffer(base64.b64decode(chunk["sources"]), dtype="<u4")
        used.update(map(int, ids[blocks != 0]))
    selected = [entry for entry in scene["sources"] if entry["id"] in used]
    assert selected
    for entry in selected:
        if entry.get("element", "").startswith(root):
            assert entry["element"] == root
            assert entry["params"] == sorted(massing["params"])
            assert entry.get("spec") and entry.get("path")
    assert any("facade" in entry.get("path", "") or "body" in entry.get("path", "")
               for entry in selected)
    assert any("frame" in entry.get("path", "") for entry in selected)
    top = "roof" if archetype == "house" else "body"
    assert any(top in entry.get("path", "") for entry in selected)

    prepared = prepare_input(source, output=tmp_path / "view")
    before = prepared.scene["size"]
    param, value = ("width", 19) if archetype == "house" else ("height", 14)
    descriptor = next(entry for entry in prepared.scene["sources"]
                      if top in entry.get("path", ""))
    assert param in descriptor["params"]
    prepared.save_edits(edits(prepared, [
        {"id": "param", "kind": "param", "element": descriptor["element"],
         "param": param, "value": value},
    ]))
    assert prepared.regenerate()["code"] in (0, 3)
    assert prepared.scene["size"] != before
    assert yaml.safe_load(source.read_bytes())["elements"][root][param] == value
    prepared.check_generation()
    with pytest.raises(BlueprintError, match="許可"):
        prepared.save_edits(edits(prepared, [
            {"id": "bad", "kind": "param", "element": root + ".body#1",
             "param": param, "value": value},
        ]))


def test_four_inputs_and_no_scene_provenance_open(tmp_path):
    source = tmp_path / "test.json"
    data = blueprint(source)
    prepared = prepare_input(source, output=tmp_path / "json")
    assert prepared.scene["origin"] == [-2, 0, 0]
    assert prepared.scene["source"]["kind"] == "blueprint"
    assert not prepared.spec
    schematic = tmp_path / "test.schem"
    model = _validated(data, tmp_path)
    EXPORTERS["schem"]().export(generate(model), model, schematic)
    imported = prepare_input(schematic, output=tmp_path / "schem")
    assert imported.scene["minecraftVersion"] == "1.21.11"
    assert imported.scene["source"]["kind"] == "schematic"
    scene = prepared.scene | {"source": {"kind": "spec", "path": "Z:/must-not-open.yaml",
                                         "sha256": "0" * 64}}
    scene["sources"] = [{"id": 1, "element": "tower#1", "params": ["height"]}]
    scene_path = tmp_path / "detached.scene.json.gz"
    original = gzip.compress(json.dumps(scene).encode())
    scene_path.write_bytes(original)
    detached = prepare_input(scene_path, output=tmp_path / "scene")
    assert not detached.spec
    assert all("params" not in entry for entry in detached.scene["sources"])
    detached.save_edits(edits(detached, [{"id": "del", "kind": "delete", "at": [0, 0, 0]}]))
    detached.regenerate()
    assert scene_path.read_bytes() == original
    assert not detached.scene_path.samefile(scene_path)


def test_spec_param_regeneration(tmp_path):
    source = tmp_path / "tower.yaml"
    source.write_text('formatVersion: 1\nminecraftVersion: "1.21.11"\narchetype: tower\n'
                      'scale: small\nconfig:\n  weathering: {age: 0}\n', encoding="utf-8")
    prepared = prepare_input(source, output=tmp_path / "output")
    original_scene = prepared.scene_hash
    assert prepared.spec
    assert any(entry.get("params") for entry in prepared.scene["sources"])
    prepared.save_edits(edits(prepared, [
        {"id": "height", "kind": "param", "element": "tower#1", "param": "height", "value": 14},
    ]))
    result = prepared.regenerate()
    assert result["code"] in (0, 3)
    assert prepared.scene_hash != original_scene
    saved = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert saved["elements"]["tower#1"]["height"] == 14
    assert prepared.scene["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    prepared.check_generation()
    # 次世代でも保存と再生成できる。
    prepared.save_edits(edits(prepared, [
        {"id": "height2", "kind": "param", "element": "tower#1", "param": "height", "value": 17},
    ]))
    assert prepared.regenerate()["code"] in (0, 3)
    prepared.check_generation()


def test_cli_apply_edits_saves_without_generation(tmp_path, capsys):
    source = tmp_path / "test.json"
    blueprint(source)
    prepared = prepare_input(source, output=tmp_path / "output")
    before = source.read_bytes(), prepared.scene_path.read_bytes()
    edit_path = tmp_path / "test.edits.json"
    edit_path.write_text(json.dumps(edits(prepared, [
        {"id": "delete", "kind": "delete", "region": [[0, 0, 0], [1, 0, 0]]},
    ])), encoding="utf-8")
    assert main(["apply-edits", str(edit_path), "--input", str(source),
                 "--scene", str(prepared.scene_path)]) == 0
    result = json.loads(source.with_name("test.edited.json").read_bytes())
    assert result["operations"][-1] == {"type": "fill", "from": [-2, 0, 0], "to": [-1, 0, 0],
                                         "block": "minecraft:air"}
    assert "overrides" not in result
    assert before == (source.read_bytes(), prepared.scene_path.read_bytes())
    assert "Wrote" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["apply-edits", "--help"])
    help_text = capsys.readouterr().out
    assert "原本を更新" in help_text and "再生成は行わない" in help_text


def test_cli_spec_atomic_update_and_stale_generation(tmp_path):
    source = tmp_path / "tower.yaml"
    source.write_text('minecraftVersion: "1.21.11"\narchetype: tower\nscale: small\n',
                      encoding="utf-8")
    prepared = prepare_input(source, output=tmp_path / "output")
    edit_path = tmp_path / "tower.edits.json"
    edit_path.write_text(json.dumps(edits(prepared, [
        {"id": "height", "kind": "param", "element": "tower#1", "param": "height", "value": 14},
    ])), encoding="utf-8")
    scene_before = prepared.scene_path.read_bytes()
    assert main(["apply-edits", str(edit_path), "--spec", str(source),
                 "--scene", str(prepared.scene_path)]) == 0
    assert yaml.safe_load(source.read_bytes())["elements"]["tower#1"]["height"] == 14
    assert scene_before == prepared.scene_path.read_bytes()
    assert edit_path.exists()
    assert main(["apply-edits", str(edit_path), "--spec", str(source),
                 "--scene", str(prepared.scene_path)]) == 2


def test_version_override_and_generation_conflict(tmp_path):
    source = tmp_path / "test.json"
    blueprint(source)
    with pytest.raises(BlueprintError, match="矛盾"):
        prepare_input(source, output=tmp_path / "output", minecraft_version="26.3")
    prepared = prepare_input(source, output=tmp_path / "output")
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(EditConflict):
        prepared.save_edits(edits(prepared, []))


def test_invalid_scene_arrays_are_rejected(tmp_path):
    source = tmp_path / "test.json"
    blueprint(source)
    prepared = prepare_input(source, output=tmp_path / "output")
    prepared.scene["chunks"][0]["blocks"] = "AA=="
    broken = tmp_path / "bad.scene.json.gz"
    broken.write_bytes(gzip.compress(json.dumps(prepared.scene).encode()))
    with pytest.raises(ValueError):
        read_scene(broken)


def test_delete_all_then_replace_preserves_scene_bounds(tmp_path):
    source = tmp_path / "test.json"
    blueprint(source)
    prepared = prepare_input(source, output=tmp_path / "output")
    original = prepared.scene["origin"], prepared.scene["size"]
    prepared.save_edits(edits(prepared, [
        {"id": "delete-all", "kind": "delete", "region": [[0, 0, 0], [4, 1, 1]]},
    ]))
    prepared.regenerate()
    assert prepared.scene["chunks"] == []
    prepared.save_edits(edits(prepared, [
        {"id": "restore", "kind": "replace", "region": [[0, 0, 0], [0, 0, 0]],
         "block": "minecraft:stone"},
    ]))
    prepared.regenerate()
    assert prepared.scene["chunks"]
    assert (prepared.scene["origin"], prepared.scene["size"]) == original


def test_cli_non_spec_rejects_overwrite(tmp_path):
    source = tmp_path / "test.json"
    blueprint(source)
    prepared = prepare_input(source, output=tmp_path / "output")
    edit_path = tmp_path / "edits.json"
    edit_path.write_text(json.dumps(edits(prepared, [])), encoding="utf-8")
    with pytest.raises(BlueprintError, match="上書き"):
        apply_input_edits(edit_path, source, prepared.scene_path, output=source)
    assert _scene_blueprint(prepared.scene, "copy")["minecraftVersion"] == "1.21.11"


def test_generated_blueprint_preserves_weather(tmp_path):
    source = tmp_path / "house.yaml"
    source.write_text('minecraftVersion: "1.21.11"\narchetype: house\nscale: small\n'
                      'materials: {wall_main: "minecraft:stone_bricks"}\n'
                      'config:\n  weathering: {age: 1, moss: {amount: 1}, cracks: {amount: 0}}\n',
                      encoding="utf-8")
    result = make(source, output=tmp_path / "made.schem", emit=["blueprint"])
    prepared = prepare_input(tmp_path / "made.blueprint.json", output=tmp_path / "view")
    assert all("params" not in entry for entry in prepared.scene["sources"])
    derived = _scene_blueprint(prepared.scene, "derived")
    blocks = blockdata.load_block_data("1.21.11")
    actual = {p: blocks.complete(s).to_string() for p, s in generate(_validated(derived, tmp_path))
              if s.id != "minecraft:air"}
    imported = read_schematic(result.output)
    expected = {p + imported.offset: blocks.complete(s).to_string() for p, s in imported.volume
                if s.id != "minecraft:air"}
    assert actual == expected


def test_schematic_requires_exact_or_missing_version_override(tmp_path):
    source = tmp_path / "test.json"
    model = _validated(blueprint(source), tmp_path)
    schematic = tmp_path / "test.schem"
    EXPORTERS["schem"]().export(generate(model), model, schematic)
    with pytest.raises(BlueprintError, match="矛盾"):
        prepare_input(schematic, output=tmp_path / "view", minecraft_version="26.3")
    nbt = nbtlib.load(schematic)
    del nbt["DataVersion"]
    nbt.save()
    with pytest.raises(BlueprintError, match="版不明"):
        prepare_input(schematic, output=tmp_path / "view")
    assert prepare_input(schematic, output=tmp_path / "view",
                         minecraft_version="1.21.11").scene["minecraftVersion"] == "1.21.11"
    nbt["DataVersion"] = nbtlib.Int(1)
    nbt.save()
    with pytest.raises(BlueprintError, match="未知"):
        prepare_input(schematic, output=tmp_path / "view", minecraft_version="1.21.11")


def test_spec_notes_returned_without_buildspec_metadata(tmp_path, capsys):
    source = tmp_path / "tower.yaml"
    source.write_text('minecraftVersion: "1.21.11"\narchetype: tower\nscale: small\n',
                      encoding="utf-8")
    prepared = prepare_input(source, output=tmp_path / "output")
    document = edits(prepared, [
        {"id": "note", "kind": "note", "element": "tower#1", "text": "入口を確認する"},
    ])
    notes = [{"id": "note", "element": "tower#1", "text": "入口を確認する"}]
    assert prepared.save_edits(document)["notes"] == notes
    saved_edits = prepared.edits_path.read_bytes()
    updated = tmp_path / "updated.yaml"
    assert main(["apply-edits", str(prepared.edits_path), "--spec", str(source),
                 "--scene", str(prepared.scene_path), "-o", str(updated)]) == 0
    output = capsys.readouterr().out
    assert "メモ " + json.dumps(notes[0], ensure_ascii=False) in output
    assert "metadata" not in yaml.safe_load(updated.read_bytes())
    assert prepared.regenerate()["notes"] == notes
    saved = yaml.safe_load(source.read_bytes())
    assert "metadata" not in saved
    assert prepared.edits_path.read_bytes() == saved_edits
    prepared.check_generation()
