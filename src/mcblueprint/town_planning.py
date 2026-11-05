"""地形コストを使う街路と、接道区画・公共空間・複合棟を計画する。"""

from __future__ import annotations

import copy
import heapq
import math
import random
from pathlib import Path

import yaml

from mcblueprint.buildspec import SUPPORTED_TERRAINS
from mcblueprint.detail import plan_detail
from mcblueprint.errors import BlueprintError
from mcblueprint.facade_projection import project_facades
from mcblueprint.junctions import junction_geometry
from mcblueprint.layouts import load_layout
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.operations.base import ExecutionContext
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.transform import Transform
from mcblueprint.parcels import split_frontage
from mcblueprint.plan_contract import hierarchy, plan_buildings
from mcblueprint.planning import PlanningResult, _house, _plan_contract, _plan_overrides, _ref, _tag
from mcblueprint.site_planning import _place, connect_houses, grade_columns
from mcblueprint.surface_path import surface_geometry
from mcblueprint.terrain_density import plan_density
from mcblueprint.terrain_field import plan_terrain
from mcblueprint.volume import BlockVolume


def street_route(field, start, goal, area, blocked=frozenset(), *, width=1):
    """閉区間area内の乾いた列をA*で結ぶ。水域・blockedは全幅で避ける。

    勾配と造成量をコストにし、安定した座標順で同点を解決する。
    blockedは保護域・棟外形などのXZ集合。直交セル列を返し、行止まりはBlueprintError。
    """
    west, north, east, south = area
    radius = width // 2

    def free(x, z):
        return (west + radius <= x <= east - radius
                and north + radius <= z <= south - radius
                and all((px, pz) not in blocked
                        and field.heights[pz, px] >= field.water_level
                        for pz in range(z - radius, z + radius + 1)
                        for px in range(x - radius, x + radius + 1)))

    if not free(*start) or not free(*goal):
        raise BlueprintError("Site: 街路端点が水域・保護域・道路全幅と干渉します")
    distance, previous = {start: 0}, {start: None}
    queue = [(0, 0, start)]
    while queue:
        _, cost, point = heapq.heappop(queue)
        if cost != distance[point]:
            continue
        if point == goal:
            result = []
            while point is not None:
                result.append(point)
                point = previous[point]
            return result[::-1]
        x, z = point
        for dx, dz in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            target = nx, nz = x + dx, z + dz
            if not free(nx, nz):
                continue
            slope = abs(int(field.heights[nz, nx]) - int(field.heights[z, x]))
            candidate = cost + 1 + 8 * slope
            if candidate < distance.get(target, math.inf):
                distance[target], previous[target] = candidate, point
                heuristic = abs(nx - goal[0]) + abs(nz - goal[1])
                heapq.heappush(queue, (candidate + heuristic, candidate, target))
    raise BlueprintError("Site: 水域・保護域・棟を避ける街路が行止まりです")


def _corners(cells, floor):
    return [[x, floor, z] for i, (x, z) in enumerate(cells)
            if i in (0, len(cells) - 1)
            or (x - cells[i - 1][0], z - cells[i - 1][1])
            != (cells[i + 1][0] - x, cells[i + 1][1] - z)]


