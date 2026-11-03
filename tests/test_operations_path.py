import random
from pathlib import Path
from typing import Any

import pytest

from mcblueprint import components
from mcblueprint.errors import BlueprintError
from mcblueprint.model import BlockState, Vec3
from mcblueprint.operations import ExecutionContext, build_operation, build_operations
from mcblueprint.operations.path import segment_cells
from mcblueprint.validator import validate
from mcblueprint.volume import BlockVolume

REPO_ROOT = Path(__file__).resolve().parent.parent
B = BlockState.parse
AIR = B("air")
GRAVEL = B("gravel")


def run(*ops: dict[str, Any]) -> BlockVolume:
    ctx = ExecutionContext(BlockVolume(), random.Random(0), {})
    with components.component_search_paths([REPO_ROOT / "components"]):
        for op in build_operations(list(ops), "operations"):
            op.apply(ctx)
    return ctx.volume


def blueprint(*operations: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "minecraftVersion": "1.21.11",
        "name": "n",
        "operations": list(operations),
        **extra,
    }


def path(**extra: Any) -> dict[str, Any]:
    data = {"type": "path", "points": [[0, 0, 0], [8, 0, 0]], "block": "gravel", **extra}
    return {k: v for k, v in data.items() if v is not None}


class TestSegments:
    def test_even_steps(self) -> None:
        assert [c.y for c in segment_cells(Vec3(0, 0, 0), Vec3(0, 3, 6))] == [0, 1, 1, 2, 2, 3, 3]
        assert [c.y for c in segment_cells(Vec3(0, 5, 0), Vec3(4, 2, 0))] == [5, 4, 3, 3, 2]
        assert [c.y for c in segment_cells(Vec3(0, 0, 0), Vec3(3, 3, 0))] == [0, 1, 2, 3]
        cells = segment_cells(Vec3(2, 0, 9), Vec3(2, 0, 5))
        assert [c.z for c in cells] == [9, 8, 7, 6, 5]


class TestStraight:
    def test_width_clearance_and_edge(self) -> None:
        volume = run(
            {"type": "fill", "from": [0, 0, -2], "to": [10, 4, 3], "block": "stone"},
            path(width=2, edge="cobblestone"),
        )
        for x in range(9):
            assert volume.get(Vec3(x, 0, 0)) == GRAVEL and volume.get(Vec3(x, 0, 1)) == GRAVEL
            assert volume.get(Vec3(x, 1, 0)) == AIR and volume.get(Vec3(x, 2, 1)) == AIR
            assert volume.get(Vec3(x, 3, 0)) == B("stone")
            assert volume.get(Vec3(x, 0, -1)) == B("cobblestone")
            assert volume.get(Vec3(x, 0, 2)) == B("cobblestone")
        assert volume.get(Vec3(9, 0, 0)) == B("stone")

    def test_odd_width_is_centred(self) -> None:
        volume = run(path(width=3))
        assert volume.get(Vec3(4, 0, -1)) == GRAVEL and volume.get(Vec3(4, 0, 1)) == GRAVEL
        assert volume.get(Vec3(4, 0, 2)) is None

    def test_no_clearance(self) -> None:
        volume = run(path(clearance=0))
        assert volume.get(Vec3(4, 1, 0)) is None


