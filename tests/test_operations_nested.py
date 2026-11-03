import random
from typing import Any

import pytest

from mcblueprint.errors import BlueprintError
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.operations import ExecutionContext, build_operation
from mcblueprint.operations.base import mirror_state
from mcblueprint.volume import BlockVolume


def run(data: dict[str, Any]) -> tuple[Any, BlockVolume]:
    op = build_operation(data, "operations[0]")
    ctx = ExecutionContext(BlockVolume(), random.Random(0), {})
    op.apply(ctx)
    return op, ctx.volume


def point(x: int, y: int, z: int, block: str = "stone") -> dict[str, Any]:
    return {"type": "set", "position": [x, y, z], "block": block}


def blueprint(*operations: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "minecraftVersion": "1.21.11",
        "name": "n",
        "operations": list(operations),
        **extra,
    }


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_translate_rotation_matches_separate_transforms(rotation):
    from mcblueprint.validator import validate

    inner = [point(1, 0, 2, "oak_stairs[facing=north]")]
    combined = blueprint({"type": "translate", "offset": [4, 2, -3],
                          "rotation": rotation, "operations": inner})
    rotated = [{"type": "rotate", "center": [0, 0, 0], "angle": rotation,
                "operations": inner}] if rotation else inner
    separate = blueprint({"type": "translate", "offset": [4, 2, -3], "operations": rotated})
    assert validate(combined) == validate(separate) == []
    a, b = load_blueprint_dict(combined), load_blueprint_dict(separate)
    assert a.operations[0].bounds() == b.operations[0].bounds()
    assert dict(generate(a)) == dict(generate(b))


@pytest.mark.parametrize("rotation", [True, 45])
def test_translate_rejects_invalid_rotation(rotation):
    from mcblueprint.validator import validate

    operation = {"type": "translate", "offset": [0, 0, 0],
                 "rotation": rotation, "operations": [point(0, 0, 0)]}
    assert validate(blueprint(operation))
    with pytest.raises(BlueprintError, match="rotation"):
        build_operation(operation, "operations[0]")


class TestMirrorState:
    @pytest.mark.parametrize(
        ("axis", "text", "expected"),
        [
            ("x", "oak_stairs[facing=east]", "minecraft:oak_stairs[facing=west]"),
            ("x", "oak_stairs[facing=west]", "minecraft:oak_stairs[facing=east]"),
            ("x", "oak_stairs[facing=north]", "minecraft:oak_stairs[facing=north]"),
            ("z", "oak_stairs[facing=north]", "minecraft:oak_stairs[facing=south]"),
            ("z", "oak_stairs[facing=east]", "minecraft:oak_stairs[facing=east]"),
            ("y", "oak_stairs[half=top]", "minecraft:oak_stairs[half=bottom]"),
            ("y", "oak_slab[type=bottom]", "minecraft:oak_slab[type=top]"),
            ("y", "oak_slab[type=double]", "minecraft:oak_slab[type=double]"),
            ("y", "piston[facing=up]", "minecraft:piston[facing=down]"),
            (
                "x",
                "oak_stairs[facing=east,shape=inner_left]",
                "minecraft:oak_stairs[facing=west,shape=inner_right]",
            ),
            (
                "z",
                "oak_door[facing=north,hinge=left]",
                "minecraft:oak_door[facing=south,hinge=right]",
            ),
            ("x", "glass_pane[east=true,west=false]", "minecraft:glass_pane[east=false,west=true]"),
            ("z", "oak_log[axis=z]", "minecraft:oak_log[axis=z]"),
            ("x", "stone", "minecraft:stone"),
        ],
    )
    def test_flips(self, axis: str, text: str, expected: str) -> None:
        assert mirror_state(BlockState.parse(text), axis).to_string() == expected


