import json
from pathlib import Path
from typing import Any

import pytest

from mcblueprint import components
from mcblueprint.cli import EXIT_OK, EXIT_VALIDATION_ERROR, main
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint, load_blueprint_dict
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.validator import validate

B = BlockState.parse
REPO_ROOT = Path(__file__).resolve().parent.parent


def blueprint(*operations: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "minecraftVersion": "1.21.11",
        "name": "n",
        "operations": list(operations),
        **extra,
    }


def write_component(directory: Path, name: str, data: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


POST = {
    "formatVersion": 1,
    "name": "post",
    "operations": [
        {"type": "set", "position": [0, 0, 0], "block": "stone"},
        {"type": "set", "position": [0, 1, 0], "block": "oak_stairs[facing=east]"},
        {"type": "set", "position": [1, 0, 0], "palette": "accent"},
    ],
    "palettes": {"accent": [{"block": "gold_block"}]},
}


@pytest.fixture
def comp_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "components"
    write_component(directory, "post", POST)
    return directory


def with_paths(paths: list[Path], data: dict[str, Any]):
    with components.component_search_paths(paths):
        return load_blueprint_dict(data)


class TestPlacement:
    def test_position_rotation_and_local_palette(self, comp_dir: Path) -> None:
        bp = with_paths(
            [comp_dir],
            blueprint(
                {"type": "component", "name": "post", "position": [10, 5, 10]},
                {"type": "component", "name": "post", "position": [20, 0, 0], "rotation": 90},
            ),
        )
        volume = generate(bp)
        assert volume.get(Vec3(10, 5, 10)) == B("stone")
        assert volume.get(Vec3(10, 6, 10)) == B("oak_stairs[facing=east]")
        assert volume.get(Vec3(11, 5, 10)) == B("gold_block")
        # rotated 90 degrees clockwise about the component origin: +x -> +z, east -> south
        assert volume.get(Vec3(20, 0, 1)) == B("gold_block")
        assert volume.get(Vec3(20, 1, 0)) == B("oak_stairs[facing=south]")
        assert bp.operations[0].bounds() == AABB(Vec3(10, 5, 10), Vec3(11, 6, 10))
        assert bp.operations[1].bounds() == AABB(Vec3(20, 0, 0), Vec3(20, 1, 1))

    def test_component_palette_shadows_blueprint_palette(self, comp_dir: Path) -> None:
        bp = with_paths(
            [comp_dir],
            blueprint(
                {"type": "component", "name": "post", "position": [0, 0, 0]},
                {"type": "set", "position": [5, 0, 0], "palette": "accent"},
                palettes={"accent": [{"block": "diamond_block"}]},
            ),
        )
        volume = generate(bp)
        assert volume.get(Vec3(1, 0, 0)) == B("gold_block")
        assert volume.get(Vec3(5, 0, 0)) == B("diamond_block")

    def test_component_can_use_blueprint_palette(self, tmp_path: Path) -> None:
        comp_dir = tmp_path / "components"
        write_component(
            comp_dir,
            "shell",
            {
                "formatVersion": 1,
                "name": "shell",
                "operations": [{"type": "set", "position": [0, 0, 0], "palette": "wall"}],
            },
        )
        bp = with_paths(
            [comp_dir],
            blueprint(
                {"type": "component", "name": "shell", "position": [0, 0, 0]},
                palettes={"wall": [{"block": "bricks"}]},
            ),
        )
        assert generate(bp).get(Vec3(0, 0, 0)) == B("bricks")

    def test_nested_components(self, tmp_path: Path) -> None:
        comp_dir = tmp_path / "components"
        write_component(comp_dir, "post", POST)
        write_component(
            comp_dir,
            "row",
            {
                "formatVersion": 1,
                "name": "row",
                "operations": [
                    {
                        "type": "repeat",
                        "count": 3,
                        "offset": [3, 0, 0],
                        "operations": [
                            {"type": "component", "name": "post", "position": [0, 0, 0]}
                        ],
                    }
                ],
            },
        )
        bp = with_paths(
            [comp_dir], blueprint({"type": "component", "name": "row", "position": [0, 0, 5]})
        )
        volume = generate(bp)
        assert all(volume.get(Vec3(x, 0, 5)) == B("stone") for x in (0, 3, 6))


class TestResolution:
    def test_blueprint_directory_then_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bp_dir = tmp_path / "bp"
        write_component(
            bp_dir / "components", "post", {**POST, "operations": [POST["operations"][0]]}
        )
        cwd = tmp_path / "cwd"
        write_component(cwd / "components", "post", POST)
        write_component(cwd / "components", "only_cwd", POST)
        monkeypatch.chdir(cwd)
        bp_path = bp_dir / "b.json"
        bp_path.write_text(
            json.dumps(
                blueprint(
                    {"type": "component", "name": "post", "position": [0, 0, 0]},
                    {"type": "component", "name": "only_cwd", "position": [0, 0, 5]},
                )
            ),
            encoding="utf-8",
        )
        bp = load_blueprint(bp_path)
        assert len(bp.operations[0].operations) == 1  # blueprint-local version wins
        assert len(bp.operations[1].operations) == 3  # falls back to cwd

    def test_missing_component(self, comp_dir: Path) -> None:
        with pytest.raises(BlueprintError, match="Component 'nope' not found"):
            with_paths(
                [comp_dir], blueprint({"type": "component", "name": "nope", "position": [0, 0, 0]})
            )

    def test_invalid_name(self, comp_dir: Path) -> None:
        with pytest.raises(BlueprintError, match="Invalid component name"):
            components.resolve("../etc")

    def test_cycle_detected(self, tmp_path: Path) -> None:
        comp_dir = tmp_path / "components"
        write_component(
            comp_dir,
            "a",
            {
                "formatVersion": 1,
                "name": "a",
                "operations": [{"type": "component", "name": "b", "position": [0, 0, 0]}],
            },
        )
        write_component(
            comp_dir,
            "b",
            {
                "formatVersion": 1,
                "name": "b",
                "operations": [{"type": "component", "name": "a", "position": [0, 0, 0]}],
            },
        )
        with pytest.raises(BlueprintError, match="includes itself: a -> b -> a"):
            with_paths(
                [comp_dir], blueprint({"type": "component", "name": "a", "position": [0, 0, 0]})
            )

    def test_bad_rotation(self, comp_dir: Path) -> None:
        with pytest.raises(BlueprintError, match="rotation"):
            with_paths(
                [comp_dir],
                blueprint(
                    {"type": "component", "name": "post", "position": [0, 0, 0], "rotation": 45}
                ),
            )


class TestValidation:
    def test_valid(self, comp_dir: Path) -> None:
        with components.component_search_paths([comp_dir]):
            assert (
                validate(blueprint({"type": "component", "name": "post", "position": [0, 0, 0]}))
                == []
            )

    def test_missing_reported_as_error(self, comp_dir: Path) -> None:
        with components.component_search_paths([comp_dir]):
            errors = validate(
                blueprint({"type": "component", "name": "nope", "position": [0, 0, 0]})
            )
        assert [e.path for e in errors] == ["operations[0]"]
        assert errors[0].op_type == "component"
        assert "not found" in errors[0].message

    def test_component_contents_are_validated(self, tmp_path: Path) -> None:
        comp_dir = tmp_path / "components"
        write_component(
            comp_dir,
            "bad",
            {
                "formatVersion": 1,
                "name": "bad",
                "palettes": {"p": [{"block": "nope_block"}]},
                "operations": [
                    {"type": "set", "position": [0, 0, 0], "block": "stone_brick"},
                    {"type": "set", "position": [0, 1, 0], "palette": "missing"},
                ],
            },
        )
        with components.component_search_paths([comp_dir]):
            errors = validate(
                blueprint({"type": "component", "name": "bad", "position": [0, 0, 0]})
            )
        assert [e.path for e in errors] == [
            "operations[0]<bad>.palettes.p[0].block",
            "operations[0]<bad>.operations[0].block",
            "operations[0]<bad>.operations[1].palette",
        ]

    def test_component_schema_rejects_blueprint_only_keys(self, tmp_path: Path) -> None:
        comp_dir = tmp_path / "components"
        write_component(
            comp_dir, "c", {**POST, "name": "c", "minecraftVersion": "1.21.11", "origin": [0, 0, 0]}
        )
        with components.component_search_paths([comp_dir]):
            errors = validate(blueprint({"type": "component", "name": "c", "position": [0, 0, 0]}))
        assert len(errors) == 1
        assert errors[0].path == "operations[0]<c>"
        assert "minecraftVersion" in errors[0].message and "origin" in errors[0].message

    def test_cli_resolves_relative_to_blueprint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_component(tmp_path / "components", "post", POST)
        bp_path = tmp_path / "b.json"
        bp_path.write_text(
            json.dumps(blueprint({"type": "component", "name": "post", "position": [0, 0, 0]})),
            encoding="utf-8",
        )
        assert main(["validate", str(bp_path)]) == EXIT_OK
        assert main(["build", str(bp_path), "-o", str(tmp_path / "out.schem")]) == EXIT_OK
        assert (tmp_path / "out.schem").exists()
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(blueprint({"type": "component", "name": "ghost", "position": [0, 0, 0]})),
            encoding="utf-8",
        )
        assert main(["validate", str(bad)]) == EXIT_VALIDATION_ERROR
        assert "Component 'ghost' not found" in capsys.readouterr().out


class TestBundled:
    @pytest.mark.parametrize(
        "name", ["medieval.lantern_post", "medieval.window", "medieval.arched_gate"]
    )
    def test_bundled_component_is_valid(self, name: str) -> None:
        assert validate(blueprint({"type": "component", "name": name, "position": [0, 0, 0]})) == []

    def test_cli_lists_bundled_modules(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["components", "--json"]) == EXIT_OK
        listed = json.loads(capsys.readouterr().out)["components"]
        assert {item["name"] for item in listed} >= {
            "medieval.lantern_post",
            "medieval.window",
            "medieval.arched_gate",
        }