class TestSlopesAndCorners:
    def test_stairs_face_uphill_both_ways(self) -> None:
        volume = run(path(points=[[0, 0, 0], [3, 3, 0], [6, 0, 0]], width=1, stairs="stone_stairs"))
        east = B("stone_stairs[facing=east,half=bottom]")
        west = B("stone_stairs[facing=west,half=bottom]")
        assert volume.get(Vec3(0, 0, 0)) == GRAVEL
        assert volume.get(Vec3(1, 1, 0)) == east and volume.get(Vec3(2, 2, 0)) == east
        assert volume.get(Vec3(3, 3, 0)) == B("stone_stairs[facing=east,half=bottom]")  # peak
        assert volume.get(Vec3(4, 2, 0)) == west and volume.get(Vec3(5, 1, 0)) == west
        assert volume.get(Vec3(6, 0, 0)) == GRAVEL

    def test_without_stairs_block_steps_use_the_surface(self) -> None:
        volume = run(path(points=[[0, 0, 0], [2, 2, 0]], width=1))
        assert volume.get(Vec3(1, 1, 0)) == GRAVEL and volume.get(Vec3(2, 2, 0)) == GRAVEL

    def test_corner_square_joins_the_legs(self) -> None:
        volume = run(path(points=[[0, 0, 0], [5, 0, 0], [5, 0, 5]], width=2))
        # first leg covers z 0..1, second leg (travelling +z) covers x 5..6
        for x, z in ((5, 0), (6, 0), (5, 1), (6, 1)):
            assert volume.get(Vec3(x, 0, z)) == GRAVEL
        assert volume.get(Vec3(6, 0, 4)) == GRAVEL and volume.get(Vec3(4, 0, 2)) is None

    def test_corner_square_follows_leg_directions(self) -> None:
        # second leg travels -x, so its width extends towards -z: no stray cells at z 6..7
        volume = run(path(points=[[8, 0, 0], [8, 0, 6], [2, 0, 6]], width=2))
        assert volume.get(Vec3(9, 0, 5)) == GRAVEL and volume.get(Vec3(9, 0, 6)) == GRAVEL
        assert volume.get(Vec3(9, 0, 7)) is None and volume.get(Vec3(8, 0, 7)) is None


class TestLights:
    def test_component_lights_beside_the_edge(self) -> None:
        volume = run(
            path(edge="cobblestone", lights={"spacing": 4, "component": "medieval.lantern_post"})
        )
        # right of travel (+z): edge at z 2, post base at z 3 one above the surface
        from mcblueprint.blockdata import load_block_data

        for x in (0, 4, 8):
            base = load_block_data("1.21.11").get(volume.get(Vec3(x, 1, 3)).id)
            assert base.shape == "full" and base.material in ("stone", "brick")
            assert volume.get(Vec3(x, 4, 3)) == B("lantern")
        assert volume.get(Vec3(2, 1, 3)) is None

    def test_block_lights_without_edge(self) -> None:
        volume = run(path(lights={"spacing": 8, "block": "torch"}))
        assert volume.get(Vec3(0, 1, 2)) == B("torch") and volume.get(Vec3(8, 1, 2)) == B("torch")

    @pytest.mark.parametrize(
        ("lights", "match"),
        [
            ({"spacing": 3}, "exactly one"),
            ({"spacing": 3, "component": "a", "block": "torch"}, "exactly one"),
            ({"spacing": 3, "component": "Bad Name"}, "component name"),
            ({"component": "medieval.lantern_post"}, "spacing"),
        ],
    )
    def test_invalid_lights(self, lights: dict[str, Any], match: str) -> None:
        with pytest.raises(BlueprintError, match=match):
            build_operation(path(lights=lights), "operations[0]")


class TestErrors:
    @pytest.mark.parametrize(
        ("points", "match"),
        [
            ([[0, 0, 0]], "at least 2"),
            ([[0, 0, 0], [3, 0, 3]], "along x or z"),
            ([[0, 0, 0], [0, 2, 0]], "same x and z"),
            ([[0, 0, 0], [2, 3, 0]], "at most one block per block"),
            ([[0, 0, 0], [1, 0]], "\\[x, y, z\\]"),
        ],
    )
    def test_invalid_points(self, points: list, match: str) -> None:
        with pytest.raises(BlueprintError, match=match):
            build_operation(path(points=points), "operations[0]")


class TestValidation:
    def test_valid_and_missing_component(self) -> None:
        with components.component_search_paths([REPO_ROOT / "components"]):
            assert (
                validate(
                    blueprint(
                        path(
                            palette="p",
                            block=None,
                            edge="cobblestone",
                            stairs="cobblestone_stairs",
                            lights={"spacing": 5, "component": "medieval.lantern_post"},
                        ),
                        palettes={"p": [{"block": "minecraft:gravel", "weight": 1}]},
                    )
                )
                == []
            )
            errors = validate(blueprint(path(lights={"spacing": 5, "component": "ghost"})))
            assert errors and "ghost" in errors[0].message

    def test_bad_blocks_reported(self) -> None:
        errors = validate(
            blueprint(
                path(edge="bad_edge", stairs="bad_stairs", lights={"spacing": 2, "block": "bad"})
            )
        )
        assert {e.path for e in errors} == {
            "operations[0].edge",
            "operations[0].stairs",
            "operations[0].lights.block",
        }
