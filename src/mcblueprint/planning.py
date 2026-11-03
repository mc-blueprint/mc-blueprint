"""単独住宅・塔のSite、Massing、Detailを既存Operationで計画する。"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mcblueprint.buildspec import ResolveResult
from mcblueprint.detail import NORMALS, plan_detail
from mcblueprint.errors import BlueprintError
from mcblueprint.foundation import STABLE_GROUND
from mcblueprint.importers import read_terrain
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.building import BuildingFace, BuildingLayout
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.shapes import disc_offsets
from mcblueprint.operations.transform import Transform


@dataclass(frozen=True)
class PlanningResult:
    """計画段のJSONとMaterializeへの入力。ブロックの展開・材質抽選は行わない。

    site/massing/detailはformatVersion=1。blueprintは役割参照を含むため、
    materializeにstyle・biome・paletteを併せて渡してからloaderで読む。
    layoutは住宅のみ、入口・各階到着点を計画座標で保持する。
    """

    site: dict[str, Any]
    massing: dict[str, Any]
    detail: dict[str, Any]
    blueprint: dict[str, Any]
    palette: dict[str, Any]
    layout: BuildingLayout | None
    diagnostics: tuple[dict[str, Any], ...]


def _ref(role: str, shape: str = "full") -> dict[str, Any]:
    return {"role": role, "shape": shape, "state": {}}


def _tag(op, element, role, source, *, reserved=None, face=None):
    op = copy.deepcopy(op)
    op.update(id=element, element=element, role=role, source=source)
    if reserved:
        op.update(reserved=reserved, owner=element)
    if face:
        op["face"] = face
    return op


def _bounds(operations):
    boxes = [build_operation(op, "planning").bounds() for op in operations]
    return AABB.from_points([point for box in boxes for point in (box.min, box.max)])


def _grid_positions(length, spacing):
    positions = list(range(0, length, spacing))
    if positions[-1] != length - 1:
        positions.append(length - 1)
    return positions


def _column_clear(x, z, height, operations, root):
    for op in operations:
        if not (op.get("reserved") == "circulation"
                or op["element"].endswith((".light#1", ".threshold"))
                or op["type"] in ("window", "doorway")):
            continue
        box = build_operation(op, root).bounds()
        if box.min.x <= x <= box.max.x and box.min.z <= z <= box.max.z and box.min.y <= height:
            return False
    return True


def _projection(box, origin, axis):
    values = [((x - origin.x) * axis.x + (z - origin.z) * axis.z)
              for x in (box.min.x, box.max.x) for z in (box.min.z, box.max.z)]
    return {"u": [min(values), max(values)],
            "v": [box.min.y - origin.y, box.max.y - origin.y]}


def _plan_contract(root, body, detail, transform, params, layout):
    """Criticが期待構造と面上の開口を照合する座標契約を作る。"""
    rotation = Transform(transform.rows)
    frame = next(op for op in body if op["element"] == f"{root}.frame")
    frame_ops = frame["operations"]
    columns, beams = [], []
    for index, op in enumerate(frame_ops):
        if op["type"] == "fill" and op["from"][0] == op["to"][0] and op["from"][2] == op["to"][2]:
            start = transform.apply(Vec3.from_seq(op["from"]))
            end = transform.apply(Vec3.from_seq(op["to"]))
            columns.append({"id": f"{root}.column#{index + 1}",
                            "from": start.to_list(), "to": end.to_list()})
        else:
            bounds = transform.bounds(build_operation(op, root).bounds())
            beams.append({"id": f"{root}.beam#{len(beams) + 1}",
                          "y": bounds.min.y, "from": bounds.min.to_list(),
                          "to": bounds.max.to_list()})
    facades = []
    for op in body:
        if "face" not in op:
            continue
        bounds = build_operation(op, root).bounds()
        bounds = transform.bounds(bounds)
        directions = {"north": Vec3(0, 0, -1), "south": Vec3(0, 0, 1),
                      "west": Vec3(-1, 0, 0), "east": Vec3(1, 0, 0)}
        face = rotation.apply(directions[op["face"]])
        origin = transform.apply(build_operation(op, root).bounds().min)
        axis = rotation.apply(Vec3.from_seq(
            [1, 0, 0] if op["face"] in ("north", "south") else [0, 0, 1]
        ))
        openings = []
        for opening in detail:
            if opening.get("face") != op["face"] or opening["type"] not in ("window", "doorway"):
                continue
            box = transform.bounds(build_operation(opening, root).bounds())
            openings.append({"id": opening["element"], "from": box.min.to_list(),
                             "to": box.max.to_list(), "projection": _projection(box, origin, axis)})
        facades.append({"id": op["element"], "normal": face.to_list(),
                        "origin": origin.to_list(), "uAxis": axis.to_list(),
                        "vAxis": [0, 1, 0], "from": bounds.min.to_list(),
                        "to": bounds.max.to_list(), "openings": openings})
    if not layout:
        points = sorted({(op["from"][0], op["from"][2]) for op in frame_ops
                         if op["type"] == "fill" and op["from"][0] == op["to"][0]
                         and op["from"][2] == op["to"][2]},
                        key=lambda p: math.atan2(p[1], p[0]))
        if params["shape"] == "square":
            radius = params["radius"]
            points = sorted(set(points) | {(x, z) for x in (-radius, radius)
                                           for z in (-radius, radius)},
                            key=lambda p: math.atan2(p[1], p[0]))
        for index, start in enumerate(points):
            end = points[(index + 1) % len(points)]
            dx, dz = end[0] - start[0], end[1] - start[1]
            arc = []
            if params["shape"] == "round":
                start_angle = math.atan2(start[1], start[0])
                sweep = (math.atan2(end[1], end[0]) - start_angle) % math.tau
                arc = [p for p in disc_offsets(params["radius"], "hollow", "y")
                       if (math.atan2(p.z, p.x) - start_angle) % math.tau <= sweep]
                nx, nz = start[0] + end[0], start[1] + end[1]
                scale = max(1, math.gcd(abs(nx), abs(nz)))
                normal = Vec3(nx // scale, 0, nz // scale)
            elif dx:
                normal = Vec3(0, 0, 1 if (start[1] + end[1]) // 2 > 0 else -1)
            else:
                normal = Vec3(1 if (start[0] + end[0]) // 2 > 0 else -1, 0, 0)
            for level in range(params["floors"]):
                lo = Vec3(start[0], level * params["floor_height"] + 1, start[1])
                hi = Vec3(end[0], min((level + 1) * params["floor_height"],
                                      params["height"]), end[1])
                local_box = AABB.of(lo, hi)
                if arc:
                    local_box = AABB.from_points([
                        Vec3(p.x, y, p.z) for p in arc for y in (lo.y, hi.y)
                    ])
                box = transform.bounds(local_box)
                openings = []
                for opening in detail:
                    if opening["type"] not in ("window", "doorway"):
                        continue
                    opening_box = transform.bounds(build_operation(opening, root).bounds())
                    center = Vec3((opening_box.min.x + opening_box.max.x) // 2,
                                  (opening_box.min.y + opening_box.max.y) // 2,
                                  (opening_box.min.z + opening_box.max.z) // 2)
                    segment_center = transform.apply(Vec3((start[0] + end[0]) // 2,
                                                           lo.y, (start[1] + end[1]) // 2))
                    if (center.x - segment_center.x) ** 2 + (center.z - segment_center.z) ** 2 < 16:
                        origin = transform.apply(lo)
                        u_axis = rotation.apply(Vec3(dx, 0, dz))
                        openings.append({"id": opening["element"],
                                         "from": opening_box.min.to_list(),
                                         "to": opening_box.max.to_list(),
                                         "projection": _projection(opening_box, origin, u_axis)})
                facades.append({"id": f"{root}.facade.segment#{index + 1}.floor#{level + 1}",
                                "normal": rotation.apply(normal).to_list(),
                                "origin": transform.apply(lo).to_list(),
                                "uAxis": rotation.apply(Vec3(dx, 0, dz)).to_list(),
                                "vAxis": [0, 1, 0],
                                "from": box.min.to_list(), "to": box.max.to_list(),
                                "openings": openings})
    return {"columns": columns, "beams": beams, "facades": facades}


def _house(params, root):
    w, d, n, h = (params[k] for k in ("width", "depth", "floors", "floor_height"))
    building = build_operation({
        "type": "building", "position": [0, 0, 0], "width": w, "depth": d,
        "floors": n, "floorHeight": h, "wall": _ref("wall_main"),
        "floor": _ref("floor"), "roof": _ref("roof", "stairs"),
        "stairs": _ref("floor", "stairs"), "window": "glass", "door": "oak_door",
    }, root)
    body, detail = [], []
    for level, room in enumerate(building.expand()[:n]):
        for index, op in enumerate(build_operation(room, root).expand()):
            kind = op["type"]
            name = f"{root}.floor#{level + 1}.{index}"
            if kind in ("window", "doorway"):
                face = ("north" if op["position"][2] == 0 else
                        "east" if op.get("axis") == "z" else "south")
                detail.append(_tag(
                    op, f"{root}.opening#{level + 1}.{index}", "window_frame",
                    "features.floors", reserved="circulation" if kind == "doorway" else None,
                    face=face,
                ))
            elif kind == "wall":
                # 四面を分け、WeatherとCriticが面の向きを参照できるようにする。
                for face, start, end in (
                    ("north", [0, level * h + 1, 0], [w - 1, (level + 1) * h, 0]),
                    ("south", [0, level * h + 1, d - 1], [w - 1, (level + 1) * h, d - 1]),
                    ("west", [0, level * h + 1, 1], [0, (level + 1) * h, d - 2]),
                    ("east", [w - 1, level * h + 1, 1], [w - 1, (level + 1) * h, d - 2]),
                ):
                    body.append(_tag(
                        {"type": "fill", "from": start, "to": end,
                         "block": _ref("wall_main")},
                        f"{root}.facade.{face}.floor#{level + 1}", "wall_main",
                        "features.floors", face=face,
                    ))
            else:
                body.append(_tag(op, name, "floor", "features.floors"))
    if n == 2:
        body.append(_tag(building.expand()[n], f"{root}.stairs", "floor",
                         "features.floor_height", reserved="circulation"))
    roof = building.expand()[-1]
    if params["roof"] == "flat":
        roof = {"type": "fill", "from": [-1, n * h, -1], "to": [w, n * h, d],
                "block": _ref("roof")}
    else:
        roof["style"] = params["roof"]
    body.append(_tag(roof, f"{root}.roof", "roof", "features.roof"))
    arrivals = building.layout.arrivals
    for level, point in enumerate(arrivals):
        detail.append(_tag(
            {"type": "translate", "offset": [0, 0, 0], "operations": [
                {"type": "set", "position": (point - Vec3(0, 1, 0)).to_list(),
                 "block": _ref("floor")},
                {"type": "fill", "from": point.to_list(),
                 "to": (point + Vec3(0, 1, 0)).to_list(), "block": "air"},
            ]},
            f"{root}.stairs" if level else f"{root}.entry", "floor", "features.floors",
            reserved="circulation",
        ))
    frames = []
    xs, zs = _grid_positions(w, 4), _grid_positions(d, 4)
    perimeter = [(x, z) for x, z in ((x, 0) for x in xs)]
    perimeter += [(x, d - 1) for x in xs]
    perimeter += [(0, z) for z in zs[1:-1]]
    perimeter += [(w - 1, z) for z in zs[1:-1]]
    clear_ops = detail + [op for op in body if op.get("reserved") == "circulation"
                          or op["element"].endswith((".light#1", ".threshold"))]
    for x, z in perimeter:
        candidates = [(x, z)]
        if z == 0 and x not in (0, w - 1):
            candidates = [(x + dx, 1) for dx in (0, 1, -1, 2, -2)
                          if 0 < x + dx < w - 1]
        elif z == d - 1 and x not in (0, w - 1):
            candidates = [(x + dx, d - 2) for dx in (0, 1, -1, 2, -2)
                          if 0 < x + dx < w - 1]
        elif x == 0 and z not in (0, d - 1):
            candidates = [(-1, z)]
        elif x == w - 1 and z not in (0, d - 1):
            candidates = [(w, z)]
        point = next(((cx, cz) for cx, cz in candidates
                      if _column_clear(cx, cz, n * h, clear_ops, root)), None)
        if point:
            frames.append({"type": "fill", "from": [point[0], 0, point[1]],
                           "to": [point[0], n * h, point[1]],
                           "block": _ref("frame")})
    for y in range(h, n * h + 1, h):
        frames.append({"type": "wall", "from": [0, y, 0], "to": [w - 1, y, d - 1],
                       "height": 1, "block": _ref("frame")})
    body.append(_tag({"type": "translate", "offset": [0, 0, 0], "operations": frames},
                     f"{root}.frame", "frame", "style.grid", reserved="structure"))
    door_x = (w - 1) // 2
    body.append(_tag({"type": "floor", "from": [door_x, 0, -1],
                      "to": [door_x, 0, -1], "block": _ref("floor")},
                     f"{root}.threshold", "floor", "site"))
    return body, detail, building.layout


def _tower(params, root):
    r, n, h = (params[k] for k in ("radius", "floors", "floor_height"))
    height = params["height"]
    tower = build_operation({
        "type": "tower", "position": [0, 0, 0], "shape": params["shape"],
        **({"radius": r} if params["shape"] == "round" else {"size": 2 * r + 1}),
        "height": height, "floors": h, "wall": _ref("wall_main"),
        "floor": _ref("floor"), "stairs": False,
        "battlement": {"block": _ref("wall_main")} if params["battlement"] else False,
        "windows": {"height": 1, "sill": 2, "block": "glass"},
        "door": {"block": "oak_door"},
    }, root)
    body, detail = [], []
    for index, op in enumerate(tower.expand()):
        kind = op["type"]
        element = f"{root}.body#{index}"
        if kind in ("window", "doorway"):
            detail.append(_tag(
                op, f"{root}.opening#{index}", "window_frame", "features.floors",
                reserved="circulation" if kind == "doorway" else None,
            ))
        else:
            role = "wall_main"
            if kind in ("circle", "floor") and op.get("mode", "solid") == "solid":
                op["block"] = _ref("floor")
                role = "floor"
            body.append(_tag(op, element, role, "features.floors"))
    for level in range(n):
        body.append(_tag(
            {"type": "stairs", "start": [-1 if level % 2 == 0 else 1,
                                         level * h + 1, -r + 2],
             "direction": "south", "height": h, "headroom": 3,
             "block": _ref("floor", "stairs"), "base": _ref("floor")},
            f"{root}.stairs#{level + 1}", "floor", "features.floor_height",
            reserved="circulation",
        ))
        body[-1]["owner"] = f"{root}.stairs"
        detail.append(_tag(
            {"type": "set", "position": [-1 if level % 2 == 0 else 1,
                                         (level + 1) * h, -r + 2 + h],
             "block": _ref("floor")},
            f"{root}.stairs#{level + 1}.landing", "floor", "features.floor_height",
            reserved="circulation",
        ))
        detail[-1]["owner"] = f"{root}.stairs"
    points = [Vec3(0, 1, -r + 1)]
    points.extend(Vec3(0, y + 1, 0) for y in tower.storey_floors[1:])
    points.append(Vec3(0, tower.platform_y + 1, 0))
    for level, point in enumerate(points):
        detail.append(_tag(
            {"type": "translate", "offset": [0, 0, 0], "operations": [
                {"type": "set", "position": (point - Vec3(0, 1, 0)).to_list(),
                 "block": _ref("floor")},
                {"type": "fill", "from": point.to_list(),
                 "to": (point + Vec3(0, 1, 0)).to_list(), "block": "air"},
            ]},
            f"{root}.arrival#{level + 1}", "floor", "features.floors",
            reserved="circulation",
        ))
    if params["shape"] == "square":
        perimeter = [(x, z) for x in range(-r, r + 1, 4)
                     for z in (-r, r)] + [(x, z) for z in range(-r + 4, r, 4)
                                          for x in (-r, r)]
    else:
        perimeter = sorted([(p.x, p.z) for p in disc_offsets(r, "hollow", "y")],
                           key=lambda p: math.atan2(p[1], p[0]))
        count = max(4, math.ceil(len(perimeter) / 4))
        perimeter = [perimeter[i * len(perimeter) // count] for i in range(count)]
    corners = list(dict.fromkeys(perimeter))
    clear_ops = detail + [op for op in body if op.get("reserved") == "circulation"
                          or op["element"].endswith((".light#1", ".threshold"))]
    frame = []
    used = set()
    for x, z in corners:
        candidates = [(x, z)]
        if params["shape"] == "square":
            candidates.extend((x + dx, z) if z in (-r, r) else (x, z + dz)
                              for delta in (1, -1, 2, -2)
                              for dx, dz in ((delta, 0), (0, delta)))
        point = next((candidate for candidate in candidates
                      if candidate not in used
                      and (params["shape"] == "round"
                           or abs(candidate[0]) == r or abs(candidate[1]) == r)
                      and _column_clear(*candidate, height, clear_ops, root)), None)
        if point:
            used.add(point)
            frame.append({"type": "fill", "from": [point[0], 0, point[1]],
                          "to": [point[0], height, point[1]], "block": _ref("frame")})
    # 同じ円／角形の輪を梁として用い、開口の高さを避ける。
    for y in tower.storey_floors[1:] + [height]:
        frame.append(
            {"type": "circle", "center": [0, y, 0], "radius": r, "mode": "hollow",
             "block": _ref("frame")} if params["shape"] == "round" else
            {"type": "wall", "from": [-r, y, -r], "to": [r, y, r], "height": 1,
             "block": _ref("frame")}
        )
    body.append(_tag({"type": "translate", "offset": [0, 0, 0], "operations": frame},
                     f"{root}.frame", "frame", "style.grid", reserved="structure"))
    body.append(_tag({"type": "floor", "from": [0, 0, -r - 1], "to": [0, 0, -r],
                      "block": _ref("floor")}, f"{root}.threshold", "floor", "site"))
    return body, detail, Vec3(0, 1, -r), points


def plan_buildspec(resolved: ResolveResult) -> PlanningResult:
    """Resolve結果から住宅・塔の計画を作る。入力を変更せず、生成は実行しない。

    寸法不足・未知のelements/param・不正なoverrideはBlueprintError。
    予約へのoverrideは生成時にも検査される。snapshotは既存loader/foundationを
    通るBlueprintとして出力し、generate_regionsではなくgenerateで施工する。
    """
    if resolved.buildspec is None:
        raise BlueprintError("Site: BuildSpecが必要です")
    spec = copy.deepcopy(resolved.buildspec)
    root = f"{spec['archetype']}#1"
    style_path = Path(__file__).parent / "data" / "patterns" / "styles" / f"{spec['style']}.yaml"
    style = yaml.safe_load(style_path.read_text(encoding="utf-8"))
    features = spec["features"]
    h = features.get("floor_height", style["grid"]["story_height"][0])
    n = features.get("floors", 1 if spec["archetype"] == "house" else 3)
    bay = style["grid"]["bay"][0]
    if spec["archetype"] == "house":
        params = {"width": 2 * bay + 1, "depth": max(2 * bay + 3, h + 4),
                  "floors": n, "floor_height": h, "roof": features.get("roof", style["roofs"][0]),
                  "angle": 0}
    else:
        params = {"radius": max(4, math.ceil((h + 4) / 2)), "floors": n,
                  "floor_height": h, "height": n * h - 1,
                  "shape": features.get("shape", "round"),
                  "battlement": features.get("battlement", True), "angle": 0}
    edits = spec.get("elements", {})
    unknown = set(edits) - {root}
    if unknown:
        raise BlueprintError(f"Massing: elementsの未知の要素ID: {sorted(unknown)}")
    changes = edits.get(root, {})
    if set(changes) - params.keys():
        raise BlueprintError(f"Massing: elements.{root}: 未対応のパラメータ")
    params.update(changes)
    for key, value in params.items():
        if key in ("roof", "shape", "battlement"):
            continue
        if type(value) is not int or value < (0 if key == "angle" else 1):
            raise BlueprintError(f"Massing: elements.{root}.{key}: 整数の範囲外です")
    if params["angle"] not in (0, 90, 180, 270) or params["floor_height"] < 4:
        raise BlueprintError(f"Massing: elements.{root}: 回転または階高の範囲外です")
    if spec["archetype"] == "house":
        if params["roof"] not in ("gable", "hip", "flat"):
            raise BlueprintError("Massing: 未対応の屋根です")
        body, detail, layout = _house(params, root)
        entrance, arrivals = layout.entrance, list(layout.arrivals)
    else:
        if "height" in changes and "floor_height" not in changes:
            if (params["height"] + 1) % params["floors"]:
                raise BlueprintError("Massing: height + 1は階数で割り切れる必要があります")
            params["floor_height"] = (params["height"] + 1) // params["floors"]
        if "height" not in changes:
            params["height"] = params["floors"] * params["floor_height"] - 1
        if "radius" not in changes:
            params["radius"] = max(4, math.ceil((params["floor_height"] + 4) / 2))
        if (params["shape"] not in ("round", "square")
                or type(params["battlement"]) is not bool or params["radius"] < 4
                or params["floor_height"] < 4
                or params["floor_height"] > 2 * params["radius"] - 4
                or params["height"] != params["floors"] * params["floor_height"] - 1):
            raise BlueprintError("Massing: 塔の半径・形・高さ・階数が不整合です")
        body, detail, entrance, arrivals = _tower(params, root)
        layout = None
    local_contract = _plan_contract(root, body, detail, Transform.identity(), params, layout)
    projected_contract = _plan_contract(
        root, body, detail, Transform.rotation(params["angle"], Vec3(0, 0, 0)), params, layout,
    )
    decorations, detail_palettes = plan_detail(
        params, style, resolved.config, root, body, detail, local_contract, projected_contract,
    )
    if spec["interior"] == "minimal":
        # 歩行域の上ではなく、壁際の床面を発光させる。
        for level in range(params["floors"]):
            point = ([params["width"] - 2, level * params["floor_height"], 1]
                     if layout else [0, level * params["floor_height"], params["radius"] - 1])
            body.append(_tag({"type": "set", "position": point, "block": "sea_lantern"},
                             f"{root}.light#{level + 1}", "accent", "interior"))
    transform = Transform.rotation(params["angle"], Vec3(0, 0, 0))
    local = transform.bounds(_bounds(body + detail))
    size = spec["size"]
    if any(local.size[i] > size[i] for i in range(3)):
        raise BlueprintError(
            f"Massing: {root}: 屋根・胸壁・頭上を含む外形{local.size}がsize{size}に収まりません"
        )
    offset = Vec3((size[0] - local.size.x) // 2 - local.min.x, -local.min.y,
                  (size[2] - local.size.z) // 2 - local.min.z)
    terrain = spec["site"]["terrain"]
    terrain_json = {}
    floor_y = 0
    if terrain not in ("none", "flat"):
        snapshot = read_terrain(terrain)
        if snapshot.minecraft_version != spec["minecraftVersion"]:
            raise BlueprintError("Site: snapshotのMinecraft版が一致しません")
        region = snapshot.source_bounds
        if region.size.x < size[0] or region.size.z < size[2]:
            raise BlueprintError("Site: snapshotがsizeの敷地を含みません")
        offset = offset + Vec3(region.min.x + (region.size.x - size[0]) // 2, 0,
                               region.min.z + (region.size.z - size[2]) // 2)
        floor_y = max((snapshot.to_source(p).y for p in snapshot.blocks), default=region.min.y) + 1
        offset = offset + Vec3(0, floor_y, 0)
        footprint = transform.bounds(AABB(
            Vec3(0, 0, 0) if layout else Vec3(-params["radius"], 0, -params["radius"]),
            Vec3(params["width"] - 1, 0, params["depth"] - 1) if layout
            else Vec3(params["radius"], 0, params["radius"]),
        ))
        terrain_json = {
            "terrain": {"source": terrain, "region": {
                "from": region.min.to_list(), "to": region.max.to_list()}},
            "foundation": {"from": (footprint.min + offset).to_list(),
                           "to": (footprint.max + offset).to_list(),
                           "scan": {"minY": region.min.y, "maxY": region.max.y},
                           "maxCut": region.size.y, "maxFill": region.size.y,
                           "maxSupportDepth": region.size.y,
                           "ground": sorted(STABLE_GROUND),
                           "block": "minecraft:stone"},
        }
    transform = transform.then(Transform.translation(offset))
    contract = _plan_contract(root, body, detail, transform, params, layout)
    reverse = Transform.rotation(-params["angle"], Vec3(0, 0, 0))
    for item in decorations:
        item["operation"] = f"/{root}@:{transform.rows}:{transform.offset}/{item['operation']}"
        facade = next(f for f in contract["facades"] if f["id"] == item["element"])
        normal = reverse.apply(Vec3.from_seq(facade["normal"]))
        item["face"] = transform.apply_direction(max(NORMALS, key=lambda face: sum(
            a * b for a, b in zip(NORMALS[face], normal.to_list(), strict=True)
        )))
    contract["decorations"] = decorations
    entrance = transform.apply(entrance)
    arrivals = [transform.apply(p) for p in arrivals]
    wrapper = {"type": "translate", "offset": offset.to_list(),
               "rotation": params["angle"], "operations": body + detail,
               "id": root, "element": root, "source": "archetype"}
    bounds = _bounds([wrapper])
    scene_bounds = region if terrain_json else bounds
    overrides = []
    seen = {}
    for override in spec.get("overrides", []):
        ident = override.get("id")
        if (not isinstance(ident, str) or not ident
                or set(override) - {"id", "at", "region", "block"}):
            raise BlueprintError("Detail: overridesにはid・atまたはregion・blockが必要です")
        if ident in seen:
            if seen[ident] != override:
                raise BlueprintError(f"Detail: overridesのIDが競合します: {ident}")
            continue
        seen[ident] = override
        if (("at" in override) == ("region" in override)
                or not isinstance(override.get("block"), str)):
            raise BlueprintError(f"Detail: overrides.{ident}: atまたはregionとblockが必要です")
        points = [override["at"]] * 2 if "at" in override else override["region"]
        if (not isinstance(points, list) or len(points) != 2
                or any(not isinstance(p, list) or len(p) != 3
                       or any(type(v) is not int for v in p) for p in points)):
            raise BlueprintError(f"Detail: overrides.{ident}: 座標が不正です")
        box = AABB.of(*(Vec3.from_seq(p) + scene_bounds.min for p in points))
        if not scene_bounds.contains(box.min) or not scene_bounds.contains(box.max):
            raise BlueprintError(f"Detail: overrides.{ident}: sceneの範囲外です")
        overrides.append(_tag({"type": "fill", "from": box.min.to_list(), "to": box.max.to_list(),
                               "block": override["block"]}, f"override#{ident}", "override",
                              f"overrides.{ident}"))
    palette = copy.deepcopy(resolved.config["palette"])
    seed = resolved.config["seed"] if resolved.config["seed"] is not None else spec["seed"]
    blueprint = {"formatVersion": 1, "minecraftVersion": spec["minecraftVersion"],
                 "name": root, "seed": seed, "operations": [wrapper, *overrides],
                 "palettes": detail_palettes, **terrain_json,
                 "metadata": {"planning": {"element": root, "params": params,
                              "entrance": entrance.to_list(),
                              "arrivals": [p.to_list() for p in arrivals], **contract}}}
    if layout:
        layout = BuildingLayout(
            entrance, transform.apply_direction(layout.entry_direction), tuple(arrivals),
            tuple(BuildingFace(transform.apply_direction(face.direction),
                               transform.bounds(face.bounds)) for face in layout.faces),
            transform.bounds(build_operation(next(
                op for op in body if op["element"] == f"{root}.roof"
            ), root).bounds()), transform.apply_axis("z"),
        )
    site = {"formatVersion": 1, "stage": "site", "offset": offset.to_list(),
            "floorY": floor_y, "terrain": terrain}
    massing = {"formatVersion": 1, "stage": "massing", "element": root, "params": params,
               "entrance": entrance.to_list(), "arrivals": [p.to_list() for p in arrivals],
               "operations": body, **contract}
    detail_json = {"formatVersion": 1, "stage": "detail", "operations": detail,
                   "decorations": decorations, "overrides": overrides}
    return PlanningResult(site, massing, detail_json, blueprint, palette, layout, ())
