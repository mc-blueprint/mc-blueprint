import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from mcblueprint import components
from mcblueprint.cli import EXIT_OK, EXIT_VALIDATION_ERROR, main
from mcblueprint.pattern_checking import check_patterns

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_DIR = ROOT / "src" / "mcblueprint" / "data" / "patterns"


def test_patterns_lock_matches_bundled_files() -> None:
    lock = json.loads(
        (ROOT / "src" / "mcblueprint" / "data" / "lock.json").read_text(encoding="utf-8")
    )
    entry = lock["patterns"]
    assert entry["repository"] == "https://github.com/mc-blueprint/mc-blueprint-patterns"
    assert entry["commit"] == "1783d4c74911f8681bb75c0e69b00e7c67b65e50"

    digest = hashlib.sha256()
    for path in sorted(path for path in PATTERNS_DIR.rglob("*") if path.is_file()):
        digest.update(path.relative_to(PATTERNS_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    assert entry["sha256"] == digest.hexdigest()


def test_pattern_module_resolution_uses_bundled_data() -> None:
    assert components.resolve("medieval.window") == (
        components.PATTERNS_DIR / "medieval" / "window.json"
    )


def test_external_pattern_modules_resolve_nested_modules_locally(tmp_path: Path) -> None:
    root = tmp_path / "patterns"
    modules = root / "modules" / "custom"
    modules.mkdir(parents=True)
    leaf = {
        "formatVersion": 1,
        "name": "custom.leaf",
        "operations": [{"type": "set", "position": [0, 0, 0], "block": "stone"}],
    }
    frame = {
        "formatVersion": 1,
        "name": "custom.frame",
        "operations": [{"type": "component", "name": "custom.leaf", "position": [0, 0, 0]}],
    }
    (modules / "leaf.json").write_text(json.dumps(leaf), encoding="utf-8")
    (modules / "frame.json").write_text(json.dumps(frame), encoding="utf-8")
    (root / "styles").mkdir()
    (root / "sources.yaml").write_text("formatVersion: 1\nsources: []\n", encoding="utf-8")
    results = check_patterns(root)
    assert all(result.ok for result in results)


def test_external_patterns_do_not_resolve_missing_modules_from_bundled_data(
    tmp_path: Path,
) -> None:
    root = tmp_path / "patterns"
    module_dir = root / "modules" / "custom"
    module_dir.mkdir(parents=True)
    (root / "styles").mkdir()
    (root / "sources.yaml").write_text("formatVersion: 1\nsources: []\n", encoding="utf-8")
    module = {
        "formatVersion": 1,
        "name": "custom.depends",
        "operations": [
            {
                "type": "component",
                "name": "medieval.window",
                "position": [0, 0, 0],
            }
        ],
    }
    (module_dir / "depends.json").write_text(json.dumps(module), encoding="utf-8")
    result = next(row for row in check_patterns(root) if row.kind == "module")
    assert not result.ok
    assert "not found" in result.errors[0].message


def test_pattern_module_invalid_schema_skips_semantic_inspection(tmp_path: Path) -> None:
    root = tmp_path / "patterns"
    module_dir = root / "modules" / "custom"
    module_dir.mkdir(parents=True)
    (root / "styles").mkdir()
    (root / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    for index, modules in enumerate((None, [], [{"role": "bad"}])):
        data = {
            "formatVersion": 1,
            "name": f"custom.bad{index}",
            "operations": [],
            "modules": modules,
        }
        (module_dir / f"bad{index}.json").write_text(json.dumps(data), encoding="utf-8")
    results = check_patterns(root)
    assert len(results) == 4
    assert all(result.errors for result in results[:3])


def test_parameter_and_anchor_module_passes_component_schema() -> None:
    from mcblueprint.validator import validate_schema

    data = {
        "formatVersion": 1,
        "name": "custom.parametric",
        "operations": [{"type": "set", "position": [0, 0, 0], "block": "stone"}],
        "parameters": {},
        "anchors": {},
    }
    assert validate_schema(data, component=True) == []


def test_module_name_with_hyphen_is_rejected_by_schema() -> None:
    from mcblueprint.pattern_checking import _module_registry, _schema, _validate

    data = {
        "formatVersion": 1,
        "name": "medieval.window-wide",
        "operations": [{"type": "set", "position": [0, 0, 0], "block": "stone"}],
    }
    schema = _schema("pattern-module.schema.json")
    assert _validate(data, schema, _module_registry(schema))


def test_role_key_in_metadata_is_not_a_role_reference(tmp_path: Path) -> None:
    root = tmp_path / "patterns"
    module_dir = root / "modules" / "custom"
    module_dir.mkdir(parents=True)
    (root / "styles").mkdir()
    (root / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    data = {
        "formatVersion": 1,
        "name": "custom.meta",
        "operations": [{"type": "set", "position": [0, 0, 0], "block": "stone"}],
        "metadata": {"role": "decoration"},
    }
    (module_dir / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    result = next(row for row in check_patterns(root) if row.kind == "module")
    assert result.ok


def test_patterns_schema_accepts_role_reference_in_registry() -> None:
    pattern_schema = json.loads(
        (ROOT / "schema" / "pattern-module.schema.json").read_text(encoding="utf-8")
    )
    blueprint_schema = json.loads(
        (ROOT / "schema" / "blueprint.schema.json").read_text(encoding="utf-8")
    )
    registry = (
        Registry()
        .with_resource(blueprint_schema["$id"], Resource.from_contents(blueprint_schema))
        .with_resource(pattern_schema["$id"], Resource.from_contents(pattern_schema))
    )
    data = {
        "formatVersion": 1,
        "name": "custom.frame",
        "operations": [
            {
                "type": "set",
                "position": [0, 0, 0],
                "block": {"role": "wall", "shape": "full", "state": {}},
            }
        ],
    }
    validator = Draft202012Validator(pattern_schema, registry=registry)
    assert not list(validator.iter_errors(data))

    for invalid_block in ("minecraft:bad block", {"material": "stone"}):
        invalid = {
            **data,
            "operations": [{**data["operations"][0], "block": invalid_block}],
        }
        assert list(validator.iter_errors(invalid))


def test_designs_styles_directory_is_not_assumed_to_be_patterns(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "designs" / "styles").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) == EXIT_OK
    assert "patterns" not in capsys.readouterr().out


def test_invalid_styles_module_shape_reports_validation_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    (tmp_path / "patterns" / "styles").mkdir(parents=True)
    (tmp_path / "patterns" / "modules").mkdir()
    (tmp_path / "patterns" / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (tmp_path / "patterns" / "styles" / "bad.yaml").write_text(
        "name: bad\nmodules: []\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert main(["check", "patterns"]) == EXIT_VALIDATION_ERROR
    assert "modules" in capsys.readouterr().out


@pytest.mark.parametrize("references", ["null", "[{}]", "[medieval.window-wide]"])
def test_invalid_style_references_report_schema_errors(
    tmp_path: Path, capsys, references: str
) -> None:
    (tmp_path / "styles").mkdir()
    (tmp_path / "modules").mkdir()
    (tmp_path / "sources.yaml").write_text("formatVersion: 1\nsources: []\n", encoding="utf-8")
    (tmp_path / "styles" / "bad.yaml").write_text(
        "formatVersion: 1\nname: bad\ngrid: {}\n"
        f"modules: {{window: {references}}}\n",
        encoding="utf-8",
    )
    assert main(["check", str(tmp_path)]) == EXIT_VALIDATION_ERROR
    assert "modules.window" in capsys.readouterr().out


@pytest.mark.parametrize("missing", ["sources.yaml", "modules", "both"])
def test_incomplete_patterns_are_checked(
    tmp_path: Path, capsys, missing: str
) -> None:
    root = tmp_path / "patterns"
    (root / "styles").mkdir(parents=True)
    (root / "styles" / "bad.yaml").write_text("modules: []\n", encoding="utf-8")
    if missing == "sources.yaml":
        (root / "modules").mkdir()
    if missing == "modules":
        (root / "sources.yaml").write_text("formatVersion: 1\nsources: []\n", encoding="utf-8")
    assert main(["check", str(root)]) == EXIT_VALIDATION_ERROR
    output = capsys.readouterr().out
    assert "bad.yaml (style)" in output
    assert "required" in output
    if missing in ("sources.yaml", "both"):
        assert "sources.yaml (sources)" in output
    if missing in ("modules", "both"):
        assert "modules (modules)" in output


def test_multiple_check_targets_include_incomplete_patterns(tmp_path: Path, capsys) -> None:
    (tmp_path / "patterns" / "styles").mkdir(parents=True)
    (tmp_path / "designs" / "styles").mkdir(parents=True)
    assert main(["check", str(tmp_path / "designs"), str(tmp_path / "patterns")]) == 1
    assert "sources.yaml (sources)" in capsys.readouterr().out


def test_module_search_context_is_restored(tmp_path: Path) -> None:
    with components.component_search_paths([], module_paths=(tmp_path,)):
        with components.component_search_paths([]):
            assert components.resolve("medieval.window").is_file()
        from mcblueprint.errors import BlueprintError

        with pytest.raises(BlueprintError, match="not found"):
            components.resolve("medieval.window")
    assert components.resolve("medieval.window").is_file()
