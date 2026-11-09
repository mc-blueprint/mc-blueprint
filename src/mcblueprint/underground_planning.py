"""主空洞の床・天井から地下の区画と入口経路を計画する。"""

import copy
import math
from dataclasses import replace

import numpy as np

from mcblueprint.errors import BlueprintError
from mcblueprint.layouts import load_layout
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.registry import build_operation
from mcblueprint.parcels import split_frontage
from mcblueprint.plan_contract import hierarchy, plan_buildings
from mcblueprint.planning import PlanningResult, _plan_overrides, _ref, _tag
from mcblueprint.site_planning import _place
from mcblueprint.terrain_density import CaveNode, plan_density
from mcblueprint.terrain_field import plan_terrain
from mcblueprint.town_planning import _corners, street_route


def cave_surfaces(density, *, margin=0):
    """主空洞の最下air・最上airをbounds起点の[z,x]配列で返す。

    densityはplan_density(main_cave=True)の結果。同じ中心高の楕円体の和を
    2Dで評価する。margin=2では周囲の岩盤も含む。空洞外はY範囲の直上/直下とし、
    枝洞は床・天井に含めない。
    """
    bounds = density.field.bounds
    x, z = np.meshgrid(np.arange(bounds.min.x, bounds.max.x + 1),
                       np.arange(bounds.min.z, bounds.max.z + 1))
    bottom = np.full(x.shape, bounds.max.y + 1, dtype=np.int32)
    ceiling = np.full(x.shape, bounds.min.y - 1, dtype=np.int32)
    for node in density.nodes:
        c = node.center
        r = node.radius + Vec3(margin, margin, margin)
        horizontal = ((x - c.x) / r.x) ** 2 + ((z - c.z) / r.z) ** 2
        inside = horizontal <= 1
        height = r.y * np.sqrt(np.maximum(0, 1 - horizontal))
        bottom[inside] = np.minimum(bottom[inside], np.ceil(c.y - height[inside]).astype(int))
        ceiling[inside] = np.maximum(ceiling[inside], np.floor(c.y + height[inside]).astype(int))
    return bottom, ceiling


def _houses(resolved, plots, floor, bounds):
    buildings, wrappers, parcels, palettes, diagnostics = [], [], [], {}, []
    edits = resolved.buildspec.get("elements", {})
    unknown = set(edits) - {f"house#{index + 1}" for index in range(len(plots))}
    if unknown:
        raise BlueprintError(f"Site: elementsの未知の要素ID: {sorted(unknown)}")
    for index, (x, z, east, south) in enumerate(plots):
        root = f"house#{index + 1}"
        building, wrapper, p, d = _place(resolved, "house", root, [x + 1, floor, z + 1],
            0, {"floors": 1, "floor_height": 4, "roof": "gable"},
            {"width": 9, "depth": 9, **edits.get(root, {})})
        box = build_operation(wrapper, "underground.parcel").bounds()
        if (box.min.x < x or box.max.x > east or box.min.z < z or box.max.z > south
                or box.min.x < bounds.min.x or box.max.x > bounds.max.x
                or box.min.z < bounds.min.z or box.max.z > bounds.max.z):
            raise BlueprintError("Site: 地下の棟外形が区画またはsizeに収まりません")
        buildings.append(building)
        wrappers.append(wrapper)
        palettes.update(p)
        diagnostics.extend(d)
        parcels.append({"element": f"parcel#{index + 1}", "bounds": [[x, z], [east, south]],
                        "groups": [{"element": f"group#{index + 1}",
                                    "buildings": [building]}]})
    return buildings, wrappers, parcels, palettes, diagnostics


