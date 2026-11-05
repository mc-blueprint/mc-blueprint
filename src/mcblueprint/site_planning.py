"""城郭・街並みで共用する棟の配置と造成。"""

import json

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.registry import build_operation
from mcblueprint.operations.transform import Transform
from mcblueprint.plan_contract import plan_buildings
from mcblueprint.planning import _house, _plan_contract, _ref, _tag, plan_buildspec


def _place(resolved, kind, root, offset, angle, features, changes):
    spec = resolved.buildspec
    if "angle" in changes and changes["angle"] != angle:
        raise BlueprintError(f"Site: {root}: 回転変更が入口の接続に干渉します")
    result = plan_buildspec(resolve_buildspec({
        "minecraftVersion": spec["minecraftVersion"], "archetype": kind,
        "style": spec["style"], "size": [64, spec["size"][1], 64],
        "seed": spec["seed"], "config": resolved.config, "interior": spec["interior"],
        "features": features, "elements": {f"{kind}#1": {**changes, "angle": angle}},
    }))
    data, diagnostics = json.loads(json.dumps(
        [result.blueprint, result.diagnostics]).replace(f"{kind}#1", root))
    building = plan_buildings(data["metadata"]["planning"])[0]
    wrapper = data["operations"][0]
    old = Transform.rotation(angle, Vec3(0, 0, 0)).then(
        Transform.translation(Vec3.from_seq(building["offset"])))
    transform = Transform.rotation(angle, Vec3(0, 0, 0)).then(
        Transform.translation(Vec3.from_seq(offset)))
    wrapper["offset"] = offset
    body = building["operations"]
    for operation in [*body, *wrapper["operations"]]:
        if operation["element"].endswith(".threshold"):
            operation.update(reserved="circulation", owner="site#1.paths")
    detail = [op for op in wrapper["operations"] if op not in body]
    layout = _house(building["params"], root)[2] if kind == "house" else None
    contract = _plan_contract(root, body, detail, transform, building["params"], layout)
    delta = transform.offset - old.offset
    building.update(contract, offset=offset,
                    entrance=(Vec3.from_seq(building["entrance"]) + delta).to_list(),
                    arrivals=[(Vec3.from_seq(p) + delta).to_list() for p in building["arrivals"]])
    for decoration in building["decorations"]:
        decoration["operation"] = decoration["operation"].replace(
            f"@:{old.rows}:{old.offset}", f"@:{transform.rows}:{transform.offset}")
    return building, wrapper, data["palettes"], diagnostics


def grade_columns(field, columns, floor, *, clearance=0):
    """0起点の地形場のXZ列を同じ床高に造成する。元地形場は変更しない。"""
    operations = []
    for x, z in columns:
        top = int(field.heights[z, x])
        operations.append(_tag({"type": "fill", "from": [x, min(top, floor), z],
                                "to": [x, floor, z], "block": _ref("foundation")},
                               "foundation#1", "foundation", "site.terrain"))
        if top > floor or clearance:
            operations.append(_tag({"type": "fill", "from": [x, floor + 1, z],
                                    "to": [x, max(top + 1, floor + clearance), z],
                                    "block": "air"},
                                   "grading#1", "ground", "site.terrain"))
    return operations


def connect_houses(plan, houses, ident):
    """同じ床高・無回転の隣接棟を接合する。実寸の重なる側壁から通路を選ぶ。

    接触しない棟、柱・梁・既存階段を避けられない接触面はBlueprintError。
    planの接続グラフとjunctionsを更新し、各棟の形状は変更しない。
    """
    nodes, edges = plan["connections"]["nodes"], plan["connections"]["edges"]
    contacts, passages = [], []
    for i, (a, b) in enumerate(zip(houses, houses[1:], strict=False)):
        x, y, z = b["offset"]
        if (a["offset"][0] + a["params"]["width"] - 1 != x
                or a["offset"][1] != y or any(c["params"]["angle"] for c in (a, b))):
            raise BlueprintError("Site: 棟の寸法変更が接合位置に干渉します")
        front = max(a["offset"][2], z)
        top = min(c["offset"][1] + c["params"]["floors"] * c["params"]["floor_height"]
                  for c in (a, b))
        back = min(c["offset"][2] + c["params"]["depth"] - 1 for c in (a, b))
        contacts.append({"first": a["element"], "second": b["element"],
                         "from": [x, y, front], "to": [x, top, back]})
        occupied = []
        for c in (a, b):
            occupied.extend(AABB.of(Vec3.from_seq(run["from"]), Vec3.from_seq(run["to"]))
                            for key in ("columns", "beams") for run in c[key])
            transform = Transform.translation(Vec3.from_seq(c["offset"]))
            occupied.extend(transform.bounds(build_operation(op, "site.junction").bounds())
                            for op in c["operations"] if op.get("reserved") == "circulation")
        passage_z = next((cz for cz in sorted(range(front + 1, back),
                                             key=lambda cz: abs(cz - z - 7))
                          if not any(box.contains(Vec3(px, py, cz)) for box in occupied
                                     for px in range(x - 1, x + 2) for py in range(y, y + 3))),
                         None)
        if passage_z is None:
            raise BlueprintError("Site: 柱・梁・階段を避ける接合通路を収容できません")
        for suffix, building, dx in (("a", a, -1), ("b", b, 1)):
            nodes.append({"id": f"{ident}.{i}.{suffix}", "element": building["element"],
                          "position": [x + dx, y + 1, passage_z]})
            edges.append({"id": f"{ident}.{i}.{suffix}.walk", "kind": "walk",
                          "from": f"{ident}.{i}.{suffix}",
                          "to": building["element"] + ".walk#1"})
        passage = {"from": f"{ident}.{i}.a", "to": f"{ident}.{i}.b"}
        passages.append(passage)
        edges.append({"id": f"{ident}.route#{i}", "kind": "walk", **passage})
    plan["junctions"].append({"element": ident, "owner": ident,
                              "parts": [{"element": b["element"], "shape": "rectangle"}
                                        for b in houses],
                              "contacts": contacts, "passages": passages,
                              "roofPolicy": "envelope"})


