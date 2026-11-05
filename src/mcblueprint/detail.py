"""固定幅の様式部品を外壁の柱間と階へ配置する。"""

from __future__ import annotations

import copy
import json
import math
import random

from mcblueprint import components
from mcblueprint.critic import plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.facade_projection import NORMALS, plan_facade_runs
from mcblueprint.materialize import _expand
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.base import derive_seed, intersect
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.shapes import disc_offsets
from mcblueprint.operations.transform import Transform

ROTATIONS = {"north": 0, "south": 180, "east": 90, "west": 270}


class DetailError(BlueprintError):
    """Detailの入力エラー。diagnosticは修正先付きのJSON互換診断。"""

    def __init__(self, message, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(f"{message} {json.dumps(diagnostic, ensure_ascii=False)}")


def _recess_openings(style, strength, root, body, detail, contract):
    maximum = style["depth_layers"]["opening"]
    if isinstance(maximum, list):
        maximum = maximum[-1]
    depth = math.floor(strength * maximum + 0.5)
    if not depth:
        return []
    structure = [AABB.of(Vec3.from_seq(item["from"]), Vec3.from_seq(item["to"]))
                 for item in (*contract["columns"], *contract["beams"])]
    passages = [(op, build_operation(op, root).bounds()) for op in body + detail
                if op.get("reserved") == "circulation"]
    recessed, diagnostics = [], []
    for op in detail:
        if op["type"] not in ("window", "doorway"):
            continue
        face = op.get("face")
        if face is None:
            axis = 2 if op.get("axis", "x") == "x" else 0
            face = ("south" if op["position"][axis] > 0 else "north") if axis == 2 else (
                "east" if op["position"][axis] > 0 else "west")
        inward = Vec3(*NORMALS[face]) * -depth
        original = build_operation(op, root).bounds()
        moved = copy.deepcopy(op)
        moved["position"] = (Vec3.from_seq(op["position"]) + inward).to_list()
        bounds = original.union(build_operation(moved, root).bounds())
        conflicts = [box for other, box in passages if other is not op] + structure
        if any(intersect(bounds, box) is not None for box in conflicts):
            diagnostic = {"code": "opening_recess_blocked", "metric": "opening_recess",
                          "severity": "warn", "element": op["element"],
                          "source": "config.decoration.depth", "actual": 0, "expected": depth,
                          "reason": "後退域が構造または通路の予約と重なるため後退量を0にしました",
                          "repairPaths": ["config.decoration.depth"]}
            op["comment"] = json.dumps(diagnostic, ensure_ascii=False)
            diagnostics.append(diagnostic)
            continue
        # 基準面の開口は投影の正本として残し、機能ブロックだけを内側へ移す。
        if op["type"] == "window":
            op["block"] = "air"
        else:
            op.pop("door", None)
        recessed.append({
            "type": "translate", "offset": [0, 0, 0],
            **{key: moved[key] for key in ("id", "element", "role", "source", "face",
                                          "reserved", "owner") if key in moved},
            "id": f"{op['id']}.recess",
            "operations": [{"type": "fill", "from": bounds.min.to_list(),
                            "to": bounds.max.to_list(), "block": "air"}, moved],
        })
    detail.extend(recessed)
    return diagnostics


def _module(name):
    try:
        return components.load_component(name, raw=True)
    except BlueprintError as exc:
        raise BlueprintError(f"Detail: 必須部品 {name}: {exc}") from exc


def _bounds(ops):
    boxes = [build_operation(op, "detail").bounds() for op in ops]
    return AABB.from_points([p for box in boxes for p in (box.min, box.max)])


def _depth_operations(raw, style, strength):
    def flatten(items):
        for op in items:
            if op["type"] == "translate" and op["offset"] == [0, 0, 0]:
                yield from flatten(op["operations"])
            else:
                yield op

    ops = list(flatten(_expand(raw["operations"])))
    palettes = raw.get("palettes", {})
    result = []
    for op in ops:
        ref = op.get("block")
        if "palette" in op:
            ref = palettes[op["palette"]][0]["block"]
        role = ref.get("role") if isinstance(ref, dict) else None
        op["role"] = role or "wall_main"
        box = build_operation(op, "detail").bounds()
        if role == "frame":
            maximum = style["depth_layers"]["frame"]
        elif role in ("trim", "roof", "window_frame"):
            maximum = style["depth_layers"]["cornice"] if role != "window_frame" else 1
        else:
            continue
        depth = math.floor(strength * maximum + 0.5)
        # 材質の役割ごとに移し、壁と機能開口の位置を一括移動しない。
        if depth:
            for key in ("from", "to", "position", "center"):
                if key in op:
                    op[key][2] = -min(depth, math.floor(
                        strength * (op[key][2] - box.min.z + 1) + 0.5))
            result.append(op)
    return result


def plan_detail(params, style, config, root, body, detail, contract, projected_contract,
                *, blocked=frozenset()):
    """detailへ装飾・開口後退を反映し、装飾計画・Palette・診断を返す。

    blockedはローカル座標の接合屋根・共有構造セル。装飾の配置候補から除く。
    """
    settings = config["decoration"]
    modules = style["modules"]
    required = ("bay", "entrance", "roof_edge")
    for kind in required:
        if not modules.get(kind):
            raise BlueprintError(f"Detail: style.modules.{kind}: 必須部品がありません")
    names = {name for kind in (*required, "window", "cornice")
             for name in modules.get(kind, [])}
    raw_modules = {name: _module(name) for name in sorted(names)}
    low, high = style["decoration"]["density"]
    target = low + settings["density"] * (high - low)
    floors, height = params["floors"], params["floor_height"]
    openings = [op for op in detail if op["type"] in ("window", "doorway")]
    opening_boxes = [(op, build_operation(op, root).bounds()) for op in openings]
    circulation = [build_operation(op, root).bounds() for op in body + detail
                   if op.get("reserved") == "circulation"]
    placed, palettes = [], {}

    def place(kind, face, start, end, fixed, level, index):
        name = modules.get(kind, modules["bay"])[0]
        raw = raw_modules[name]
        bounds = _bounds(raw["operations"])
        if bounds.size.x > end - start + 1 or bounds.size.y > height + 1:
            return
        if settings["depth"] == 0:
            return
        axis = 0 if face in ("north", "south") else 2
        middle = (start + end) // 2
        center = Vec3(middle, level * height + 1, fixed) if axis == 0 else Vec3(
            fixed, level * height + 1, middle)
        facades = [f for f in contract["facades"]
                   if f["from"][1] <= center.y <= f["to"][1]
                   and max(NORMALS, key=lambda n: sum(a * b for a, b in
                           zip(NORMALS[n], f["normal"], strict=True))) == face]
        if not facades:
            return
        facade = min(facades, key=lambda f: sum((center[i] - f["origin"][i]) ** 2
                                               for i in (0, 2)))
        origin_u = middle - (bounds.size.x - 1) // 2
        if face in ("south", "west"):
            origin_u += bounds.size.x - 1
        y = (level + 1) * height - bounds.max.y
        if kind in ("cornice", "roof_edge"):
            y = min((level + 1) * height, params.get("height", floors * height)) - bounds.max.y
        position = [origin_u, y, fixed] if axis == 0 else [fixed, y, origin_u]
        ident = f"{root}.detail.{face}.floor#{level + 1}.{kind}#{index}"
        ops = _depth_operations(copy.deepcopy(raw), style, settings["depth"])
        transform = Transform.rotation(ROTATIONS[face], Vec3(0, 0, 0)).then(
            Transform.translation(Vec3.from_seq(position)))
        children = []
        masks = [box for _, box in opening_boxes]
        masks = [box for box in masks if box.min[2 if axis == 0 else 0] <= fixed
                 <= box.max[2 if axis == 0 else 0]]
        # 予約と機能開口の投影を切り抜き、外付けの枠でも窓や扉を塞がない。
        for op in ops:
            if op.get("role") not in ("frame", "trim", "window_frame", "roof", "accent"):
                continue
            model = build_operation(op, "detail")
            points = list(model.cells())
            kept = []
            for point in points:
                world = transform.apply(point)
                if world in blocked:
                    continue
                if op.get("role") == "frame" and world.y == level * height + 1:
                    continue
                if any(box.contains(world) for box in circulation):
                    continue
                if any(box.min[axis] <= world[axis] <= box.max[axis]
                       and box.min.y <= world.y <= box.max.y for box in masks):
                    continue
                kept.append(point)
            if len(kept) == len(points):
                children.append(op)
            else:
                for point in kept:
                    children.append({"type": "set", "position": point.to_list(),
                                     **{key: op[key] for key in ("block", "palette", "role")
                                        if key in op}})
        for number, op in enumerate(children):
            op["id"] = f"part#{number + 1}"
        if not children:
            return
        placed.append({"type": "translate", "offset": position, "rotation": ROTATIONS[face],
                       "operations": children, "id": ident, "element": facade["id"],
                       "source": f"style.modules.{kind}", "face": "north",
                       "onConflict": "skip"})
        # モジュール内Paletteはこの配置だけの名前で保持する。
        for palette, entries in raw.get("palettes", {}).items():
            local_name = f"detail_{len(palettes)}_{palette}"
            palettes[local_name] = copy.deepcopy(entries)
            for op in children:
                if op.get("palette") == palette:
                    op["palette"] = local_name

    for face in NORMALS:
        axis = 0 if face in ("north", "south") else 2
        for run_index, (start, end, fixed) in enumerate(plan_facade_runs(params, face)):
            # 柱間4の端数は基本壁・開口を維持する。
            starts = list(range(start, end, 4)) or [start]
            for bay_index, bay_start in enumerate(starts):
                bay_end = min(bay_start + 4, end)
                index = run_index * 100 + bay_index + 1
                for level in range(floors):
                    door = any(op["type"] == "doorway"
                               and box.min[2 if axis == 0 else 0]
                               == fixed and bay_start <= op["position"][axis] <= bay_end
                               for op, box in opening_boxes) and level == 0
                    kind = "entrance" if door else "bay"
                    if not door and modules.get("window") and any(
                        op["type"] == "window" and bay_start <= op["position"][axis] <= bay_end
                        and box.min[2 if axis == 0 else 0] == fixed
                        and level * height <= op["position"][1] < (level + 1) * height
                        for op, box in opening_boxes
                    ):
                        kind = "window"
                    place(kind, face, bay_start, bay_end, fixed, level, index)
            # 屋根縁は幅の合う連続区間へ原寸で置く。
            place("roof_edge", face, start, end, fixed, floors - 1, run_index + 1)
    placed, decorations = _facade_selection(
        placed, params, style, config, root, body, projected_contract, raw_modules, target,
        circulation,
        blocked,
    )
    used_palettes = {op["palette"] for wrapper in placed for op in wrapper["operations"]
                     if "palette" in op}
    diagnostics = _recess_openings(style, settings["depth"], root, body, detail, contract)
    detail[:] = placed + detail
    return decorations, {name: entries for name, entries in palettes.items()
                         if name in used_palettes}, diagnostics


def _facade_selection(placed, params, style, config, root, body, contract, modules, target,
                      circulation=(), blocked=frozenset()):
    """階境の帯を先に確保し、非開口面積の予算内で部品の装飾部位を選ぶ。"""
    plan = plan_from_metadata(
        {"element": root, "params": params, **contract},
        massing={"operations": body}, offset=[0, 0, 0],
    )
    rays = {}
    for facade in plan.facades:
        for cell in facade.cells:
            if not cell.opening:
                rays.setdefault((facade.face, *facade.ray_key(cell.pos)), []).append(
                    (facade.element, cell.pos))
    selected = {facade.element: set() for facade in plan.facades}
    candidates = {facade.element: [] for facade in plan.facades}
    base, ornaments, bands, decorations = [], [], [], []
    rotation = Transform.rotation(params["angle"], Vec3(0, 0, 0))
    reverse = Transform.rotation(-params["angle"], Vec3(0, 0, 0))
    occupied = {point for run in (*plan.columns, *plan.beams) for point in run.cells}
    occupied.update(rotation.apply(point) for point in blocked)
    circulation = [rotation.bounds(box) for box in circulation]
    footprint = set()
    if "radius" in params:
        r = params["radius"]
        footprint = {(p.x, p.z) for p in disc_offsets(r, "solid", "y")} \
            if params["shape"] == "round" else {
                (x, z) for x in range(-r, r + 1) for z in range(-r, r + 1)}
    depth = math.floor(config["decoration"]["depth"] * style["depth_layers"]["cornice"] + 0.5)
    cornice = style["modules"].get("cornice", [])
    if cornice and depth:
        trim = next(op["block"] for op in modules[cornice[0]]["operations"]
                    if isinstance(op.get("block"), dict) and op["block"]["role"] == "trim")
        for facade in plan.facades:
            if not facade.cells:
                continue
            level = min(cell.pos.y for cell in facade.cells) // params["floor_height"]
            top = level == params["floors"] - 1
            if not style["decoration"].get("cornice" if top else "band_course"):
                continue
            y = min((level + 1) * params["floor_height"],
                    params.get("height", params["floors"] * params["floor_height"])) - 1
            points = {cell.pos + Vec3(*NORMALS[facade.face]) * depth
                      for cell in facade.cells if not cell.opening and cell.pos.y == y}
            if footprint:
                points = {p for p in points if (
                    reverse.apply(p).x, reverse.apply(p).z) not in footprint}
            points.difference_update(occupied)
            points = {p for p in points if not any(box.contains(p) for box in circulation)}
            area = sum(not cell.opening for cell in facade.cells)
            budget = min(max(1, math.floor(area * target + 0.5) // 2),
                         math.floor(area * style["decoration"]["density"][1]))
            ordered = sorted(points, key=lambda p: (p.x, p.y, p.z))
            if len(ordered) > budget:
                points = {ordered[i * len(ordered) // budget] for i in range(budget)}
            if not points:
                continue
            block = copy.deepcopy(trim)
            local_face = reverse.apply_direction(facade.face)
            if "facing" in block["state"]:
                block["state"]["facing"] = {
                    "north": "south", "south": "north", "east": "west", "west": "east",
                }[local_face]
            ident = f"{facade.element}.detail.cornice"
            ops = [{"type": "set", "position": reverse.apply(p).to_list(),
                    "block": block, "role": "trim"}
                   for p in sorted(points, key=lambda p: (p.x, p.y, p.z))]
            bands.append({"type": "translate", "offset": [0, 0, 0], "operations": ops,
                          "id": ident, "element": facade.element, "face": local_face,
                          "source": "style.modules.cornice", "onConflict": "skip"})
            selected[facade.element].update(points)
            occupied.update(points)

    for wrapper in placed:
        transform = Transform.rotation(wrapper["rotation"], Vec3(0, 0, 0)).then(
            Transform.translation(Vec3.from_seq(wrapper["offset"])))
        face = rotation.apply_direction(transform.apply_direction("north"))
        axis = 0 if face in ("north", "south") else 2
        keep = []
        for part, op in enumerate(wrapper["operations"]):
            if op["role"] not in ("trim", "window_frame", "accent"):
                keep.append(op)
                continue
            groups = {}
            for local in build_operation(op, "detail").cells():
                point = rotation.apply(transform.apply(local))
                targets = rays.get((face, point[axis], point.y), [])
                if not targets:
                    continue
                element, _ = min(targets, key=lambda t: abs(
                    t[1][2 if axis == 0 else 0] - point[2 if axis == 0 else 0]))
                # 窓枠は左右の縦枠と横枠を独立した部位として選ぶ。
                key = (element, local.x if op["role"] == "window_frame" else None)
                groups.setdefault(key, []).append((local, point))
            for (element, column), points in groups.items():
                candidates[element].append((wrapper, op, part, column, points))
        if keep:
            base.append({**wrapper, "operations": keep})

    exterior = {}
    for wrapper in base + bands:
        transform = Transform.rotation(wrapper.get("rotation", 0), Vec3(0, 0, 0)).then(
            Transform.translation(Vec3.from_seq(wrapper["offset"])))
        face = rotation.apply_direction(transform.apply_direction(wrapper.get("face", "north")))
        for op in wrapper["operations"]:
            for local in build_operation(op, "detail").cells():
                point = rotation.apply(transform.apply(local))
                exterior[point] = face, op["role"]
    depths = {}
    for point, (face, role) in exterior.items():
        if role != "roof":
            axis = 0 if face in ("north", "south") else 2
            depths.setdefault((face, point[axis], point.y), []).append(point)

    for facade in plan.facades:
        area = sum(not cell.opening for cell in facade.cells)
        upper = math.floor(area * style["decoration"]["density"][1])
        if area and math.ceil(area * style["decoration"]["density"][0]) > upper:
            dimensions = ("radius", "floor_height") if "radius" in params else (
                "width", "depth", "floor_height")
            raise DetailError(
                f"Detail: {facade.element}: 装飾の収容不足 "
                f"(非開口面積={area}、密度範囲={style['decoration']['density']})",
                {"code": "detail_capacity", "metric": "decoration_density",
                 "severity": "error", "element": facade.element,
                 "source": "style.decoration.density",
                 "actual": {"area": area, "maxBlocks": upper},
                 "expected": {"density": style["decoration"]["density"]},
                 "repairPaths": [*(f"elements.{root}.{key}" for key in dimensions), "size"]},
            )
        desired = min(upper, math.floor(area * target + 0.5))
        options = candidates[facade.element]
        opposite = {"south": "north", "west": "east"}.get(facade.face, facade.face)
        rng = random.Random(derive_seed(config["seed"], "detail", f"{root}:{opposite}"))
        shared = rng.random() < config["decoration"]["symmetry"]
        seed_face = opposite if shared else facade.face
        ranking = random.Random(derive_seed(config["seed"], "detail", f"{root}:{seed_face}"))
        ranking.shuffle(options)
        layer = math.floor(config["decoration"]["depth"] * style["depth_layers"]["frame"] + 0.5)
        if layer:
            for number, cell in enumerate(facade.cells):
                if cell.opening:
                    continue
                point = cell.pos + Vec3(*NORMALS[facade.face]) * layer
                if any(box.contains(point) for box in circulation):
                    continue
                if footprint and (reverse.apply(point).x, reverse.apply(point).z) in footprint:
                    continue
                local = reverse.apply(point)
                wrapper = {"type": "translate", "offset": [0, 0, 0],
                           "id": f"{facade.element}.detail.bay#{number}",
                           "element": facade.element, "face": reverse.apply_direction(facade.face),
                           "source": "style.modules.bay", "onConflict": "skip"}
                op = {"type": "set", "position": local.to_list(), "role": "frame",
                      "block": {"role": "frame", "shape": "full", "state": {}}}
                options.append((wrapper, op, 0, None, [(local, point)]))

        axis = facade.ray_axis
        surfaces = {cell.pos: 0 for cell in facade.cells if not cell.opening}
        projection = facade.wall_projection()
        windows = {}
        window_at = {}
        for cell in facade.cells:
            if not cell.opening:
                windows.setdefault(cell.window, []).append(cell.pos)
                window_at[cell.pos] = cell.window
        for pos in surfaces:
            surfaces[pos] = max([0, *(facade.depth_at(p) - facade.depth_at(pos)
                                     for p in depths.get((facade.face, pos[axis], pos.y), ()))])

        def relief(points, surfaces, projection=projection,
                   windows=windows, window_at=window_at, facade=facade):
            changed = {}
            for _, point in points:
                if point in occupied:
                    continue
                pos = projection.get(facade.ray_key(point))
                if pos is not None:
                    depth = max(surfaces[pos], facade.depth_at(point) - facade.depth_at(pos))
                    if depth != surfaces[pos]:
                        changed[pos] = max(changed.get(pos, depth), depth)
            affected = {window_at[pos] for pos in changed}
            gain = sum(len({changed.get(p, surfaces[p]) for p in windows[key]}) > 1
                       for key in affected) - sum(
                len({surfaces[p] for p in windows[key]}) > 1 for key in affected)
            return gain, changed

        while options:
            used = selected[facade.element]
            valid = [(index, len({point for _, point in candidate[4]} - occupied))
                     for index, candidate in enumerate(options)]
            valid = [(i, count, relief(options[i][4], surfaces)[0])
                     for i, count in valid if count and len(used) + count <= upper]
            valid = [item for item in valid if item[2] >= 0]
            if not valid:
                break
            index, count, gain = min(valid, key=lambda item: (
                -item[2], abs(desired - len(used) - item[1])))
            if (abs(desired - len(used) - count) >= abs(desired - len(used))
                    and gain <= 0):
                break
            wrapper, op, part, column, points = options.pop(index)
            _, changes = relief(points, surfaces)
            surfaces.update(changes)
            ident = f"{wrapper['id']}.ornament#{part}.{column}.{facade.element}"
            children = [{"type": "set", "position": local.to_list(),
                         **{k: op[k] for k in ("block", "palette", "role") if k in op}}
                        for local, point in points if point not in occupied]
            ornaments.append({**wrapper, "id": ident, "element": facade.element,
                              "operations": children})
            additions = {point for _, point in points} - occupied
            used.update(additions)
            occupied.update(additions)

    for wrapper in ornaments + bands:
        transform = Transform.rotation(wrapper.get("rotation", 0), Vec3(0, 0, 0)).then(
            Transform.translation(Vec3.from_seq(wrapper["offset"])))
        face = next(f.face for f in plan.facades if f.element == wrapper["element"])
        for index, op in enumerate(wrapper["operations"]):
            op["id"] = f"part#{index + 1}"
            decorations.append({
                "operation": f"{wrapper['id']}@:{transform.rows}:{transform.offset}/{op['id']}",
                "element": wrapper["element"], "face": face,
            })
    return base + ornaments + bands, decorations
