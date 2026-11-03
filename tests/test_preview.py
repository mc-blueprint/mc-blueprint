import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mcblueprint.blockdata import BlockData, BlockInfo, load_block_data, supported_versions
from mcblueprint.cli import EXIT_OK, EXIT_USAGE_ERROR, main
from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.preview import (
    BACKGROUND,
    ORIGIN,
    VIEWS,
    Options,
    _face_linear,
    _isometric_faces,
    _oklab_to_linear,
    _shape_cells,
    _srgb,
    block_color,
    layer_name,
    render,
    render_layer,
    shade,
    uncoloured_blocks,
    write_previews,
)
from mcblueprint.volume import BlockVolume

B = BlockState.parse


def volume_with(*cells: tuple[tuple[int, int, int], str]) -> BlockVolume:
    volume = BlockVolume()
    for (x, y, z), block in cells:
        volume.set(Vec3(x, y, z), B(block))
    return volume


class TestColors:
    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            ("air", None), ("cave_air", None), ("void_air", None),
            ("stone", (127, 127, 127)), ("gold_block", (255, 216, 62)),
            ("grass_block", (80, 106, 47)), ("oak_leaves", (68, 101, 24)),
            ("spruce_leaves", (49, 80, 49)), ("water", (63, 118, 228)),
        ],
    )
    def test_block_color(self, block: str, expected: Any) -> None:
        assert block_color(B(block)) == expected

    def test_unknown_block_is_an_error(self) -> None:
        with pytest.raises(BlueprintError, match="Unknown block id"):
            block_color(B("some_unknown_block"))

    @pytest.mark.parametrize("version", supported_versions())
    def test_all_bundled_blocks_have_colours(self, version: str) -> None:
        data = load_block_data(version)
        for info in data.blocks():
            for face in ("top", "side", "bottom"):
                color = block_color(B(info.id), version, face)
                if info.id in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air"):
                    assert color is None
                else:
                    assert color is not None
                    assert all(0 <= channel <= 255 for channel in color)

    @pytest.mark.parametrize(
        ("lab", "rgb"),
        [
            ((0, 0, 0), (0, 0, 0)), ((1, 0, 0), (255, 255, 255)),
            ((0.62795536, 0.22486306, 0.1258463), (255, 0, 0)),
            ((0.86643961, -0.23388757, 0.17949848), (0, 255, 0)),
            ((0.45201372, -0.03245698, -0.31152815), (0, 0, 255)),
        ],
    )
    def test_oklab_reference_colours(self, lab: tuple[float, ...], rgb: Any) -> None:
        assert _srgb(_oklab_to_linear(lab)) == rgb

    def test_tint_is_linear_and_only_applied_to_tinted_layers(self) -> None:
        def quantiles(lightness: float) -> dict[str, list[float]]:
            return {"L": [lightness] * 5, "a": [0] * 5, "b": [0] * 5}

        info = BlockInfo("minecraft:test", {}, {}, tint="grass")
        data = BlockData("test", {}, {"grass": {"plains": [0.5, 0, 0]}})
        face = {"oklab": quantiles(1), "tintedOklab": quantiles(0.5)}
        assert _face_linear(info, face, data) == pytest.approx((0.015625,) * 3)
        face["untintedOklab"] = quantiles(1)
        assert _face_linear(info, face, data) == pytest.approx((0.5078125,) * 3)
        info = BlockInfo("minecraft:test", {}, {}, tint="none")
        assert _face_linear(info, face, data) == pytest.approx((1,) * 3)

    def test_views_use_top_and_side_faces(self) -> None:
        volume = volume_with(((0, 0, 0), "grass_block"))
        top = block_color(B("grass_block"))
        side = block_color(B("grass_block"), face="side")
        assert top is not None and side is not None and top != side
        assert render(volume, "top", scale=4).getpixel((1, 1)) == shade(top, 0.6)
        assert render_layer(volume, 0, scale=4).getpixel((1, 1)) == top
        for view in ("north", "south", "east", "west"):
            assert render(volume, view, scale=4).getpixel((1, 2)) == shade(side, 0.9)
        assert {factor: color for _, color, factor in _isometric_faces(volume)} == {
            1.05: top, 0.75: side, 0.6: side,
        }

    def test_shade(self) -> None:
        assert shade((100, 200, 250), 0.5) == (50, 100, 125)
        assert shade((100, 200, 250), 2.0) == (200, 255, 255)