class TestMirror:
    def test_keep_original_default(self) -> None:
        op, volume = run({"type": "mirror", "axis": "x", "at": 0, "operations": [point(3, 0, 1)]})
        assert op.keep_original is True
        assert set(volume.positions()) == {Vec3(3, 0, 1), Vec3(-3, 0, 1)}
        assert op.bounds() == AABB(Vec3(-3, 0, 1), Vec3(3, 0, 1))

    def test_mirror_only(self) -> None:
        op, volume = run(
            {
                "type": "mirror",
                "axis": "z",
                "at": 2,
                "keepOriginal": False,
                "operations": [point(0, 0, 0), point(0, 0, 1)],
            }
        )
        assert set(volume.positions()) == {Vec3(0, 0, 4), Vec3(0, 0, 3)}
        assert op.bounds() == AABB(Vec3(0, 0, 3), Vec3(0, 0, 4))

    def test_half_plane(self) -> None:
        _, volume = run({"type": "mirror", "axis": "x", "at": 0.5, "operations": [point(1, 0, 0)]})
        assert set(volume.positions()) == {Vec3(1, 0, 0), Vec3(0, 0, 0)}

    def test_flips_block_state(self) -> None:
        _, volume = run(
            {
                "type": "mirror",
                "axis": "x",
                "at": 0,
                "operations": [point(2, 0, 0, "oak_stairs[facing=east,half=top]")],
            }
        )
        assert volume.get(Vec3(2, 0, 0)) == BlockState.parse("oak_stairs[facing=east,half=top]")
        assert volume.get(Vec3(-2, 0, 0)) == BlockState.parse("oak_stairs[facing=west,half=top]")

    def test_execution_order_original_then_mirror(self) -> None:
        # both copies land on the plane; the mirrored pass runs last and wins
        _, volume = run(
            {
                "type": "mirror",
                "axis": "x",
                "at": 0,
                "operations": [point(0, 0, 0, "stone"), point(0, 0, 0, "stone_bricks")],
            }
        )
        assert volume.get(Vec3(0, 0, 0)) == BlockState.parse("stone_bricks")

    def test_mirror_of_wall_bounds(self) -> None:
        wall = {"type": "wall", "from": [0, 0, 0], "to": [3, 0, 2], "height": 2, "block": "stone"}
        op, volume = run({"type": "mirror", "axis": "z", "at": -0.5, "operations": [wall]})
        assert op.bounds() == AABB(Vec3(0, 0, -3), Vec3(3, 1, 2))
        cells = set(volume.positions())
        assert cells == {Vec3(p.x, p.y, -1 - p.z) for p in cells}

    @pytest.mark.parametrize(
        "data",
        [
            {"type": "mirror", "at": 0, "operations": [point(0, 0, 0)]},
            {"type": "mirror", "axis": "w", "at": 0, "operations": [point(0, 0, 0)]},
            {"type": "mirror", "axis": "x", "at": 0.3, "operations": [point(0, 0, 0)]},
            {"type": "mirror", "axis": "x", "at": "0", "operations": [point(0, 0, 0)]},
            {"type": "mirror", "axis": "x", "at": 0, "operations": []},
            {
                "type": "mirror",
                "axis": "x",
                "at": 0,
                "keepOriginal": 1,
                "operations": [point(0, 0, 0)],
            },
        ],
    )
    def test_invalid(self, data: dict[str, Any]) -> None:
        with pytest.raises(BlueprintError):
            build_operation(data, "operations[0]")