def _floor(density, bottom, ceiling, tops, rules, plots, buildings, footprints, width, ends):
    bounds = density.field.bounds
    main = density.nodes[0]
    hub = main.center.x, main.center.z
    headroom = rules["floorClearance"][0]
    low = max(bounds.min.y, *(int(bottom[n.center.z, n.center.x]) - 1 for n in ends))
    high = min(int(ceiling[hub[1], hub[0]]) - headroom,
               *(int(ceiling[n.center.z, n.center.x]) - headroom for n in ends))
    for x, z, east, south in plots:
        low = max(low, int(bottom[z:south + 1, x:east + 1].max()) - 1)
        high = min(high, int(ceiling[z:south + 1, x:east + 1].min()) - headroom)
    for box in footprints.values():
        low = max(low, bounds.min.y - box.min.y)
        high = min(high, int(ceiling[box.min.z:box.max.z + 1,
                                     box.min.x:box.max.x + 1].min())
                   - box.max.y - rules["ceilingClearance"][0])
    if density.water_level is not None:
        if density.water_level > high:
            raise BlueprintError("Site: 地下床・入口経路が水没します")
        low = max(low, density.water_level)
    if low > high:
        raise BlueprintError("Site: 地下区画の床・屋根が主空洞の床・天井余裕と干渉します")
    occupied = {(x, z) for box in footprints.values()
                for z in range(box.min.z, box.max.z + 1)
                for x in range(box.min.x, box.max.x + 1)}
    area = (0, 0, bounds.max.x, bounds.max.z)
    preferred = main.center.y - headroom
    for floor in sorted(range(low, high + 1), key=lambda y: (abs(y - preferred), y)):
        field = replace(density.field, heights=np.maximum(bottom - 1, floor),
                        water_level=density.water_level if density.water_level is not None else 0)
        blocked = {(x, z) for z, x in np.argwhere(
            (bottom > floor + 1) | (ceiling < floor + headroom))}
        try:
            for node in ends:
                street_route(field, (node.center.x, node.center.z), hub, area, blocked | occupied)
                _surface_access(density, tops, (node.center.x, node.center.z), floor,
                                headroom, node.id)
            if plots:
                columns = (len(plots) + 1) // 2
                street_route(field, (plots[0][0] - 2, hub[1]),
                             (plots[columns - 1][2] + 2, hub[1]), area,
                             blocked | occupied, width=width)
            for building in buildings:
                x, _, z = building["entrance"]
                box = footprints[building["element"]]
                terminal = {(x, pz) for pz in range(box.min.z, z)}
                street_route(field, hub, (x, z - 1), area, blocked | (occupied - terminal))
        except BlueprintError:
            continue
        return floor
    raise BlueprintError("Site: 主空洞の床・頭上余裕を満たす入口経路が行止まりです")


def _surface_access(density, tops, start, floor, headroom, ident):
    """生成範囲内の乾いた地表まで、両端の踊り場を持つ階段を計画する。"""
    bounds = density.field.bounds
    dx, dz = (0, 1) if start[0] in (bounds.min.x, bounds.max.x) else (1, 0)
    candidates = []
    for sign in (-1, 1):
        for distance in range(5, max(bounds.size.x, bounds.size.z)):
            x, z = start[0] + sign * distance * dx, start[1] + sign * distance * dz
            if not (bounds.min.x <= x <= bounds.max.x and bounds.min.z <= z <= bounds.max.z):
                break
            top = int(tops[z, x])
            if (abs(top - floor) > distance - 4 or top + headroom > bounds.max.y
                    or density.water_level is not None and top < density.water_level):
                continue
            candidates.append((top != int(density.field.heights[z, x]), distance, x, z, top))
    if not candidates:
        raise BlueprintError("Site: 生成範囲内の乾いた地表へ入口階段を接続できません")
    _, distance, x, z, top = min(candidates)
    sx, sz = (x - start[0]) // distance, (z - start[1]) // distance
    points = [[start[0], floor, start[1]],
              [start[0] + 2 * sx, floor, start[1] + 2 * sz],
              [x - 2 * sx, top, z - 2 * sz], [x, top, z]]
    operation = _tag({"type": "path", "points": points, "width": 1,
                      "block": _ref("path"), "stairs": "minecraft:stone_brick_stairs",
                      "clearance": headroom}, ident + ".surface", "path", "site",
                     reserved="circulation")
    operation["owner"] = "site#1.paths"
    path = build_operation(operation, "underground.surface")
    columns = {(p.x, p.z): p.y for p, _ in path.centre_line()}
    return operation, columns, [x, top + 1, z]