class TestRender:
    def test_top_view_size_and_highest_block(self) -> None:
        volume = volume_with(((0, 0, 0), "stone"), ((2, 3, 1), "gold_block"))
        image = render(volume, "top", scale=4)
        assert image.size == (3 * 4, 2 * 4)
        assert image.getpixel((2 * 4 + 1, 1 * 4 + 1)) != BACKGROUND
        assert image.getpixel((1 * 4 + 1, 0 * 4 + 1)) == BACKGROUND  # empty column

    def test_elevations_show_nearest_block(self) -> None:
        volume = volume_with(((0, 0, 0), "gold_block"), ((0, 0, 5), "stone"))
        north = render(volume, "north", scale=4)
        south = render(volume, "south", scale=4)
        assert north.size == (4, 4) and south.size == (4, 4)
        assert north.getpixel((1, 2)) == shade((255, 216, 62), 0.9)  # row 1 is the top highlight
        assert south.getpixel((1, 2)) == shade((127, 127, 127), 0.9)
        east = render(volume, "east", scale=4)
        assert east.size == (6 * 4, 4)

    def test_isometric_size_and_hidden_cubes(self) -> None:
        volume = volume_with(
            *(((x, y, z), "stone") for x in range(3) for y in range(3) for z in range(3))
        )
        image = render(volume, "isometric", scale=8)
        assert image.size == ((3 + 3) * 8, (3 + 3) * 4 + 3 * 8 + 8)
        # something was drawn
        assert any(
            image.getpixel((x, y)) != BACKGROUND
            for x in range(0, image.width, 4)
            for y in range(0, image.height, 4)
        )

    def test_unknown_view_and_empty_volume(self) -> None:
        with pytest.raises(BlueprintError, match="Unknown view"):
            render(volume_with(((0, 0, 0), "stone")), "bottom")
        with pytest.raises(BlueprintError, match="no blocks"):
            render(BlockVolume(), "top")

    def test_write_previews(self, tmp_path: Path) -> None:
        volume = volume_with(((0, 0, 0), "stone"))
        paths = write_previews(volume, tmp_path / "out", "x", VIEWS, scale=4)
        assert [p.name for p in paths] == [f"x-{v}.png" for v in VIEWS]
        assert all(Image.open(p).size[0] > 0 for p in paths)


