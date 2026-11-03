"""Preview images of a generated BlockVolume (requires the optional ``Pillow`` dependency).

Views: ``top`` (plan, lighter = higher), ``north`` / ``south`` / ``east`` / ``west``
(elevations seen from that side), ``isometric`` (2:1 pixel isometric, viewed from
the south-east) and horizontal sections (``render_layer``: one y level, with the
level below ghosted for context).

Every cell gets a flat colour plus simple shading: a light line where a block's
top face is exposed, a dark line where a side face is exposed (elevations, top
view and sections), and darker side faces with outlines in the isometric view.
``grid`` draws heavier lines every N blocks with world coordinates in a margin,
and ``origin`` marks the blueprint origin in red.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from statistics import median
from typing import Any

from mcblueprint.blockdata import load_block_data
from mcblueprint.colors import face_linear as _face_linear
from mcblueprint.colors import oklab_to_linear as _oklab_to_linear
from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.volume import BlockVolume

RGB = tuple[int, int, int]
VIEWS = ("top", "north", "south", "east", "west", "isometric")
DEFAULT_VIEWS = ("top", "north", "east", "isometric")
BACKGROUND: RGB = (245, 245, 245)
GRID: RGB = (225, 225, 225)
MAJOR_GRID: RGB = (160, 160, 160)
LABEL: RGB = (90, 90, 90)
ORIGIN: RGB = (220, 40, 40)
GHOST_BLEND = 0.7  # how far the layer below is faded towards the background
MARGIN = 14  # pixels reserved for coordinate labels when a grid is drawn


def _pil() -> Any:
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover - exercised via the CLI test with a stub
        raise BlueprintError(
            "Preview needs the optional dependency Pillow: pip install 'mcblueprint[preview]'"
        ) from None
    return Image, ImageDraw


# --- colours ------------------------------------------------------------------


def _srgb(linear: Iterable[float]) -> RGB:
    def encode(value: float) -> int:
        value = max(0.0, min(1.0, value))
        value = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
        return round(value * 255)

    return tuple(encode(value) for value in linear)  # type: ignore[return-value]


@cache
def _material_colors(version: str, face: str) -> dict[str, RGB]:
    data = load_block_data(version)
    samples: dict[str, list[tuple[float, ...]]] = {}
    for info in data.blocks():
        if info.faces:
            source = info.faces.get(face, next(iter(info.faces.values())))
            samples.setdefault(info.material, []).append(_face_linear(info, source, data))
    return {
        material: _srgb(median(values) for values in zip(*colors, strict=True))
        for material, colors in samples.items()
    }


def _blend(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(round(x * (1 - t) + y * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def block_color(state: BlockState, version: str = "1.21.11", face: str = "top") -> RGB | None:
    """既定状態の面色を返す。face は top / side / bottom、着色は plains。

    空気3種は None。欠けた面は同ブロックの別面、水の面色欠損は水の着色値、
    その他の全方面欠損は同版・同材質の中央値で補完する。未知のIDは BlueprintError。
    """
    return _block_color(state.id, version, face)


@cache
def _block_color(block_id: str, version: str, face: str) -> RGB | None:
    data = load_block_data(version)
    info = data.get(block_id)
    if info is None:
        raise BlueprintError(f"Unknown block id: {block_id}")
    if block_id in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air"):
        return None
    if info.faces:
        source = info.faces.get(face, next(iter(info.faces.values())))
        return _srgb(_face_linear(info, source, data))
    if info.tint == "water":
        return _srgb(_oklab_to_linear(data.tints["water"]["plains"]))
    return _material_colors(version, face)[info.material]


def uncoloured_blocks(volume: BlockVolume, version: str = "1.21.11") -> list[str]:
    """面色を持たず、着色値または材質色で補完する非空気ブロックIDを返す。"""
    data = load_block_data(version)
    return sorted({
        state.id for _, state in volume
        if (info := data.get(state.id)) is not None and not info.faces
        and state.id not in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air")
    })


def shade(color: RGB, factor: float) -> RGB:
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


# --- rendering ----------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """描画設定。region は Blueprint 座標の両端包含範囲で、生成範囲との交差を描く。

    範囲外の遮蔽と断面の ghost は除外する。source は範囲画像の元 Blueprint 参照
    （API 呼出元が指定するパス等）。範囲指定時だけ PNG 情報と出力 JSON に残す。
    version は面色と着色表を取得する同梱 Minecraft 版で、既定は 1.21.11。
    """

    scale: int = 8
    grid: int | None = None  # draw major grid lines and labels every ``grid`` blocks
    origin: Vec3 | None = None  # blueprint origin to mark
    region: AABB | None = None
    source: str | None = None
    version: str = "1.21.11"


def selected_bounds(bounds: AABB, region: AABB | None) -> AABB:
    """元範囲と選択範囲の交差を返す。完全範囲外なら BlueprintError。"""
    if region is None:
        return bounds
    low, high = Vec3.max(bounds.min, region.min), Vec3.min(bounds.max, region.max)
    if any(low[i] > high[i] for i in range(3)):
        raise BlueprintError(f"Preview region {region} is outside the structure ({bounds})")
    return AABB(low, high)


def _select_region(volume: BlockVolume, bounds: AABB, version: str) -> BlockVolume:
    selected = BlockVolume()
    for pos, state in volume:
        if bounds.contains(pos):
            selected.set(pos, state)
    if not any(_visible(selected, version)):
        raise BlueprintError(f"Nothing to preview: no visible blocks in region ({bounds})")
    return selected


def _region_info(
    image: Any, source_bounds: AABB, bounds: AABB, options: Options,
    view: str, layer: int | None = None,
) -> Any:
    if options.region is not None:
        image.info["mcblueprint"] = json.dumps({
            "sourceBlueprint": options.source,
            "coordinateSystem": "blueprint",
            "origin": options.origin.to_list() if options.origin is not None else None,
            "sourceBounds": {
                "min": source_bounds.min.to_list(), "max": source_bounds.max.to_list(),
            },
            "requestedBounds": {
                "min": options.region.min.to_list(), "max": options.region.max.to_list(),
            },
            "renderedBounds": {"min": bounds.min.to_list(), "max": bounds.max.to_list()},
            "view": view,
            "layer": layer,
            "outsideOcclusion": "removed",
            "cutFaces": "exposed-block-geometry",
            "layerGhost": "within-selection",
        }, ensure_ascii=False)
    return image


def render(volume: BlockVolume, view: str, scale: int = 8, options: Options | None = None) -> Any:
    """生成済み volume を描く。範囲指定時も座標を移動せず、元 volume を変更しない。

    戻り値は Pillow Image。範囲が空なら BlueprintError。範囲画像の info["mcblueprint"]
    に元範囲・要求/描画範囲・座標系・元入力参照・view を JSON で保持する。
    """
    if view not in VIEWS:
        raise BlueprintError(f"Unknown view {view!r}; choose from {', '.join(VIEWS)}")
    source_bounds = volume.bounds()
    if source_bounds is None:
        raise BlueprintError("Nothing to preview: no blocks were generated")
    options = options or Options(scale=scale)
    bounds = selected_bounds(source_bounds, options.region)
    if options.region is not None:
        volume = _select_region(volume, bounds, options.version)
    if view == "top":
        image = _render_top(volume, bounds, options)
    elif view == "isometric":
        image = _render_isometric(volume, bounds, options)
    else:
        image = _render_elevation(volume, bounds, options, view)
    return _region_info(image, source_bounds, bounds, options, view)


def render_layer(
    volume: BlockVolume, y: int, scale: int = 8, options: Options | None = None
) -> Any:
    """高さ y の水平断面。覆われない直下段を薄く表示する。

    region 指定時は選択内だけを描き、範囲外の直下段を含めない。y が実描画範囲外、
    または選択範囲に可視ブロックがなければ BlueprintError。画像情報は render と同じ。
    """
    source_bounds = volume.bounds()
    if source_bounds is None:
        raise BlueprintError("Nothing to preview: no blocks were generated")
    options = options or Options(scale=scale)
    bounds = selected_bounds(source_bounds, options.region)
    if options.region is not None:
        volume = _select_region(volume, bounds, options.version)
    if not bounds.min.y <= y <= bounds.max.y:
        raise BlueprintError(
            f"Layer y={y} is outside the preview bounds (y {bounds.min.y}..{bounds.max.y})"
        )
    cells: dict[tuple[int, int], RGB] = {}
    ghosts: dict[tuple[int, int], RGB] = {}
    for pos, color in _visible(volume, options.version):
        if pos.y == y:
            cells[(pos.x, pos.z)] = color
        elif pos.y == y - 1:
            ghosts[(pos.x, pos.z)] = _blend(color, BACKGROUND, GHOST_BLEND)
    canvas = _Canvas(
        bounds.size.x,
        bounds.size.z,
        options,
        lambda c: bounds.min.x + c,
        lambda r: bounds.min.z + r,
    )
    for (x, z), color in ghosts.items():
        if (x, z) not in cells:
            canvas.cell(x - bounds.min.x, z - bounds.min.z, color)
    for (x, z), color in cells.items():
        cx, cz = x - bounds.min.x, z - bounds.min.z
        canvas.cell(cx, cz, color)
        canvas.edges(cx, cz, color, (x + 1, z) not in cells, (x, z + 1) not in cells)
    canvas.grid(("x", "z"))
    if options.origin is not None:
        canvas.mark(options.origin.x - bounds.min.x, options.origin.z - bounds.min.z)
    return _region_info(canvas.image, source_bounds, bounds, options, "layer", y)


def _visible(
    volume: BlockVolume, version: str = "1.21.11", face: str = "top",
) -> Iterable[tuple[Vec3, RGB]]:
    for pos, state in volume:
        color = block_color(state, version, face)
        if color is not None:
            yield pos, color


class _Canvas:
    """Cell grid drawing helper: ``columns`` x ``rows`` cells of ``scale`` pixels with an
    optional label margin. ``column_world`` / ``row_world`` map a grid line index to
    the world coordinate written next to it."""

    def __init__(
        self,
        columns: int,
        rows: int,
        options: Options,
        column_world: Callable[[int], int],
        row_world: Callable[[int], int],
    ) -> None:
        Image, ImageDraw = _pil()
        self.scale = options.scale
        self.options = options
        self.margin = MARGIN if options.grid else 0
        self.columns, self.rows = columns, rows
        self.column_world = column_world
        self.row_world = row_world
        self.image = Image.new(
            "RGB", (columns * self.scale + self.margin, rows * self.scale + self.margin), BACKGROUND
        )
        self.draw = ImageDraw.Draw(self.image)

    def _px(self, column: int, row: int) -> tuple[int, int]:
        return self.margin + column * self.scale, self.margin + row * self.scale

    def cell(self, column: int, row: int, color: RGB) -> None:
        px, py = self._px(column, row)
        self.draw.rectangle([px, py, px + self.scale - 1, py + self.scale - 1], fill=color)

    def edges(
        self, column: int, row: int, color: RGB, right_exposed: bool, bottom_exposed: bool
    ) -> None:
        """Dark lines on the right / bottom edge of a cell whose neighbour there is
        missing or lower (reads as a step or a wall in top views)."""
        if self.scale < 4:
            return
        px, py = self._px(column, row)
        dark = shade(color, 0.55)
        if right_exposed:
            self.draw.line(
                [(px + self.scale - 1, py), (px + self.scale - 1, py + self.scale - 1)], fill=dark
            )
        if bottom_exposed:
            self.draw.line(
                [(px, py + self.scale - 1), (px + self.scale - 1, py + self.scale - 1)], fill=dark
            )

    def top_highlight(self, column: int, row: int, color: RGB) -> None:
        """Light line along the top edge of a cell whose top face is exposed (elevations);
        one pixel in so the fine grid line drawn on the edge does not cover it."""
        if self.scale < 4:
            return
        px, py = self._px(column, row)
        self.draw.line([(px, py + 1), (px + self.scale - 1, py + 1)], fill=shade(color, 1.25))

    def grid(self, axes: tuple[str, str]) -> None:
        s = self.scale
        w, h = self.columns * s, self.rows * s
        if s >= 6:
            for c in range(self.columns + 1):
                x = self.margin + c * s
                self.draw.line([(x, self.margin), (x, self.margin + h)], fill=GRID)
            for r in range(self.rows + 1):
                y = self.margin + r * s
                self.draw.line([(self.margin, y), (self.margin + w, y)], fill=GRID)
        if not self.options.grid:
            return
        step = self.options.grid
        for c in range(self.columns + 1):
            world = self.column_world(c)
            if world % step == 0:
                x = self.margin + c * s
                self.draw.line([(x, 0), (x, self.margin + h)], fill=MAJOR_GRID)
                self.draw.text((x + 1, 0), str(world), fill=LABEL)
        for r in range(self.rows + 1):
            world = self.row_world(r)
            if world % step == 0:
                y = self.margin + r * s
                self.draw.line([(0, y), (self.margin + w, y)], fill=MAJOR_GRID)
                self.draw.text((0, y + 1), str(world), fill=LABEL)
        self.draw.text((1, 1), f"{axes[0]} {axes[1]}", fill=LABEL)

    def mark(self, column: int, row: int) -> None:
        if not (0 <= column < self.columns and 0 <= row < self.rows):
            return
        px, py = self._px(column, row)
        s = self.scale
        self.draw.rectangle([px, py, px + s - 1, py + s - 1], outline=ORIGIN, width=max(1, s // 6))
        self.draw.line([(px, py), (px + s - 1, py + s - 1)], fill=ORIGIN)
        self.draw.line([(px + s - 1, py), (px, py + s - 1)], fill=ORIGIN)


def _render_top(volume: BlockVolume, bounds: AABB, options: Options) -> Any:
    size = bounds.size
    highest: dict[tuple[int, int], tuple[int, RGB]] = {}
    for pos, color in _visible(volume, options.version):
        key = (pos.x, pos.z)
        if key not in highest or pos.y > highest[key][0]:
            highest[key] = (pos.y, color)
    span = max(1, size.y - 1)
    canvas = _Canvas(
        size.x, size.z, options, lambda c: bounds.min.x + c, lambda r: bounds.min.z + r
    )
    for (x, z), (y, color) in highest.items():
        factor = 0.6 + 0.4 * (y - bounds.min.y) / span
        cx, cz = x - bounds.min.x, z - bounds.min.z
        canvas.cell(cx, cz, shade(color, factor))
        right_lower = (x + 1, z) not in highest or highest[(x + 1, z)][0] < y
        front_lower = (x, z + 1) not in highest or highest[(x, z + 1)][0] < y
        canvas.edges(cx, cz, color, right_lower, front_lower)
    canvas.grid(("x", "z"))
    if options.origin is not None:
        canvas.mark(options.origin.x - bounds.min.x, options.origin.z - bounds.min.z)
    return canvas.image


def _render_elevation(volume: BlockVolume, bounds: AABB, options: Options, view: str) -> Any:
    size = bounds.size
    # horizontal screen axis and the depth axis (nearest block wins)
    if view in ("north", "south"):
        width = size.x
        column = lambda p: p.x - bounds.min.x if view == "south" else bounds.max.x - p.x  # noqa: E731
        depth = lambda p: p.z if view == "north" else -p.z  # noqa: E731
        lo, hi, axes = bounds.min.x, bounds.max.x, ("x", "y")
    else:
        width = size.z
        column = lambda p: p.z - bounds.min.z if view == "west" else bounds.max.z - p.z  # noqa: E731
        depth = lambda p: p.x if view == "west" else -p.x  # noqa: E731
        lo, hi, axes = bounds.min.z, bounds.max.z, ("z", "y")
    # the column index runs with the world axis for south / west, against it otherwise
    forward = view in ("south", "west")
    column_world = (lambda c: lo + c) if forward else (lambda c: hi + 1 - c)
    nearest: dict[tuple[int, int], tuple[int, RGB]] = {}
    for pos, color in _visible(volume, options.version, "side"):
        key = (column(pos), pos.y)
        d = depth(pos)
        if key not in nearest or d < nearest[key][0]:
            nearest[key] = (d, color)
    canvas = _Canvas(width, size.y, options, column_world, lambda r: bounds.max.y + 1 - r)
    for (c, y), (d, color) in nearest.items():
        row = bounds.max.y - y
        canvas.cell(c, row, shade(color, 0.9))
        if _farther(nearest, (c, y + 1), d):
            canvas.top_highlight(c, row, color)
        canvas.edges(
            c, row, color, _farther(nearest, (c + 1, y), d), _farther(nearest, (c, y - 1), d)
        )
    canvas.grid(axes)
    if options.origin is not None:
        o = options.origin
        canvas.mark(column(o), bounds.max.y - o.y)
    return canvas.image


def _farther(nearest: dict[tuple[int, int], tuple[int, RGB]], key: tuple[int, int], d: int) -> bool:
    """True when nothing is drawn at ``key`` or what is drawn there is behind depth ``d``."""
    return key not in nearest or nearest[key][0] > d


@cache
def _shape_cells(state: BlockState) -> frozenset[Vec3]:
    """アイソメトリック用の半ブロック占有セル。未対応形状は立方体で近似する。"""
    full = frozenset(Vec3(x, y, z) for x in range(2) for y in range(2) for z in range(2))
    props = dict(state.properties)
    if state.id.startswith("minecraft:") and state.id.endswith("_slab"):
        kind = props.get("type", "bottom")
        if kind in ("bottom", "top"):
            return frozenset(p for p in full if p.y == (kind == "top"))
    if state.id.startswith("minecraft:") and state.id.endswith("_stairs"):
        facing = props.get("facing", "north")
        half = props.get("half", "bottom")
        shape = props.get("shape", "straight")
        directions = {"north": (0, -1), "east": (1, 0), "south": (0, 1), "west": (-1, 0)}
        if facing not in directions or half not in ("bottom", "top") or shape not in (
            "straight", "inner_left", "inner_right", "outer_left", "outer_right"
        ):
            return full
        dx, dz = directions[facing]
        lx, lz = dz, -dx
        if shape.endswith("right"):
            lx, lz = -lx, -lz
        cells = set()
        for p in full:
            front = (p.x * 2 - 1) * dx + (p.z * 2 - 1) * dz > 0
            side = (p.x * 2 - 1) * lx + (p.z * 2 - 1) * lz > 0
            raised = front
            if shape.startswith("inner"):
                raised = front or side
            elif shape.startswith("outer"):
                raised = front and side
            if p.y == (half == "top") or raised:
                cells.add(p)
        return frozenset(cells)
    return full


def _isometric_faces(
    volume: BlockVolume, version: str = "1.21.11",
) -> Iterable[tuple[tuple[Vec3, ...], RGB, float]]:
    """隣接形状に覆われない上面・南面・東面を、同一ブロック内で結合して返す。"""
    blocks = {
        pos: (state, color) for pos, state in volume
        if (color := block_color(state, version)) is not None
    }
    directions = (Vec3(0, 1, 0), Vec3(1, 0, 0), Vec3(0, 0, 1))
    for pos, (state, color) in blocks.items():
        faces: dict[tuple[int, int], set[tuple[int, int]]] = {}
        for cell in _shape_cells(state):
            world = Vec3(pos.x * 2 + cell.x, pos.y * 2 + cell.y, pos.z * 2 + cell.z)
            for axis, delta in enumerate(directions):
                neighbour = world + delta
                other = blocks.get(Vec3(neighbour.x // 2, neighbour.y // 2, neighbour.z // 2))
                local = Vec3(neighbour.x % 2, neighbour.y % 2, neighbour.z % 2)
                if other is not None and local in _shape_cells(other[0]):
                    continue
                if axis == 0:
                    plane, u, v = world.y + 1, world.x, world.z
                elif axis == 1:
                    plane, u, v = world.x + 1, world.z, world.y
                else:
                    plane, u, v = world.z + 1, world.x, world.y
                faces.setdefault((axis, plane), set()).add((u, v))
        for (axis, plane), tiles in faces.items():
            while tiles:
                component = {min(tiles)}
                pending = list(component)
                while pending:
                    u, v = pending.pop()
                    for tile in ((u - 1, v), (u + 1, v), (u, v - 1), (u, v + 1)):
                        if tile in tiles and tile not in component:
                            component.add(tile)
                            pending.append(tile)
                tiles.difference_update(component)
                edges = set()
                for u, v in component:
                    corners = ((u, v), (u + 1, v), (u + 1, v + 1), (u, v + 1))
                    for start, end in zip(corners, corners[1:] + corners[:1], strict=True):
                        if (end, start) in edges:
                            edges.remove((end, start))
                        else:
                            edges.add((start, end))
                links = dict(edges)
                start = min(links)
                corners = [start]
                while (end := links[corners[-1]]) != start:
                    corners.append(end)
                # 共線の中点を除き、立方体の輪郭と描画順を維持する。
                corners = [
                    p for i, p in enumerate(corners)
                    if not (
                        corners[i - 1][0] == p[0] == corners[(i + 1) % len(corners)][0]
                        or corners[i - 1][1] == p[1] == corners[(i + 1) % len(corners)][1]
                    )
                ]
                if axis == 2:
                    corners.reverse()
                if axis == 0:
                    vertices = tuple(Vec3(a, plane, b) for a, b in corners)
                elif axis == 1:
                    vertices = tuple(Vec3(plane, b, a) for a, b in corners)
                else:
                    vertices = tuple(Vec3(a, b, plane) for a, b in corners)
                face_color = color if axis == 0 else block_color(state, version, "side")
                assert face_color is not None
                yield vertices, face_color, (1.05, 0.75, 0.6)[axis]


def _render_isometric(volume: BlockVolume, bounds: AABB, options: Options) -> Any:
    Image, ImageDraw = _pil()
    s = max(2, options.scale)
    half = s // 2
    size = bounds.size
    width = (size.x + size.z) * s
    height = (size.x + size.z) * half + size.y * s + s
    image = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    origin_x = size.z * s
    origin_y = size.y * s

    def project(p: Vec3) -> tuple[int, int]:
        x, y, z = p.x / 2 - bounds.min.x, p.y / 2 - bounds.min.y, p.z / 2 - bounds.min.z
        return round(origin_x + (x - z) * s), round(origin_y + s + (x + z) * half - y * s)

    faces = sorted(
        _isometric_faces(volume, options.version),
        key=lambda face: (
            sum(p.x + p.z for p in face[0]) / len(face[0]),
            sum(p.y for p in face[0]) / len(face[0]),
        ),
    )
    for vertices, color, factor in faces:
        outline = shade(color, 0.45) if s >= 6 else None
        draw.polygon([project(p) for p in vertices], fill=shade(color, factor), outline=outline)
    if options.origin is not None and bounds.min.y <= options.origin.y <= bounds.max.y:
        p = options.origin - bounds.min
        if 0 <= p.x < size.x and 0 <= p.z < size.z:
            cx = origin_x + (p.x - p.z) * s
            cy = origin_y + (p.x + p.z) * half - p.y * s
            draw.polygon(
                [(cx, cy), (cx + s, cy + half), (cx, cy + s), (cx - s, cy + half)],
                outline=ORIGIN,
            )
    return image


def layer_name(y: int, bounds: AABB) -> str:
    """``y03`` style suffix, zero-padded so file names sort by level."""
    width = max(len(str(abs(bounds.min.y))), len(str(abs(bounds.max.y))))
    return f"y{y:0{width + (1 if y < 0 else 0)}d}"


def write_previews(
    volume: BlockVolume,
    out_dir: Path,
    stem: str,
    views: Iterable[str] = DEFAULT_VIEWS,
    scale: int = 8,
    layers: Iterable[int] = (),
    options: Options | None = None,
) -> list[Path]:
    """PNG を保存してパスを返す。範囲指定時は region 接尾辞と座標情報 JSON も出力する。

    返すパスは PNG、最後に JSON の順。範囲指定なしのファイル名・出力は従来どおり。
    範囲/レベルのエラーは書込み前に検査する。source の参照値は Options で指定する。
    """
    options = options or Options(scale=scale)
    views, layers = list(views), list(layers)
    source_bounds = volume.bounds()
    if source_bounds is None:
        raise BlueprintError("Nothing to preview: no blocks were generated")
    bounds = selected_bounds(source_bounds, options.region)
    if options.region is not None:
        _select_region(volume, bounds, options.version)
        outside = [y for y in layers if not bounds.min.y <= y <= bounds.max.y]
        if outside:
            raise BlueprintError(f"Layer(s) {outside} are outside the preview bounds ({bounds})")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{stem}-region" if options.region is not None else stem
    written = []
    records = []
    targets = [(view, None, view) for view in views]
    targets.extend(("layer", y, layer_name(y, bounds)) for y in layers)
    for view, y, suffix in targets:
        image = (
            render(volume, view, options=options) if y is None
            else render_layer(volume, y, options=options)
        )
        path = out_dir / f"{prefix}-{suffix}.png"
        if options.region is not None:
            from PIL.PngImagePlugin import PngInfo

            info = PngInfo()
            info.add_text("mcblueprint", image.info["mcblueprint"])
            image.save(path, pnginfo=info)
            records.append({"image": path.name, **json.loads(image.info["mcblueprint"])})
        else:
            image.save(path)
        written.append(path)
    if options.region is not None:
        path = out_dir / f"{prefix}.json"
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written