def _city_space(field, plots, buildings, footprints, rules, width, entrances):
    """区画と実外形を内包する空洞群を、殻厚2内の共通床候補から求める。"""
    bounds = field.bounds
    center = Vec3(*((bounds.min[i] + bounds.max[i] + 1) // 2 for i in range(3)))
    radius = Vec3(*(min(center[i] - bounds.min[i], bounds.max[i] - center[i]) - 2
                    for i in range(3)))
    central = CaveNode("cave#0", center, radius)
    headroom, clearance = rules["floorClearance"][0], rules["ceilingClearance"][0]
    failure = BlueprintError("Site: 要求された地下区画の床・天井余裕がsizeの主空洞に収まりません")
    candidates = range(bounds.min.y + 3, bounds.max.y - headroom - 1)
    for floor in sorted(candidates, key=lambda y: (abs(y - (center.y - headroom)), y)):
        chambers = [central]
        for index, ((x, z, east, south), box) in enumerate(
                zip(plots, footprints.values(), strict=True)):
            cx, cz = (x + east) // 2, (z + south) // 2
            dx = max(cx - x, east - cx, cx - box.min.x, box.max.x - cx)
            dz = max(cz - z, south - cz, cz - box.min.z, box.max.z - cz)
            max_rx = min(cx - bounds.min.x - 2, bounds.max.x - cx - 2)
            max_rz = min(cz - bounds.min.z - 2, bounds.max.z - cz - 2)
            rx = min(math.ceil(math.sqrt(3) * dx), max_rx)
            rz = min(math.ceil(math.sqrt(3) * dz), max_rz)
            if rx <= dx or rz <= dz:
                break
            remaining = 1 - (dx / rx) ** 2 - (dz / rz) ** 2
            dy = max(abs(floor + 1 - center.y),
                     abs(floor + max(headroom, box.max.y + clearance) - center.y))
            if remaining <= 0 or dy ** 2 > radius.y ** 2 * remaining:
                rx, rz = max_rx, max_rz
                remaining = 1 - (dx / rx) ** 2 - (dz / rz) ** 2
            if remaining <= 0:
                break
            ry = max(1, math.ceil(dy / math.sqrt(remaining)))
            if ry > radius.y:
                break
            chambers.append(CaveNode(f"cave.chamber#{index + 1}", Vec3(cx, center.y, cz),
                                     Vec3(rx, ry, rz)))
        if len(chambers) != len(plots) + 1:
            continue
        density = plan_density(field, main_cave=True, overhangs=False,
                               entrances=entrances, chambers=tuple(chambers))
        bottom, ceiling = cave_surfaces(density)
        tops = np.maximum(field.heights, cave_surfaces(density, margin=2)[1])
        ends = [n for n in density.nodes if n.id.startswith("cave.entrance#")
                and (n.center.x in (bounds.min.x, bounds.max.x)
                     or n.center.z in (bounds.min.z, bounds.max.z))]
        try:
            selected = _floor(density, bottom, ceiling, tops, rules, plots, buildings,
                              footprints, width, ends)
        except BlueprintError as exc:
            failure = exc
            continue
        return density, bottom, ceiling, tops, ends, selected
    raise failure


def plan_underground(resolved) -> PlanningResult:
    """cave/underground_cityの内部計画v2を返す。入力は変更しない。

    同梱layoutに従う乾いた共通床を主空洞内に設け、入口から各棟まで予約通路を
    接続する。天井不足・水没・区画不足・入口閉塞・未知の編集はBlueprintError。
    地下の床基準場をWeatherへ渡し、地表や屋根の高さは使わない。
    """
    spec, config = resolved.buildspec, resolved.config
    if spec["site"]["terrain"] not in ("none", "flat", "cave"):
        raise BlueprintError("Site: 地下計画はnone/flat/caveの生成地形を指定してください")
    layout = load_layout(f"{spec['style']}.underground_city")
    rules, features, size = layout["rules"], spec["features"], spec["size"]
    bounds = AABB(Vec3(0, 0, 0), Vec3(*(v - 1 for v in size)))
    field = plan_terrain(bounds, "flat", config["terrain"], spec["seed"])
    density = plan_density(field, main_cave=True, overhangs=False,
                           entrances=features["entrances"])
    bottom, ceiling = cave_surfaces(density)
    main = density.nodes[0]
    headroom = rules["floorClearance"][0]
    area = (0, 0, size[0] - 1, size[2] - 1)
    width = rules["roadWidth"][spec["seed"] % 2]
    plots = []
    if spec["archetype"] == "underground_city":
        count = features["buildings"]
        plot_width = rules["plotWidth"][0]
        depth = rules["plotDepth"][0]
        columns = math.ceil(count / 2)
        west = main.center.x - columns * plot_width // 2
        rows = [main.center.z - width // 2 - depth - 1, main.center.z + width // 2 + 2]
        for row in rows:
            plots.extend(split_frontage(west, row, west + columns * plot_width - 1,
                                        row + depth - 1, [plot_width, plot_width],
                                        rules["plotDepth"]))
        plots = plots[:count]
    probes = _houses(resolved, plots, 0, bounds)
    footprints = {b["element"]: build_operation(w, "underground.approach").bounds()
                  for b, w in zip(probes[0], probes[1], strict=True)}
    ends = [n for n in density.nodes if n.center.x in (0, size[0] - 1)
            or n.center.z in (0, size[2] - 1)]
    tops = np.maximum(field.heights, cave_surfaces(density, margin=2)[1])
    try:
        floor = _floor(density, bottom, ceiling, tops, rules, plots, probes[0], footprints,
                       width, ends)
    except BlueprintError:
        if not plots:
            raise
        density, bottom, ceiling, tops, ends, floor = _city_space(
            field, plots, probes[0], footprints, rules, width, features["entrances"])
        main = density.nodes[0]
    buildings, wrappers, parcels, palettes, diagnostics = _houses(resolved, plots, floor, bounds)
    # 地表場は密度計画に保持し、地下床の場は独立して保存する。
    ground = replace(field, heights=np.maximum(bottom - 1, floor),
                     water_level=density.water_level if density.water_level is not None else 0)
    usable = (bottom <= floor + 1) & (ceiling >= floor + headroom)
    blocked = {(x, z) for z, x in np.argwhere(~usable)}
    plan = hierarchy(spec["archetype"], buildings)
    plan["parcels"] = parcels
    operations = [_tag({"type": "terrain_density", "plan": density.to_dict(),
                        "minecraftVersion": spec["minecraftVersion"]},
                       "terrain#1", "ground", "site.terrain")]
    nodes, edges = plan["connections"]["nodes"], plan["connections"]["edges"]
    occupied = {(x, z) for box in footprints.values()
                for z in range(box.min.z, box.max.z + 1)
                for x in range(box.min.x, box.max.x + 1)}
    graded, roads, road_cells, surface_columns = set(), [], set(), {}

    def route(cells, road_width, ident, *, clearance=headroom):
        points = _corners(cells, floor)
        if len(points) < 2:
            return set()
        op = _tag({"type": "path", "points": points, "width": road_width,
                   "block": _ref("path"), "clearance": clearance}, ident, "path", "site",
                  reserved="circulation")
        op["owner"] = "site#1.paths"
        radius = road_width // 2
        cells = {(x + dx, z + dz) for x, z in cells for dx in range(-radius, radius + 1)
                 for dz in range(-radius, radius + 1)}
        graded.update(cells)
        roads.append(op)
        return cells

    hub = main.center.x, main.center.z
    nodes.append({"id": "site#1.hub", "element": "site#1",
                  "position": [hub[0], floor + 1, hub[1]]})
    if buildings:
        cells = street_route(ground, (plots[0][0] - 2, hub[1]),
                             (plots[columns - 1][2] + 2, hub[1]), area,
                             blocked | occupied, width=width)
        road_cells.update(route(cells, width, "site#1.street"))
    for index, node in enumerate(ends):
        start = node.center.x, node.center.z
        cells = street_route(ground, start, hub, area, blocked | occupied)
        road_cells.update(route(cells, 1, f"site#1.entrance#{index + 1}"))
        ident = "site#1.entry" if index == 0 else f"site#1.entry#{index + 1}"
        nodes.append({"id": ident, "element": "site#1",
                      "position": [start[0], floor + 1, start[1]]})
        edges.append({"id": ident + ".route", "kind": "cave", "from": ident,
                      "to": "site#1.hub"})
        access, columns_map, surface = _surface_access(density, tops, start, floor,
                                                       headroom, ident)
        roads.append(access)
        surface_columns.update(columns_map)
        nodes.append({"id": ident + ".surface", "element": "site#1", "position": surface})
        edges.append({"id": ident + ".access", "kind": "walk", "from": ident + ".surface",
                      "to": ident})
    for building in buildings:
        root = building["element"]
        x, _, z = building["entrance"]
        box = footprints[root]
        terminal = {(x, pz) for pz in range(box.min.z, z)}
        endpoint = (x, z - 1)
        start = min(road_cells, key=lambda p: (abs(p[0] - x) + abs(p[1] - z), p))
        cells = street_route(ground, start, endpoint, area, blocked | (occupied - terminal))
        terminal_start = next(i for i, point in enumerate(cells) if point in terminal)
        route(cells[:terminal_start], 1, f"site#1.{root}.approach")
        route(cells[max(0, terminal_start - 1):], 1,
              f"site#1.{root}.threshold", clearance=2)
        ident = f"site#1.{root}.road"
        nodes.append({"id": ident, "element": "site#1",
                      "position": [start[0], floor + 1, start[1]]})
        edges.extend([{"id": ident, "kind": "road", "from": "site#1.hub", "to": ident},
                      {"id": root + ".approach", "kind": "walk", "from": ident,
                       "to": root + ".walk#1"}])
    for x, z, east, south in plots:
        graded.update((px, pz) for pz in range(z, south + 1) for px in range(x, east + 1))
    for x, z in sorted(graded, key=lambda p: (p[1], p[0])):
        low = int(bottom[z, x])
        if low > floor + 1 or ceiling[z, x] < floor + headroom:
            raise BlueprintError("Site: 地下の造成・入口経路が主空洞の床・天井と干渉します")
        if low <= floor:
            operations.append(_tag({"type": "fill", "from": [x, low, z],
                                    "to": [x, floor, z], "block": _ref("foundation")},
                                   "foundation#1", "foundation", "site.terrain"))
    for (x, z), top in sorted(surface_columns.items(), key=lambda item: item[0][::-1]):
        low = min(int(field.heights[z, x]) + 1, int(bottom[z, x]))
        if low <= top:
            operations.append(_tag({"type": "fill", "from": [x, low, z],
                                    "to": [x, top, z], "block": _ref("foundation")},
                                   "foundation#1", "foundation", "site.terrain"))
    supports = []
    occupied_paths = graded | surface_columns.keys()
    free = {(int(x), int(z)) for z, x in np.argwhere(usable)
            if (x, z) not in occupied_paths and (x, z) not in occupied
            and (not features.get("lights", True) or x + 1 < size[0]
                 and usable[z, x + 1] and (x + 1, z) not in occupied_paths
                 and (x + 1, z) not in occupied)}
    targets = (-1, 1) if features.get("supports", True) or features.get("lights", True) else ()
    for index, sign in enumerate(targets):
        target = (hub[0] + sign * (main.radius.x // 2),
                  hub[1] + sign * (main.radius.z // 2))
        if not free:
            raise BlueprintError("Site: 区画・経路を避ける支柱・光源を主空洞に収容できません")
        x, z = min(free, key=lambda p: (abs(p[0] - target[0]) + abs(p[1] - target[1]), p))
        free.difference_update(((x - 1, z), (x, z), (x + 1, z)))
        low, high = int(bottom[z, x]), int(ceiling[z, x])
        if features.get("supports", True):
            ident = f"site#1.support#{index + 1}"
            support = _tag({"type": "fill", "from": [x, low, z], "to": [x, high, z],
                            "block": _ref("frame")}, ident, "frame",
                           "features.supports" if buildings else "site",
                           reserved="structure")
            operations.append(support)
            for suffix, y in (("footing", low - 1), ("bearing", high + 1)):
                anchor = _tag({"type": "set", "position": [x, y, z], "block": "stone"},
                              ident + "." + suffix, "frame", support["source"],
                              reserved="structure")
                anchor["owner"] = ident
                operations.append(anchor)
            supports.append({"element": ident, "id": ident, "owner": ident,
                             "from": [x, low - 1, z], "to": [x, high + 1, z],
                             "reserved": "structure"})
        if features.get("lights", True):
            operations.append(_tag({"type": "set", "position": [x + 1, low, z],
                                    "block": "minecraft:glowstone"},
                                   f"site#1.light#{index + 1}", "light",
                                   "features.lights" if buildings else "site"))
    operations.extend(roads)
    operations.extend(wrappers)
    operations.extend(_plan_overrides(spec, bounds))
    plan["supports"] = supports
    plan_buildings(plan)
    blueprint = {"formatVersion": 1, "minecraftVersion": spec["minecraftVersion"],
                 "name": spec["archetype"], "seed": spec["seed"], "operations": operations,
                 "palettes": palettes, "metadata": {"planning": copy.deepcopy(plan)}}
    heights = np.where(bottom <= ceiling, bottom - 1, field.heights)
    for x, z in graded:
        heights[z, x] = floor
    for (x, z), top in surface_columns.items():
        heights[z, x] = top
    site = {**copy.deepcopy(plan), "stage": "site", "offset": [0, 0, 0], "floorY": floor,
            "terrain": "cave", "groundHeight": {"type": "heightfield",
                "field": replace(ground, heights=heights).to_dict()},
            "layout": layout["name"], "layoutRules": rules,
            "supports": supports, "density": density.to_dict()}
    return PlanningResult(site, {**copy.deepcopy(plan), "stage": "massing"},
        {**copy.deepcopy(plan), "stage": "detail", "diagnostics": diagnostics},
        blueprint, copy.deepcopy(config["palette"]), None, tuple(diagnostics))