class TestCli:
    DATA = {
        "formatVersion": 1,
        "minecraftVersion": "1.21.11",
        "name": "p",
        "operations": [{"type": "fill", "from": [0, 0, 0], "to": [3, 2, 3], "block": "stone"}],
    }

    def test_version_override_reaches_colours(self, tmp_path: Path) -> None:
        old = load_block_data("1.21.11")
        info = next(info for info in load_block_data("26.3").blocks() if info.id not in old)
        data = {
            **self.DATA,
            "operations": [{"type": "set", "position": [0, 0, 0], "block": info.id}],
        }
        path = tmp_path / "new.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        assert main([
            "preview", str(path), "-o", str(tmp_path), "--minecraft-version", "26.3",
            "--views", ",".join(VIEWS), "--layer", "0", "--scale", "8",
        ]) == EXIT_OK
        color = block_color(B(info.id), "26.3")
        assert Image.open(tmp_path / "new-y0.png").getpixel((2, 4)) == color

    def test_missing_faces_warn_and_still_render(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "fallback.json"
        path.write_text(json.dumps({
            **self.DATA,
            "operations": [{"type": "set", "position": [0, 0, 0], "block": "water"}],
        }), encoding="utf-8")
        assert main([
            "preview", str(path), "-o", str(tmp_path), "--layer", "0", "--scale", "8",
        ]) == EXIT_OK
        output = capsys.readouterr().out
        assert "WARNING: no blockdata face colour for minecraft:water" in output
        assert "using plains water tint or same-material median" in output
        assert Image.open(tmp_path / "fallback-y0.png").getpixel((2, 4)) == (63, 118, 228)

    def test_default_views(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = tmp_path / "p.json"
        path.write_text(json.dumps(self.DATA), encoding="utf-8")
        assert main(["preview", str(path), "-o", str(tmp_path / "pv")]) == EXIT_OK
        out = capsys.readouterr().out.splitlines()
        assert [Path(line.split(" ", 1)[1]).name for line in out] == [
            "p-top.png",
            "p-north.png",
            "p-east.png",
            "p-isometric.png",
        ]
        assert Image.open(tmp_path / "pv" / "p-top.png").size == (32, 32)

    def test_views_and_scale(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(json.dumps(self.DATA), encoding="utf-8")
        assert (
            main(["preview", str(path), "-o", str(tmp_path), "--views", "south", "--scale", "2"])
            == EXIT_OK
        )
        assert Image.open(tmp_path / "p-south.png").size == (8, 6)
        assert (
            main(["preview", str(path), "-o", str(tmp_path), "--views", "bottom"])
            == EXIT_USAGE_ERROR
        )
        assert main(["preview", str(path), "-o", str(tmp_path), "--scale", "0"]) == EXIT_USAGE_ERROR

    def test_missing_pillow_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "PIL":
                raise ImportError("no PIL")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(BlueprintError, match="Pillow"):
            render(volume_with(((0, 0, 0), "stone")), "top")


class TestLayersAndShading:
    def test_layer_shows_level_and_ghosts_the_one_below(self) -> None:
        volume = volume_with(((0, 0, 0), "gold_block"), ((2, 0, 1), "stone"), ((2, 1, 1), "stone"))
        image = render_layer(volume, 1, scale=4)
        assert image.size == (3 * 4, 2 * 4)
        assert image.getpixel((2 * 4 + 1, 1 * 4 + 1)) == (127, 127, 127)  # stone at y=1
        ghost = image.getpixel((1, 1))  # gold block one level below, faded
        assert ghost != BACKGROUND and ghost != (255, 216, 62)
        assert ghost[0] > ghost[2]  # still yellowish
        assert image.getpixel((1 * 4 + 1, 1)) == BACKGROUND

    def test_layer_outside_the_structure(self) -> None:
        volume = volume_with(((0, 0, 0), "stone"))
        with pytest.raises(BlueprintError, match="outside"):
            render_layer(volume, 5)
        with pytest.raises(BlueprintError, match="no blocks"):
            render_layer(BlockVolume(), 0)

    def test_layer_name_is_zero_padded(self) -> None:
        from mcblueprint.model import AABB

        bounds = AABB.of(Vec3(0, 0, 0), Vec3(0, 12, 0))
        assert layer_name(3, bounds) == "y03" and layer_name(12, bounds) == "y12"
        assert layer_name(-2, AABB.of(Vec3(0, -5, 0), Vec3(0, 3, 0))) == "y-2"

    def test_elevation_highlights_exposed_top_and_side(self) -> None:
        volume = volume_with(((0, 0, 0), "stone"), ((0, 1, 0), "stone"), ((1, 0, 0), "stone"))
        image = render(volume, "south", scale=8)
        # column 0 (x=0): top block's top edge is lighter than its body
        body = image.getpixel((3, 4))
        assert image.getpixel((3, 1)) == shade((127, 127, 127), 1.25)
        assert body == shade((127, 127, 127), 0.9)
        # the right edge of x=1 (nothing beyond) is darker, the right edge of x=0 at y=0 is not
        assert image.getpixel((15, 12)) == shade((127, 127, 127), 0.55)
        assert image.getpixel((7, 12)) == body

    def test_top_view_marks_steps(self) -> None:
        volume = volume_with(((0, 1, 0), "stone"), ((1, 0, 0), "stone"))
        image = render(volume, "top", scale=8)
        # x=0 is higher than x=1: its right edge is dark; x=1's right edge is dark (nothing)
        assert image.getpixel((7, 3)) == shade((127, 127, 127), 0.55)
        assert image.getpixel((15, 3)) == shade((127, 127, 127), 0.55)

    def test_grid_adds_margin_labels_and_origin(self) -> None:
        volume = volume_with(*(((x, 0, z), "stone") for x in range(6) for z in range(6)))
        plain = render(volume, "top", scale=8)
        gridded = render(volume, "top", options=Options(scale=8, grid=5, origin=Vec3(2, 0, 3)))
        assert gridded.size == (plain.size[0] + 14, plain.size[1] + 14)
        # a major grid line at x=5 runs through the margin
        assert gridded.getpixel((14 + 5 * 8, 2)) == (160, 160, 160)
        # origin cell outlined in red
        assert gridded.getpixel((14 + 2 * 8, 14 + 3 * 8)) == ORIGIN
        # label text (antialiased dark pixels) was drawn somewhere in the margin
        assert any(
            gridded.getpixel((x, y))[0] < 140 and gridded.getpixel((x, y)) != (160, 160, 160)
            for x in range(14, 60)
            for y in range(0, 12)
        )
        iso = render(volume, "isometric", options=Options(scale=8, grid=5, origin=Vec3(2, 0, 3)))
        assert any(
            iso.getpixel((x, y)) == ORIGIN for x in range(iso.width) for y in range(iso.height)
        )

    def test_origin_outside_the_image_is_ignored(self) -> None:
        volume = volume_with(((0, 0, 0), "stone"))
        image = render(volume, "top", options=Options(scale=8, origin=Vec3(9, 0, 9)))
        assert image.size == (8, 8)

    def test_isometric_outlines(self) -> None:
        volume = volume_with(((0, 0, 0), "stone"))
        image = render(volume, "isometric", scale=8)
        assert any(
            image.getpixel((x, y)) == shade((127, 127, 127), 0.45)
            for x in range(image.width)
            for y in range(image.height)
        )

    def test_uncoloured_blocks(self) -> None:
        volume = volume_with(
            ((0, 0, 0), "stone"), ((1, 0, 0), "minecraft:sniffer_egg"), ((2, 0, 0), "air")
        )
        assert uncoloured_blocks(volume) == []
        volume.set(Vec3(3, 0, 0), B("water"))
        volume.set(Vec3(4, 0, 0), B("chest"))
        assert uncoloured_blocks(volume) == ["minecraft:chest", "minecraft:water"]

    def test_write_layers(self, tmp_path: Path) -> None:
        volume = volume_with(((0, 0, 0), "stone"), ((0, 3, 0), "stone"))
        paths = write_previews(volume, tmp_path, "x", views=[], layers=[0, 3])
        assert [p.name for p in paths] == ["x-y0.png", "x-y3.png"]


class TestCliLayers:
    DATA = {
        "formatVersion": 1,
        "minecraftVersion": "1.21.11",
        "name": "p",
        "origin": [1, 0, 1],
        "operations": [
            {"type": "fill", "from": [0, 0, 0], "to": [3, 2, 3], "block": "stone"},
            {"type": "set", "position": [1, 3, 1], "block": "sniffer_egg"},
        ],
    }

    def write(self, tmp_path: Path) -> Path:
        path = tmp_path / "p.json"
        path.write_text(json.dumps(self.DATA), encoding="utf-8")
        return path

    def test_layers_only(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        path = self.write(tmp_path)
        assert main(["preview", str(path), "-o", str(tmp_path), "--layers", "all"]) == EXIT_OK
        out = capsys.readouterr().out
        names = [
            Path(line.split(" ", 1)[1]).name
            for line in out.splitlines()
            if line.startswith("Wrote")
        ]
        assert names == ["p-y0.png", "p-y1.png", "p-y2.png", "p-y3.png"]
        assert "WARNING:" not in out

    def test_layer_range_and_single_with_views(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = self.write(tmp_path)
        assert (
            main(
                [
                    "preview",
                    str(path),
                    "-o",
                    str(tmp_path),
                    "--layers",
                    "1..2",
                    "--layer",
                    "0",
                    "--views",
                    "top",
                    "--grid",
                    "2",
                ]
            )
            == EXIT_OK
        )
        names = [
            Path(line.split(" ", 1)[1]).name
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("Wrote")
        ]
        assert names == ["p-top.png", "p-y0.png", "p-y1.png", "p-y2.png"]
        assert Image.open(tmp_path / "p-top.png").size == (4 * 8 + 14, 4 * 8 + 14)
        assert main(["preview", str(path), "-o", str(tmp_path), "--layers", "..1"]) == EXIT_OK
        assert (
            main(["preview", str(path), "-o", str(tmp_path), "--layers", "9"]) == EXIT_USAGE_ERROR
        )
        assert (
            main(["preview", str(path), "-o", str(tmp_path), "--layers", "x"]) == EXIT_USAGE_ERROR
        )
        assert main(["preview", str(path), "-o", str(tmp_path), "--grid", "0"]) == EXIT_USAGE_ERROR


SHAPES = ("straight", "inner_left", "inner_right", "outer_left", "outer_right")
FACINGS = ("north", "east", "south", "west")


class TestIsometricShapes:
    @pytest.mark.parametrize("facing", FACINGS)
    @pytest.mark.parametrize("half", ("bottom", "top"))
    @pytest.mark.parametrize("shape", SHAPES)
    def test_stair_occupancy(self, facing: str, half: str, shape: str) -> None:
        # 北向きの高段を手で定義し、時計回りに回した期待形状と照合する。
        footprints = {
            "straight": {(0, 0), (1, 0)},
            "inner_left": {(0, 0), (1, 0), (0, 1)},
            "inner_right": {(0, 0), (1, 0), (1, 1)},
            "outer_left": {(0, 0)},
            "outer_right": {(1, 0)},
        }
        footprint = footprints[shape]
        for _ in range(FACINGS.index(facing)):
            footprint = {(1 - z, x) for x, z in footprint}
        base, step = (0, 1) if half == "bottom" else (1, 0)
        expected = {Vec3(x, base, z) for x in range(2) for z in range(2)}
        expected |= {Vec3(x, step, z) for x, z in footprint}
        state = B(f"oak_stairs[facing={facing},half={half},shape={shape}]")
        assert _shape_cells(state) == expected

    @pytest.mark.parametrize(
        ("kind", "levels"), [("bottom", {0}), ("top", {1}), ("double", {0, 1})]
    )
    def test_slab_occupancy(self, kind: str, levels: set[int]) -> None:
        assert _shape_cells(B(f"stone_slab[type={kind}]")) == {
            Vec3(x, y, z) for x in range(2) for y in levels for z in range(2)
        }

    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("facing", FACINGS)
    @pytest.mark.parametrize("axis", ("x", "y", "z"))
    @pytest.mark.parametrize("half", ("bottom", "top"))
    def test_mirrored_states_match_reflected_geometry(
        self, shape: str, facing: str, axis: str, half: str
    ) -> None:
        from mcblueprint.operations.transform import Transform

        state = B(f"oak_stairs[facing={facing},half={half},shape={shape}]")
        mirror = Transform.mirror(axis, 0.5)
        assert _shape_cells(mirror.apply_state(state)) == {
            mirror.apply(p) for p in _shape_cells(state)
        }

    def test_defaults_and_unsupported_shapes(self) -> None:
        default_stairs = B("oak_stairs[facing=north,half=bottom,shape=straight]")
        assert _shape_cells(B("oak_stairs")) == _shape_cells(default_stairs)
        assert _shape_cells(B("stone_slab")) == _shape_cells(B("stone_slab[type=bottom]"))
        cube = _shape_cells(B("stone"))
        for block in (
            "oak_fence", "custom:oak_stairs",
            "oak_stairs[shape=unknown]", "stone_slab[type=unknown]"
        ):
            assert _shape_cells(B(block)) == cube

    @pytest.mark.parametrize("axis", ("x", "y", "z"))
    @pytest.mark.parametrize(("kind", "area"), [("bottom", 0.5), ("top", 0.5), ("double", 0)])
    def test_neighbour_only_hides_occupied_contact(self, axis: str, kind: str, area: float) -> None:
        neighbour = Vec3(0, 0, 0).with_axis(axis, 1)
        volume = volume_with(((0, 0, 0), "stone"), (tuple(neighbour), f"oak_slab[type={kind}]"))
        # y 境界では bottom/double だけが石の上面へ接触する。
        expected = (1 if kind == "top" else 0) if axis == "y" else area
        stone_color = block_color(B("stone"))
        exposed = 0.0
        for vertices, color, _ in _isometric_faces(volume):
            if color == stone_color and all(p.axis(axis) == 2 for p in vertices):
                other_axes = [a for a in ("x", "y", "z") if a != axis]
                spans = [
                    max(p.axis(a) for p in vertices) - min(p.axis(a) for p in vertices)
                    for a in other_axes
                ]
                exposed += spans[0] * spans[1] / 4
        assert exposed == expected

    def test_full_cube_faces_are_merged_and_hidden(self) -> None:
        assert len(list(_isometric_faces(volume_with(((0, 0, 0), "stone"))))) == 3
        volume = volume_with(
            *(((x, y, z), "stone") for x in range(2) for y in range(2) for z in range(2))
        )
        assert len(list(_isometric_faces(volume))) == 12

    def test_shapes_change_only_isometric_view(self) -> None:
        cube = volume_with(((0, 0, 0), "oak_planks"))
        cube_iso = render(cube, "isometric", scale=24).tobytes()
        for block in (
            "oak_stairs[facing=south,shape=outer_left]",
            "oak_slab[type=top]", "oak_slab[type=bottom]"
        ):
            volume = volume_with(((0, 0, 0), block))
            assert render(volume, "isometric", scale=24).tobytes() != cube_iso
            for view in VIEWS[:-1]:
                assert render(volume, view).tobytes() == render(cube, view).tobytes()
            assert render_layer(volume, 0).tobytes() == render_layer(cube, 0).tobytes()
        double = volume_with(((0, 0, 0), "oak_slab[type=double]"))
        assert render(double, "isometric", scale=24).tobytes() == cube_iso

    def test_fixed_shape_fixture(self) -> None:
        from mcblueprint.generator import generate
        from mcblueprint.loader import load_blueprint

        path = Path(__file__).parent / "data" / "preview_shapes.json"
        volume = generate(load_blueprint(path))
        stairs = [state for _, state in volume if state.id.endswith("_stairs")]
        assert {(s.get("facing"), s.get("half"), s.get("shape")) for s in stairs} == {
            (facing, half, shape)
            for facing in FACINGS for half in ("bottom", "top") for shape in SHAPES
        }
        assert render(volume, "isometric", scale=8).getbbox() is not None

    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("angle", (90, 180, 270))
    def test_rotated_states_match_geometry(self, shape: str, angle: int) -> None:
        from mcblueprint.operations.transform import Transform

        state = B(f"oak_stairs[facing=north,half=bottom,shape={shape}]")
        rotation = Transform.rotation(angle, Vec3(0, 0, 0))
        offset = {90: Vec3(1, 0, 0), 180: Vec3(1, 0, 1), 270: Vec3(0, 0, 1)}[angle]
        assert _shape_cells(rotation.apply_state(state)) == {
            rotation.apply(p) + offset for p in _shape_cells(state)
        }

    @pytest.mark.parametrize(
        ("scale", "expected"),
        [
            (8, "6d12ac8876d00955bf8a5ad847dacc4d66baa03fe87ee60c235b71bc335b8f32"),
            (16, "f4e053b7d7b5de9bb5fe143f78cdbbdc616acdf85c1aca60ad6b0c4c38a21c2e"),
        ],
    )
    def test_single_cube_pixels(self, scale: int, expected: str) -> None:
        image = render(volume_with(((0, 0, 0), "stone")), "isometric", scale=scale)
        assert hashlib.sha256(image.tobytes()).hexdigest() == expected

    def test_depth_order_matches_independent_ray_intersections(self) -> None:
        volume = volume_with(
            ((-1, 0, -1), "stone"),
            ((0, 0, -1), "oak_stairs[facing=east,half=top,shape=inner_left]"),
            ((-1, 1, -1), "oak_slab[type=top]"),
            ((-1, 0, 0), "gold_block"),
            ((0, 0, 0), "oak_stairs[facing=north,shape=outer_left]"),
            ((0, 1, 0), "stone_slab[type=bottom]"),
        )
        bounds = volume.bounds()
        assert bounds is not None
        scale = 24
        image = render(volume, "isometric", scale=scale)
        cells = [
            (p + (pos - bounds.min) * 2, block_color(state))
            for pos, state in volume for p in _shape_cells(state)
        ]
        checked = 0
        for px in range(image.width):
            for py in range(image.height):
                a = (px - bounds.size.z * scale) * 2 / scale
                b = (py - (bounds.size.y + 1) * scale) * 2 / scale
                offset = (a, a / 2 - b, 0)
                hits = []
                edge_ray = False
                for cell, color in cells:
                    low = max(cell[i] - offset[i] for i in range(3))
                    exits = [cell[i] + 1 - offset[i] for i in range(3)]
                    high = min(exits)
                    edge_ray |= abs(high - low) < 0.15
                    if high <= low:
                        continue
                    axis = exits.index(high)
                    # 輪郭のラスタライズを除き、面の内部だけを独立したレイで照合する。
                    point = [offset[i] + high - cell[i] for i in range(3)]
                    interior = all(0.15 < point[i] < 0.85 for i in range(3) if i != axis)
                    hits.append((high, axis, color, interior))
                if hits and not edge_ray:
                    _, axis, color, interior = max(hits, key=lambda hit: hit[0])
                    if interior:
                        assert color is not None
                        assert image.getpixel((px, py)) == shade(color, (0.75, 1.05, 0.6)[axis]), (
                            px, py, axis, color
                        )
                        checked += 1
        assert checked > 500

    @pytest.mark.parametrize("scale", (2, 3, 7, 8, 16))
    def test_translation_does_not_change_shapes(self, scale: int) -> None:
        cells = (
            ((0, 0, 0), "oak_stairs[facing=west,shape=inner_right]"),
            ((1, 0, 0), "oak_slab[type=top]"),
        )
        shifted = tuple(((x - 4, y - 2, z - 5), block) for (x, y, z), block in cells)
        assert render(volume_with(*cells), "isometric", scale=scale).tobytes() == render(
            volume_with(*shifted), "isometric", scale=scale
        ).tobytes()


class TestPreviewRegion:
    @pytest.mark.parametrize("view", VIEWS)
    def test_region_removes_outside_occlusion_and_keeps_coordinates(self, view: str) -> None:
        inside = (
            ((-2, -1, -3), "oak_stairs[facing=south,shape=inner_left]"),
            ((0, 1, -1), "oak_slab[type=top]"),
            ((-1, 0, -2), "stone"),
        )
        volume = volume_with(*inside, ((1, 1, 0), "gold_block"), ((-3, -2, -4), "stone"))
        region = AABB(Vec3(-2, -1, -3), Vec3(0, 1, -1))
        options = Options(scale=16, grid=1, origin=Vec3(-1, 0, -2), region=region, source="家.json")
        image = render(volume, view, options=options)
        expected = render(
            volume_with(*inside), view,
            options=Options(scale=16, grid=1, origin=Vec3(-1, 0, -2)),
        )
        assert image.tobytes() == expected.tobytes()
        info = json.loads(image.info["mcblueprint"])
        assert info["sourceBlueprint"] == "家.json"
        assert info["coordinateSystem"] == "blueprint"
        assert info["origin"] == [-1, 0, -2]
        assert info["sourceBounds"] == {"min": [-3, -2, -4], "max": [1, 1, 0]}
        assert info["requestedBounds"] == info["renderedBounds"] == {
            "min": [-2, -1, -3], "max": [0, 1, -1],
        }
        assert info["view"] == view and info["layer"] is None
        assert len(volume) == 5 and volume.get(Vec3(1, 1, 0)) == B("gold_block")

    @pytest.mark.parametrize("view", VIEWS)
    def test_large_region_clips_to_generated_bounds(self, view: str) -> None:
        volume = volume_with(((-2, -1, -3), "stone"), ((1, 1, 0), "oak_slab[type=bottom]"))
        region = AABB(Vec3(-10**9, -10**9, -10**9), Vec3(10**9, 10**9, 10**9))
        image = render(volume, view, options=Options(scale=8, region=region))
        full = render(volume, view, scale=8)
        assert image.size == full.size and image.tobytes() == full.tobytes()
        info = json.loads(image.info["mcblueprint"])
        assert info["requestedBounds"] == {"min": [-10**9] * 3, "max": [10**9] * 3}
        assert info["renderedBounds"] == info["sourceBounds"]

    def test_empty_cells_at_region_edges_keep_requested_extent(self) -> None:
        volume = volume_with(((0, 0, 0), "stone"), ((4, 4, 4), "stone"))
        options = Options(scale=4, region=AABB(Vec3(0, 0, 0), Vec3(2, 2, 2)))
        assert render(volume, "top", options=options).size == (12, 12)
        assert render(volume, "north", options=options).size == (12, 12)
        assert render(volume, "isometric", options=options).size == (24, 28)

    @pytest.mark.parametrize(
        ("low", "high", "message"),
        [((4, 4, 4), (5, 5, 5), "outside")],
    )
    def test_invalid_region(self, low: tuple, high: tuple, message: str) -> None:
        volume = volume_with(((0, 0, 0), "stone"))
        with pytest.raises(BlueprintError, match=message):
            render(volume, "top", options=Options(region=AABB(Vec3(*low), Vec3(*high))))

    @pytest.mark.parametrize("block", ("air", "cave_air"))
    def test_air_only_selection_does_not_create_output(self, tmp_path: Path, block: str) -> None:
        volume = volume_with(((0, 0, 0), block), ((2, 2, 2), "stone"))
        options = Options(region=AABB(Vec3(0, 0, 0), Vec3(1, 1, 1)))
        out = tmp_path / "empty"
        with pytest.raises(BlueprintError, match="no visible blocks"):
            write_previews(volume, out, "x", options=options)
        assert not out.exists()

    def test_layer_omits_ghost_below_selection_and_rejects_outside_y(self) -> None:
        volume = volume_with(((0, 0, 0), "gold_block"), ((1, 2, 0), "oak_slab[type=top]"))
        options = Options(scale=4, region=AABB(Vec3(0, 1, 0), Vec3(1, 2, 0)))
        image = render_layer(volume, 1, options=options)
        assert image.tobytes() == bytes(BACKGROUND) * image.width * image.height
        assert json.loads(image.info["mcblueprint"])["layer"] == 1
        with pytest.raises(BlueprintError, match="outside"):
            render_layer(volume, 0, options=options)

    def test_region_png_and_manifest_preserve_reference(self, tmp_path: Path) -> None:
        volume = volume_with(((-2, 0, 0), "stone"), ((1, 2, 2), "oak_slab[type=top]"))
        options = Options(region=AABB(Vec3(-2, 0, 0), Vec3(0, 1, 1)), source="blueprints/家.json")
        full = write_previews(volume, tmp_path, "家", views=["top"])[0]
        original = full.read_bytes()
        paths = write_previews(volume, tmp_path, "家", views=["top"], layers=[0], options=options)
        assert [p.name for p in paths] == [
            "家-region-top.png", "家-region-y0.png", "家-region.json"
        ]
        records = json.loads(paths[-1].read_text(encoding="utf-8"))
        assert records[0]["image"] == paths[0].name
        assert records[0]["sourceBlueprint"] == "blueprints/家.json"
        assert json.loads(Image.open(paths[0]).info["mcblueprint"]) == {
            key: value for key, value in records[0].items() if key != "image"
        }
        assert records[1]["view"] == "layer" and records[1]["layer"] == 0
        assert full.read_bytes() == original

    def test_invalid_layer_writes_no_partial_success(self, tmp_path: Path) -> None:
        volume = volume_with(((0, 0, 0), "stone"), ((1, 2, 1), "stone"))
        options = Options(region=AABB(Vec3(0, 0, 0), Vec3(1, 1, 1)))
        out = tmp_path / "invalid"
        with pytest.raises(BlueprintError, match="outside"):
            write_previews(volume, out, "x", views=["top"], layers=[0, 2], options=options)
        assert not out.exists()


class TestCliPreviewRegion:
    DATA = {
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "region",
        "origin": [-1, 0, -1],
        "operations": [
            {"type": "fill", "from": [-2, -1, -3], "to": [2, 3, 2], "block": "stone"},
            {"type": "set", "position": [0, 1, 0], "block": "oak_stairs[facing=north]"},
        ],
    }

    def test_views_layers_and_source_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mcblueprint.cli as cli

        path = tmp_path / "region.json"
        path.write_text(json.dumps(self.DATA), encoding="utf-8")
        calls = []
        generate = cli.generate

        def tracked_generate(*args: Any, **kwargs: Any) -> BlockVolume:
            result = generate(*args, **kwargs)
            calls.append(result.bounds())
            return result

        monkeypatch.setattr(cli, "generate", tracked_generate)
        out = tmp_path / "images"
        assert main([
            "preview", str(path), "-o", str(out), "--from", "-1", "0", "-2",
            "--to", "1", "1", "1", "--views", ",".join(VIEWS), "--layers", "all", "--grid", "1",
        ]) == EXIT_OK
        assert calls == [AABB(Vec3(-2, -1, -3), Vec3(2, 3, 2))]
        records = json.loads((out / "region-region.json").read_text(encoding="utf-8"))
        assert [r["view"] for r in records] == list(VIEWS) + ["layer", "layer"]
        assert [r["layer"] for r in records[-2:]] == [0, 1]
        assert all(r["sourceBlueprint"] == str(path.resolve()) for r in records)
        assert all(r["renderedBounds"] == {"min": [-1, 0, -2], "max": [1, 1, 1]} for r in records)
        assert Image.open(out / "region-region-top.png").size == (3 * 8 + 14, 4 * 8 + 14)

    @pytest.mark.parametrize(
        "extra",
        [
            ["--from", "0", "0", "0"],
            ["--to", "1", "1", "1"],
            ["--from", "1", "0", "0", "--to", "0", "0", "0"],
            ["--from", "5", "5", "5", "--to", "6", "6", "6"],
            ["--from", "0", "0", "0", "--to", "1", "1", "1", "--layer", "2"],
        ],
    )
    def test_invalid_cli_selection_creates_no_success(
        self, tmp_path: Path, extra: list[str]
    ) -> None:
        path = tmp_path / "region.json"
        path.write_text(json.dumps(self.DATA), encoding="utf-8")
        out = tmp_path / "images"
        assert main(["preview", str(path), "-o", str(out), *extra]) == EXIT_USAGE_ERROR
        assert not out.exists()

    def test_empty_cli_selection_creates_no_success(self, tmp_path: Path) -> None:
        data = {**self.DATA, "operations": [
            {"type": "set", "position": [0, 0, 0], "block": "stone"},
            {"type": "set", "position": [4, 4, 4], "block": "stone"},
        ]}
        path = tmp_path / "region.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        out = tmp_path / "images"
        assert main([
            "preview", str(path), "-o", str(out), "--from", "1", "1", "1", "--to", "3", "3", "3",
        ]) == EXIT_USAGE_ERROR
        assert not out.exists()
