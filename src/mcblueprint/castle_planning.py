"""地形の高所に城壁・接合棟・塔と通行経路を計画する。"""

from __future__ import annotations

import copy
from collections import deque
from itertools import combinations, product

from mcblueprint.buildspec import SUPPORTED_TERRAINS
from mcblueprint.errors import BlueprintError
from mcblueprint.layouts import load_layout
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.shapes import disc_offsets
from mcblueprint.operations.transform import Transform
from mcblueprint.plan_contract import hierarchy, plan_buildings
from mcblueprint.planning import (
    PlanningResult,
    _plan_overrides,
    _ref,
    _tag,
)
from mcblueprint.site_planning import _place, connect_houses, grade_columns
from mcblueprint.terrain_density import plan_density
from mcblueprint.terrain_field import plan_terrain

TOWER_RADIUS = 4


def _tower_positions(west, north, span, count, spacing, road_width):
    corners = [(west + TOWER_RADIUS, north, 180),
               (west + span - TOWER_RADIUS, north, 180),
               (west + span - TOWER_RADIUS, north + span, 0),
               (west + TOWER_RADIUS, north + span, 0)]
    if count <= 4:
        return corners[:count]
    options = []
    before, after = (road_width - 1) // 2, road_width // 2
    gate_min, gate_max = span // 2 - before - 1, span // 2 + after + 1
    for side in range(4):
        lo, hi = (0, span) if side < 2 else (TOWER_RADIUS, span - TOWER_RADIUS)
        slots = [u for u in range(lo + spacing, hi - spacing + 1)
                 if side != 3 or u + TOWER_RADIUS + 1 < gate_min
                 or u - TOWER_RADIUS - 1 > gate_max]
        choices = {0: ()}
        for number in range(1, (hi - lo) // spacing):
            candidates = (points for points in combinations(slots, number)
                          if all(b - a >= spacing
                                 for a, b in zip(points, points[1:], strict=False)))
            points = min(candidates, key=lambda points: (sum(
                ((u - lo) * (number + 1) - (i + 1) * (hi - lo)) ** 2
                for i, u in enumerate(points)), points), default=None)
            if points is not None:
                choices[number] = points
        options.append(choices)
    counts = min((counts for counts in product(*(side.keys() for side in options))
                  if sum(counts) == count - 4),
                 key=lambda counts: (sum(n * n for n in counts), tuple(-n for n in counts)),
                 default=None)
    if counts is None:
        raise BlueprintError("Site: 要求された塔数をsizeに収容できません")
    positions = list(corners)
    for side, number in enumerate(counts):
        for u in options[side][number]:
            positions.append(((west, north + u, 90), (west + span, north + u, 270),
                              (west + u, north + span, 0), (west + u, north, 180))[side])
    return positions


def _courtyard_path(start, goal, blocked, area):
    """城壁内の空き列だけを探索し、既存pathへ渡す直交経由点を返す。"""
    west, north, east, south = area
    previous = {start: None}
    pending = deque([start])
    while pending:
        point = pending.popleft()
        if point == goal:
            break
        for dx, dz in ((0, 1), (1, 0), (-1, 0), (0, -1)):
            neighbor = point[0] + dx, point[1] + dz
            x, z = neighbor
            if (west < x < east and north < z < south
                    and neighbor not in blocked and neighbor not in previous):
                previous[neighbor] = point
                pending.append(neighbor)
    if goal not in previous:
        raise BlueprintError("Site: 城壁・棟・塔と干渉しない接続路を収容できません")
    cells = []
    point = goal
    while point is not None:
        cells.append(point)
        point = previous[point]
    cells.reverse()
    return [point for i, point in enumerate(cells) if i in (0, len(cells) - 1)
            or (point[0] - cells[i - 1][0], point[1] - cells[i - 1][1])
            != (cells[i + 1][0] - point[0], cells[i + 1][1] - point[1])]


def plan_castle(resolved) -> PlanningResult:
    """castleの内部計画v2を返す。収容不足・地形急勾配・干渉はBlueprintError。

    medievalの固定layoutと既存住宅・角塔を使用する。要求塔数は削減しない。
    生成地形は既存の局所密度Operation、造成は列ごとの切土・基礎で表現する。
    """
    spec, config = resolved.buildspec, resolved.config
    layout = load_layout(f"{spec['style']}.castle")
    features, size = spec["features"], spec["size"]
    if min(size[0], size[2]) < 64 or size[1] < 32:
        raise BlueprintError("Site: castleのsizeは64×32×64以上が必要です")
    if spec["site"]["terrain"] not in SUPPORTED_TERRAINS[:-1]:
        raise BlueprintError("Site: castleは生成地形またはnone/flatを指定してください")
    span = min(size[0] - 21, size[2] - 21, layout["rules"]["wallSpacing"][1])
    spacing = layout["rules"]["towerSpacing"][0]
    road_width = layout["rules"]["roadWidth"][0]
    positions = _tower_positions(0, 0, span, features["towers"], spacing, road_width)
    bounds = AABB(Vec3(0, 0, 0), Vec3(*(v - 1 for v in size)))
    terrain = spec["site"]["terrain"]
    field = plan_terrain(bounds, "flat" if terrain == "none" else terrain,
                         config["terrain"], spec["seed"], clearance=20)
    candidates = []
    for x in sorted({10, (size[0] - span) // 2, size[0] - span - 11}):
        for z in sorted({10, (size[2] - span) // 2, size[2] - span - 11}):
            heights = field.heights[z:z + span + 1, x:x + span + 1]
            candidates.append((float(heights.mean()) - float(heights.max() - heights.min()),
                               x, z, int(heights.max())))
    _, west, north, ground = max(candidates, key=lambda c: (c[0], -c[1], -c[2]))
    east, south, floor = west + span, north + span, ground + 1
    if floor + 16 >= size[1]:
        raise BlueprintError("Site: 地形基礎と城郭の頭上がsizeに収まりません")
    heights = field.heights[north:south + 1, west:east + 1]
    if int(heights.max() - heights.min()) > span // 2:
        raise BlueprintError("Site: 地形が急勾配で城郭の造成範囲を超えます")
    cx = (west + east) // 2
    corridor = north + 9
    positions = [(west + x, north + z, angle) for x, z, angle in positions]
    if any(max(abs(a[0] - b[0]), abs(a[1] - b[1])) < spacing
           for i, a in enumerate(positions) for b in positions[i + 1:]):
        raise BlueprintError("Site: 要求された塔数をsizeに収容できません")
    buildings, wrappers, palettes, diagnostics = [], [], {}, []
    edits = spec.get("elements", {})
    roots = {f"house#{i + 1}" for i in range(3 if features["keep"] else 2)}
    roots.update(f"tower#{i + 1}" for i in range(len(positions)))
    if set(edits) - roots:
        raise BlueprintError(f"Massing: elementsの未知の要素ID: {sorted(set(edits) - roots)}")
    for i in range(3 if features["keep"] else 2):
        root = f"house#{i + 1}"
        placed = _place(resolved, "house", root, [cx - 12 + i * 8, floor, north + 18], 0,
                        {"floors": 2 if i == 0 and features["keep"] else 1,
                         "floor_height": 4, "roof": "gable"},
                        {"width": 9, "depth": 11, **edits.get(root, {})})
        b, wrapper, p, d = placed
        buildings.append(b)
        wrappers.append(wrapper)
        palettes.update(p)
        diagnostics.extend(d)
    for i, (x, z, angle) in enumerate(positions):
        root = f"tower#{i + 1}"
        b, wrapper, p, d = _place(
            resolved, "tower", root, [x, floor, z], angle,
            {"shape": "square", "floors": 2, "floor_height": 4, "battlement": True},
            {"radius": TOWER_RADIUS, **edits.get(root, {})})
        buildings.append(b)
        wrappers.append(wrapper)
        palettes.update(p)
        diagnostics.extend(d)
    plan = hierarchy("castle", buildings)
    nodes, edges = plan["connections"]["nodes"], plan["connections"]["edges"]
    nodes.append({"id": "gate#1.outside", "element": "site#1",
                  "position": [cx, floor + 1, north - 10]})
    nodes.append({"id": "gate#1.inside", "element": "site#1",
                  "position": [cx, floor + 1, corridor]})
    operations = [_tag({"type": "terrain_density", "plan": plan_density(
        field, overhangs=False).to_dict(), "minecraftVersion": spec["minecraftVersion"]},
        "terrain#1", "ground", "site.terrain")]
    # 中庭全体を同じ床高に造成する。元地形場は別に保持する。
    operations.extend(grade_columns(field, (
        (x, z) for z in range(north - 10, south + 6)
        for x in range(west - 5, east + 6)), floor))
    notch = 2 + spec["seed"] % 2
    polygon = [[west, north], [east, north], [east, south], [cx + notch, south],
               [cx + notch, south - 1], [cx - notch, south - 1],
               [cx - notch, south], [west, south], [west, north]]
    tower_columns = set()
    for building in buildings:
        if building["archetype"] != "tower":
            continue
        x, _, z = building["offset"]
        radius = building["params"]["radius"]
        offsets = (disc_offsets(radius, "solid", "y")
                   if building["params"]["shape"] == "round" else
                   (Vec3(dx, 0, dz) for dx in range(-radius, radius + 1)
                    for dz in range(-radius, radius + 1)))
        tower_columns.update((x + p.x, z + p.z) for p in offsets)
    wall_cells = set()
    if features["curtain_wall"]:
        perimeter = {(x, z) for x in range(west, east + 1) for z in (north, south)}
        perimeter.update((x, z) for z in range(north, south + 1) for x in (west, east))
        for x in range(cx - notch + 1, cx + notch):
            perimeter.remove((x, south))
        perimeter.update((x, south - 1) for x in range(cx - notch, cx + notch + 1))
        for x, z in sorted(perimeter):
            if (x, z) in tower_columns:
                continue
            if (z == north and cx - (road_width - 1) // 2 - 1 <= x
                    <= cx + road_width // 2 + 1):
                continue
            wall_cells.add((x, z))
            for dy, height, reserved in ((1, 2, "structure"), (3, 3, None)):
                operations.append(_tag({"type": "wall", "from": [x, floor + dy, z],
                                        "to": [x, floor + dy, z], "height": height,
                                        "block": _ref("wall_main")},
                                       "wall#1", "wall_main", "features.curtain_wall",
                                       reserved=reserved))
    if features["gatehouse"]:
        operations.append(_tag({"type": "gate", "position": [cx, floor, north],
                                "axis": "x", "width": road_width, "height": 3, "depth": 2,
                                "jamb": 1, "block": _ref("wall_main")},
                               "gate#1", "wall_main", "features.gatehouse"))
    if features["moat"]:
        for distance in (6, 10):
            for z in range(north - distance, south + distance + 1):
                for x in range(west - distance, east + distance + 1):
                    if x not in (west - distance, east + distance) and z not in (
                            north - distance, south + distance):
                        continue
                    base = min(int(field.heights[z, x]), floor - 3)
                    operations.append(_tag({"type": "fill",
                                            "from": [x, base, z],
                                            "to": [x, floor, z], "block": _ref("foundation")},
                                           "moat#1.bank", "foundation", "features.moat"))
        for a, b in (([west - 9, south + 7], [east + 9, south + 9]),
                     ([west - 9, north - 9], [west - 7, south + 9]),
                     ([east + 7, north - 9], [east + 9, south + 9]),
                     ([west - 9, north - 9], [east + 9, north - 7])):
            for z in range(a[1], b[1] + 1):
                for x in range(a[0], b[0] + 1):
                    base = min(int(field.heights[z, x]), floor - 3)
                    operations.append(_tag({"type": "fill",
                                            "from": [x, base, z],
                                            "to": [x, floor - 3, z], "block": _ref("foundation")},
                                           "moat#1.base", "foundation", "features.moat"))
            operations.append(_tag({"type": "fill", "from": [a[0], floor - 2, a[1]],
                                    "to": [b[0], floor - 1, b[1]], "block": "water"},
                                   "moat#1", "ground", "features.moat"))
            operations.append(_tag({"type": "fill", "from": [a[0], floor, a[1]],
                                    "to": [b[0], max(floor + 3, int(field.heights[
                                        a[1]:b[1] + 1, a[0]:b[0] + 1].max()) + 5), b[1]],
                                    "block": "air"},
                                   "moat#1", "ground", "features.moat"))
    # 接触面と通路候補は実寸・期待構造・既存階段から決める。
    houses = [b for b in buildings if b["archetype"] == "house"]
    connect_houses(plan, houses, "junction#1")

    def route(ident, points, target, *, width=1, start="gate#1.inside", kind="walk"):
        points = [p for i, p in enumerate(points) if not i or p != points[i - 1]]
        operations.append(_tag({"type": "path", "points": points, "width": width,
                                "block": _ref("path"), "clearance": 2},
                               f"site#1.{ident}", "path", "site", reserved="circulation"))
        operations[-1]["owner"] = "site#1.paths"
        edges.append({"id": ident, "kind": kind, "from": start, "to": target})

    route("road#1", [[cx, floor, north - 10], [cx, floor, corridor]], "gate#1.inside",
          width=road_width, start="gate#1.outside", kind="road")
    footprints = {b["element"]: build_operation(wrapper, "castle.approach").bounds()
                  for b, wrapper in zip(buildings, wrappers, strict=True)}
    for b in buildings:
        entry = b["entrance"]
        x, _, z = entry
        outward = Transform.rotation(b["params"]["angle"], Vec3(0, 0, 0)).apply(Vec3(0, 0, -1))
        x, z = x + outward.x, z + outward.z
        terminal = set()
        box = footprints[b["element"]]
        px, pz = x, z
        while box.min.x <= px <= box.max.x and box.min.z <= pz <= box.max.z:
            terminal.add((px, pz))
            px, pz = px + outward.x, pz + outward.z
        blocked = set(wall_cells)
        for root, box in footprints.items():
            blocked.update((px, pz) for pz in range(box.min.z, box.max.z + 1)
                           for px in range(box.min.x, box.max.x + 1)
                           if root != b["element"] or (px, pz) not in terminal)
        points = [[px, floor, pz] for px, pz in _courtyard_path(
            (cx, corridor), (x, z), blocked, (west, north, east, south))]
        route(b["element"] + ".approach", points, b["element"] + ".walk#1")
    operations.extend(wrappers)
    plan_buildings(plan)
    blueprint = {"formatVersion": 1, "minecraftVersion": spec["minecraftVersion"],
                 "name": "castle", "seed": spec["seed"], "operations": operations,
                 "palettes": palettes, "metadata": {"planning": copy.deepcopy(plan)}}
    operations.extend(_plan_overrides(spec, bounds))
    for wrapper in wrappers:
        box = build_operation(wrapper, "castle").bounds()
        if not bounds.contains(box.min) or not bounds.contains(box.max):
            raise BlueprintError("Site: 棟・塔の外形がsizeに収まりません")
        building = next(b for b in buildings if b["element"] == wrapper["element"])
        if (building["archetype"] == "tower"
                and building["params"]["radius"] != TOWER_RADIUS):
            raise BlueprintError("Site: 塔の半径変更が城壁・堀に干渉します")
        if building["archetype"] == "house" and not (
                west + 3 < box.min.x <= box.max.x < east - 3
                and north + 5 < box.min.z <= box.max.z < south - 3):
            raise BlueprintError("Site: 棟の外形が城壁・堀・外周経路に干渉します")
    site = {**copy.deepcopy(plan), "stage": "site", "offset": [0, 0, 0], "floorY": floor,
            "terrain": terrain, "groundHeight": {"type": "heightfield", "field": field.to_dict()},
            "wallPolygon": polygon, "layout": layout["name"], "layoutRules": layout["rules"]}
    return PlanningResult(site, {**copy.deepcopy(plan), "stage": "massing"},
                          {**copy.deepcopy(plan), "stage": "detail", "diagnostics": diagnostics},
                          blueprint, copy.deepcopy(config["palette"]), None, tuple(diagnostics))