def _exterior_detail(plan, wrappers, config, style_name):
    style = yaml.safe_load((Path(__file__).parent / "data/patterns/styles" /
                            f"{style_name}.yaml").read_text(encoding="utf-8"))
    junctions = junction_geometry(plan)
    junction_reserved = {
        j.element: set(j.shared) | j.supports | set(j.passages) | set(j.roof_cells(
            ExecutionContext(BlockVolume(), random.Random(0), {})))
        for j in junctions}
    palettes, diagnostics = {}, []
    for building, wrapper in zip(plan_buildings(plan), wrappers, strict=True):
        root, params = building["element"], building["params"]
        body, detail, layout = _house(params, root)
        contract = _plan_contract(root, body, detail, Transform.identity(), params, layout)
        projected = copy.deepcopy(contract)
        offset = Vec3.from_seq(building["offset"])
        facades = project_facades({"element": root, "params": params, **contract})
        for facade, raw in zip(facades, projected["facades"], strict=True):
            for cell in facade.cells:
                if any(j.internal_face(cell.pos + offset, facade.face, root) for j in junctions):
                    raw["openings"].append({"from": cell.pos.to_list(), "to": cell.pos.to_list()})
        blocked = {point - offset for j in junctions
                   if any(part.element == root for part in j.parts)
                   for point in junction_reserved[j.element]}
        if any(any(part.element == root for part in j.parts) for j in junctions):
            box = build_operation(wrapper, "town.detail").bounds()
            blocked.update(Vec3(x - offset.x, y - offset.y, z - offset.z)
                           for y in range(offset.y + params["floors"] * params["floor_height"],
                                          box.max.y + 1)
                           for z in range(box.min.z, box.max.z + 1)
                           for x in range(box.min.x, box.max.x + 1))
        decorations, p, d = plan_detail(params, style, config, root, body, detail,
                                         contract, projected, blocked=blocked)
        body.extend(op for op in building["operations"] if ".light#" in op["element"])
        for operation in [*body, *detail]:
            if operation["element"].endswith(".threshold"):
                operation.update(reserved="circulation", owner="site#1.paths")
        transform = Transform.translation(offset)
        building.update(_plan_contract(root, body, detail, transform, params, layout),
                        operations=body, decorations=decorations)
        for decoration in decorations:
            decoration["operation"] = (f"/{root}@:{transform.rows}:{offset}/"
                                       + decoration["operation"])
        wrapper["operations"] = body + detail
        palettes.update(p)
        diagnostics.extend(d)
    return palettes, diagnostics


