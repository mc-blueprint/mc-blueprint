import json
import shutil
from copy import deepcopy
from pathlib import Path

import nbtlib
import pytest

from mcblueprint import blockdata, components
from mcblueprint.cli import EXIT_OK, main
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint, load_blueprint_dict, read_blueprint_json
from mcblueprint.validator import validate

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.json"))
LEGACY_COMPONENTS = Path(__file__).parent / "data" / "patterns-baseline"
LEGACY_NAMES = {
    "medieval.arched_gate": "arched_gate",
    "medieval.lantern_post": "lantern_post",
    "medieval.window": "medieval_window",
}


def test_examples_exist() -> None:
    assert {p.name for p in EXAMPLES} >= {"house.json", "tower.json"}


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_valid(path: Path) -> None:
    assert validate(read_blueprint_json(path)) == []


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_generates_within_declared_size(path: Path) -> None:
    blueprint = load_blueprint(path)
    volume = generate(blueprint)
    bounds = volume.bounds()
    assert bounds is not None and len(volume) > 0
    assert blueprint.size is not None
    size = bounds.size
    assert (size.x, size.y, size.z) <= (blueprint.size.x, blueprint.size.y, blueprint.size.z)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_pattern_migration_preserves_example_geometry(path: Path, tmp_path: Path) -> None:
    raw = read_blueprint_json(path)

    def use_legacy_names(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"name", "component"} and isinstance(child, str):
                    value[key] = LEGACY_NAMES.get(child, child)
                else:
                    use_legacy_names(child)
        elif isinstance(value, list):
            for child in value:
                use_legacy_names(child)

    old_blueprint = deepcopy(raw)
    use_legacy_names(old_blueprint)
    old_components = tmp_path / "components"
    shutil.copytree(LEGACY_COMPONENTS, old_components)
    with components.component_search_paths([old_components]):
        before = generate(load_blueprint_dict(old_blueprint))
    after = generate(load_blueprint(path))
    if path.stem not in ("gatehouse", "village_path"):
        assert dict(before) == dict(after)
        return
    # patterns#11の役割参照化で材質は変わるが、位置・形状・向きは維持する。
    registry = blockdata.load_block_data(raw["minecraftVersion"])
    assert set(dict(before)) == set(dict(after))
    for pos, state in before:
        actual = after.get(pos)
        assert registry.get(state.id).shape == registry.get(actual.id).shape
        for key in ("facing", "half", "type"):
            assert state.get(key) == actual.get(key)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_builds(path: Path, tmp_path: Path) -> None:
    out = tmp_path / (path.stem + ".schem")
    assert main(["build", str(path), "-o", str(out)]) == EXIT_OK
    nbt = nbtlib.load(out)
    assert nbt["Version"] == 2
    assert nbt["PaletteMax"] == len(nbt["Palette"])


@pytest.mark.parametrize("version", blockdata.supported_versions())
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_is_valid_for_every_bundled_version(path: Path, version: str) -> None:
    data = read_blueprint_json(path)
    data["minecraftVersion"] = version
    assert validate(data) == []


def test_minecraft_version_override_changes_data_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = EXAMPLES_DIR / "house.json"
    out = tmp_path / "house.schem"
    assert main(["build", str(path), "-o", str(out), "--minecraft-version", "26.3"]) == EXIT_OK
    assert int(nbtlib.load(out)["DataVersion"]) == blockdata.data_version("26.3")
    assert main(["validate", str(path), "--minecraft-version", "0.0.1"]) == 1
    assert "Use --minecraft-version" in capsys.readouterr().out


def test_versions_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["versions"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "1.21.11" in out and "26.3" in out and "DataVersion" in out
    assert main(["versions", "--json"]) == EXIT_OK
    rows = json.loads(capsys.readouterr().out)["versions"]
    assert [row["version"] for row in rows] == blockdata.supported_versions()
    assert all(row["blocks"] > 1000 for row in rows)