class TestRepeat:
    def test_translates_each_copy(self) -> None:
        op, volume = run(
            {"type": "repeat", "count": 3, "offset": [2, 1, 0], "operations": [point(0, 0, 0)]}
        )
        assert set(volume.positions()) == {Vec3(0, 0, 0), Vec3(2, 1, 0), Vec3(4, 2, 0)}
        assert op.bounds() == AABB(Vec3(0, 0, 0), Vec3(4, 2, 0))

    def test_count_one(self) -> None:
        _, volume = run(
            {"type": "repeat", "count": 1, "offset": [5, 5, 5], "operations": [point(1, 1, 1)]}
        )
        assert set(volume.positions()) == {Vec3(1, 1, 1)}

    def test_negative_offset_bounds(self) -> None:
        op, _ = run(
            {"type": "repeat", "count": 4, "offset": [0, 0, -3], "operations": [point(0, 0, 0)]}
        )
        assert op.bounds() == AABB(Vec3(0, 0, -9), Vec3(0, 0, 0))

    def test_zero_offset_allowed(self) -> None:
        _, volume = run(
            {"type": "repeat", "count": 3, "offset": [0, 0, 0], "operations": [point(0, 0, 0)]}
        )
        assert len(volume) == 1

    @pytest.mark.parametrize(
        "data",
        [
            {"type": "repeat", "count": 0, "offset": [1, 0, 0], "operations": [point(0, 0, 0)]},
            {"type": "repeat", "count": 513, "offset": [1, 0, 0], "operations": [point(0, 0, 0)]},
            {"type": "repeat", "count": 2, "operations": [point(0, 0, 0)]},
            {"type": "repeat", "count": 2, "offset": [1, 0, 0], "operations": []},
        ],
    )
    def test_invalid(self, data: dict[str, Any]) -> None:
        with pytest.raises(BlueprintError):
            build_operation(data, "operations[0]")


class TestNesting:
    def test_mirror_inside_repeat_moves_plane(self) -> None:
        mirror = {"type": "mirror", "axis": "x", "at": 0, "operations": [point(1, 0, 0)]}
        op, volume = run(
            {"type": "repeat", "count": 2, "offset": [10, 0, 0], "operations": [mirror]}
        )
        assert set(volume.positions()) == {
            Vec3(1, 0, 0),
            Vec3(-1, 0, 0),
            Vec3(11, 0, 0),
            Vec3(9, 0, 0),
        }
        assert op.bounds() == AABB(Vec3(-1, 0, 0), Vec3(11, 0, 0))

    def test_repeat_inside_mirror(self) -> None:
        repeat = {"type": "repeat", "count": 2, "offset": [0, 0, 1], "operations": [point(0, 0, 1)]}
        _, volume = run({"type": "mirror", "axis": "z", "at": 0, "operations": [repeat]})
        assert set(volume.positions()) == {
            Vec3(0, 0, 1),
            Vec3(0, 0, 2),
            Vec3(0, 0, -1),
            Vec3(0, 0, -2),
        }

    def test_double_mirror_restores_facing(self) -> None:
        inner = {
            "type": "mirror",
            "axis": "x",
            "at": 5,
            "keepOriginal": False,
            "operations": [point(1, 0, 0, "oak_stairs[facing=east]")],
        }
        _, volume = run(
            {"type": "mirror", "axis": "x", "at": 0, "keepOriginal": False, "operations": [inner]}
        )
        # inner: x -> 10 - 1 = 9 (facing west); outer: x -> -9 (facing east again)
        assert volume.get(Vec3(-9, 0, 0)) == BlockState.parse("oak_stairs[facing=east]")

    @staticmethod
    def nest(levels: int) -> dict[str, Any]:
        data: dict[str, Any] = point(0, 0, 0)
        for _ in range(levels):
            data = {"type": "repeat", "count": 1, "offset": [0, 0, 0], "operations": [data]}
        return data

    def test_depth_limit(self) -> None:
        bp = load_blueprint_dict(blueprint(self.nest(8)))
        with pytest.raises(BlueprintError, match="nesting"):
            generate(bp)

    def test_depth_within_limit(self) -> None:
        bp = load_blueprint_dict(blueprint(self.nest(7)))
        assert len(generate(bp)) == 1

    def test_palette_draws_independently_per_copy(self) -> None:
        repeat = {
            "type": "repeat",
            "count": 40,
            "offset": [1, 0, 0],
            "operations": [{"type": "set", "position": [0, 0, 0], "palette": "p"}],
        }
        bp = load_blueprint_dict(
            blueprint(repeat, seed=3, palettes={"p": [{"block": "a"}, {"block": "b"}]})
        )
        volume = generate(bp)
        assert len({state.id for _, state in volume}) == 2