def plan_town(resolved) -> PlanningResult:
    """town/cityの内部計画v2を返す。要求建物数は区画内の建物群数。

    medievalの生成地形を対象に、街区ごとの共通床を造成する。L/T型は
    複数の矩形棟で表す。収容不足・水域干渉・未知の編集IDはBlueprintError。
    外部BuildSpec/scene/editsの版は変更しない。
    """
    spec, config = resolved.buildspec, resolved.config
    layout = load_layout(f"{spec['style']}.town")
    rules, size, features = layout["rules"], spec["size"], spec["features"]
    terrain = spec["site"]["terrain"]
    if terrain not in SUPPORTED_TERRAINS[:-1]:
        raise BlueprintError("Site: town/cityは生成地形またはnone/flatを指定してください")
    bounds = AABB(Vec3(0, 0, 0), Vec3(*(v - 1 for v in size)))
    field = plan_terrain(bounds, "flat" if terrain == "none" else terrain,
                         config["terrain"], spec["seed"], clearance=24)
    count = features["buildings"]
    width = rules["roadWidth"][spec["seed"] % 2]
    depth = min(rules["plotDepth"][1], (size[2] - 16 - width) // 2)
    columns = (count + 1) // 2
    plot_width = min(rules["plotWidth"][1], (size[0] - 16) // columns)
    if plot_width < rules["plotWidth"][0] or depth < rules["plotDepth"][0]:
        raise BlueprintError("Site: 要求された建物数・道路・区画がsizeに収まりません")
    west = (size[0] - columns * plot_width - 12) // 2
    middle = west + (columns // 2) * plot_width + 6
    north = max(width + 3, (size[2] - (2 * depth + 12 + width)) // 2 + 6)
    rows = [north, north + depth + width + 6]
    plots = []
    for row in rows:
        for x, number in ((west, columns // 2), (middle + 6, columns - columns // 2)):
            if number:
                plots.extend(split_frontage(x, row, x + number * plot_width - 1,
                                            row + depth - 1, [plot_width, plot_width],
                                            rules["plotDepth"]))
    plots = plots[:count]
    active = (west - 2, north - width - 3,
              west + columns * plot_width + 13, rows[-1] + depth)
    if active[0] < 0 or active[1] < 0 or active[2] >= size[0] or active[3] >= size[2]:
        raise BlueprintError("Site: 道路・区画の造成範囲がsizeに収まりません")
    floor = int(field.heights[active[1]:active[3] + 1,
                              active[0]:active[2] + 1].max()) + 1
    if floor + 20 >= size[1]:
        raise BlueprintError("Site: 地形基礎と街並みの頭上がsizeに収まりません")
    if int(field.heights[active[1]:active[3] + 1,
                          active[0]:active[2] + 1].min()) < field.water_level:
        # 水域のある区画を盛土で黙って埋めない。
        for a, b, c, d in plots:
            if int(field.heights[b:d + 1, a:c + 1].min()) < field.water_level:
                raise BlueprintError("Site: 区画が水域と干渉します")
    buildings, wrappers, groups, palettes, diagnostics = [], [], [], {}, []
    edits = spec.get("elements", {})
    for index, (x, z, east, south) in enumerate(plots):
        shape = ("L" if index % 3 == 0 else "T" if index % 3 == 1 else "rectangle")
        if east - x < 20 or south - z < 20:
            shape = "rectangle"
        params = [(x + 2, z + 2, 9, 17 if shape == "T" else 13)]
        if shape != "rectangle":
            params.append((x + 10, z + 6, 9, 9))
        members = []
        for px, pz, bw, bd in params:
            root = f"house#{len(buildings) + 1}"
            b, wrapper, p, d = _place(resolved, "house", root, [px, floor, pz], 0,
                {"floors": 1 + (index + spec["seed"]) % 2,
                 "floor_height": 4, "roof": "gable"},
                {"width": bw, "depth": bd, **edits.get(root, {})})
            box = build_operation(wrapper, "town.parcel").bounds()
            if (box.min.x < x or box.max.x > east or box.min.z < z or box.max.z > south
                    or not bounds.contains(box.min) or not bounds.contains(box.max)):
                raise BlueprintError("Site: 棟の外形が区画またはsizeに収まりません")
            b["use"] = ("dwelling", "workshop", "shop")[index % 3]
            buildings.append(b)
            members.append(b)
            wrappers.append(wrapper)
            palettes.update(p)
            diagnostics.extend(d)
        groups.append({"element": f"parcel#{index + 1}", "bounds": [[x, z], [east, south]],
                       "groups": [{"element": f"group#{index + 1}", "shape": shape,
                                   "buildings": members}]})
    unknown = set(edits) - {b["element"] for b in buildings}
    if unknown:
        raise BlueprintError(f"Massing: elementsの未知の要素ID: {sorted(unknown)}")
    plan = hierarchy(spec["archetype"], buildings)
    plan["parcels"] = groups
    for index, parcel in enumerate(groups):
        members = parcel["groups"][0]["buildings"]
        if len(members) > 1:
            connect_houses(plan, members, f"junction#{index + 1}")
    palettes, diagnostics = _exterior_detail(plan, wrappers, config, spec["style"])
    nodes, edges = plan["connections"]["nodes"], plan["connections"]["edges"]
    operations = [_tag({"type": "terrain_density", "plan": plan_density(
        field, overhangs=False).to_dict(), "minecraftVersion": spec["minecraftVersion"]},
        "terrain#1", "ground", "site.terrain")]
    road_cells, roads, graded = set(), [], set()

    def route(cells, road_width, ident):
        points = _corners(cells, floor)
        if len(points) < 2:
            raise BlueprintError("Site: 街路の長さが不足しています")
        geometry = surface_geometry([Vec3.from_seq(p) for p in points], road_width,
                                    BlockState.of("stone_bricks"),
                                    BlockState.of("stone_brick_stairs"))
        footprint = {(op["position"][0], op["position"][2]) for op in geometry.expand()
                     if op["type"] == "set"}
        graded.update(footprint)
        op = _tag({"type": "path", "points": points, "width": road_width,
                   "block": _ref("path"), "clearance": 2}, ident, "path", "site",
                  reserved="circulation")
        op["owner"] = "site#1.paths"
        roads.append(op)
        return footprint

    pz = rows[0] + depth // 2
    public_blocked = set()
    if features["well"]:
        public_blocked.update((x, z) for x in range(middle + 4, middle + 7)
                              for z in range(pz - 1, pz + 2))
    if features["market"]:
        public_blocked.update((middle - 5, z) for z in range(pz - 2, pz + 3))
    footprints = {b["element"]: build_operation(w, "town.approach").bounds()
                  for b, w in zip(buildings, wrappers, strict=True)}
    occupied = {(x, z) for box in footprints.values()
                for z in range(box.min.z, box.max.z + 1)
                for x in range(box.min.x, box.max.x + 1)}
    highways = []
    for row in rows:
        rz = row - width // 2 - 2
        area = (active[0], rz - width, active[2], rz + width)
        cells = street_route(field, (active[0] + width, rz),
                             (active[2] - width, rz), area, occupied, width=width)
        highways.append(cells)
        road_cells.update(route(cells, width, f"site#1.road#{len(highways)}"))
    a = min(highways[0], key=lambda p: abs(p[0] - middle))
    b = min(highways[1], key=lambda p: abs(p[0] - middle))
    radius = width // 2
    link_area = (middle - 5, min(active[1], a[1] - radius, b[1] - radius),
                 middle + 5, max(active[3], a[1] + radius, b[1] + radius))
    link = street_route(field, a, b, link_area, occupied | public_blocked, width=width)
    road_cells.update(route(link, width, "site#1.road#3"))
    origin = highways[0][0]
    nodes.append({"id": "site#1.entry", "element": "site#1",
                  "position": [origin[0], floor + 1, origin[1]]})
    if road_cells & occupied:
        raise BlueprintError("Site: 道路上に建物が重なっています")
    for building in buildings:
        root = building["element"]
        x, _, z = building["entrance"]
        endpoint = (x, z - 1)
        box = footprints[root]
        terminal = {(x, pz) for pz in range(box.min.z, z)}
        start = min(road_cells, key=lambda p: (abs(x - p[0]) + abs(z - p[1]), p))
        cells = street_route(field, start, endpoint, active,
                             (occupied - terminal) | public_blocked)
        route(cells, 1, f"site#1.{root}.approach")
        ident = f"site#1.{root}.road"
        nodes.append({"id": ident, "element": "site#1",
                      "position": [start[0], floor + 1, start[1]]})
        edges.extend([{"id": ident, "kind": "road", "from": "site#1.entry", "to": ident},
                      {"id": root + ".approach", "kind": "walk", "from": ident,
                       "to": root + ".walk#1"}])
    for x, z, east, south in plots:
        graded.update((px, pz) for pz in range(z, south + 1) for px in range(x, east + 1))
    public = []
    if features["plaza"]:
        graded.update((x, z) for z in range(pz - 5, pz + 6)
                      for x in range(middle - 5, middle + 6))
        public.append(_tag({"type": "fill", "from": [middle - 5, floor, pz - 5],
                            "to": [middle + 5, floor, pz + 5], "block": _ref("path")},
                           "plaza#1", "path", "features.plaza"))
    if features["well"]:
        x = middle + 5
        graded.update((px, z) for px in range(x - 1, x + 2) for z in range(pz - 1, pz + 2))
        for dx, dz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            public.append(_tag({"type": "set", "position": [x + dx, floor + 1, pz + dz],
                                "block": _ref("foundation")},
                               "well#1", "foundation", "features.well"))
        public.append(_tag({"type": "set", "position": [x, floor + 1, pz], "block": "water"},
                           "well#1.water", "ground", "features.well"))
    if features["market"]:
        x = middle - 5
        graded.update((px, z) for px in range(x - 1, x + 2) for z in range(pz - 2, pz + 3))
        public.append(_tag({"type": "fill", "from": [x, floor + 1, pz - 2],
                            "to": [x, floor + 1, pz + 2], "block": _ref("floor")},
                           "market#1", "floor", "features.market"))
    if any(field.heights[z, x] < field.water_level for x, z in graded):
        raise BlueprintError("Site: 造成範囲に水域が含まれています")
    operations.extend(grade_columns(field, sorted(graded, key=lambda p: (p[1], p[0])),
                                    floor, clearance=20))
    operations.extend(public)
    operations.extend(roads)
    operations.extend(wrappers)
    operations.extend(_plan_overrides(spec, bounds))
    plan_buildings(plan)
    blueprint = {"formatVersion": 1, "minecraftVersion": spec["minecraftVersion"],
                 "name": spec["archetype"], "seed": spec["seed"], "operations": operations,
                 "palettes": palettes, "metadata": {"planning": copy.deepcopy(plan)}}
    site = {**copy.deepcopy(plan), "stage": "site", "offset": [0, 0, 0], "floorY": floor,
            "terrain": terrain, "groundHeight": {"type": "heightfield", "field": field.to_dict()},
            "layout": layout["name"], "layoutRules": rules}
    return PlanningResult(site, {**copy.deepcopy(plan), "stage": "massing"},
        {**copy.deepcopy(plan), "stage": "detail", "diagnostics": diagnostics},
        blueprint, copy.deepcopy(config["palette"]), None, tuple(diagnostics))
