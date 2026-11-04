"""内部計画 v2 の接合レコードを施工 Operation へ写す。"""

from __future__ import annotations

import copy

from mcblueprint.errors import BlueprintError
from mcblueprint.operations.registry import build_operation
from mcblueprint.plan_contract import plan_buildings


def junction_operations(blueprint, materials):
    """接合に属する棟をまとめる。入力は変更せず、存在しない参照・重複所属は拒否する。

    Materialize 後に呼び、棟別の配色を保ったまま施工責務だけを接合へ移す。
    contacts/parts の部材参照は実在 ID を照合して所属棟へ解決する。
    """
    plan = blueprint["metadata"]["planning"]
    buildings = plan_buildings(plan)
    owners = {}
    for building in buildings:
        root = building["element"]
        owners[root] = root
        for key in ("columns", "beams", "facades"):
            owners.update({item["id"]: root for item in building[key]})
        pending = list(building["operations"])
        while pending:
            raw = pending.pop()
            if raw.get("element"):
                owners[raw["element"]] = root
            pending.extend(raw.get("operations", []))
    operations = list(blueprint["operations"])
    used = set()
    for junction in plan["junctions"]:
        roots = {owners[part["element"]] for part in junction["parts"]}
        if used & roots:
            raise BlueprintError("接合を共有する棟は一つの接合レコードへまとめてください")
        used.update(roots)
        selected = [raw for raw in operations if raw.get("element") in roots]
        if len(selected) != len(roots):
            raise BlueprintError("接合の各棟には一つの施工Operationが必要です")
        shapes = {}
        for part in junction["parts"]:
            root = owners[part["element"]]
            if root in shapes and shapes[root] != part["shape"]:
                raise BlueprintError("同じ棟の接合形状が矛盾しています")
            shapes[root] = part["shape"]
        raw = {"type": "building_junction", **copy.deepcopy(junction),
               "parts": [{"element": root, "shape": shapes[root]} for root in sorted(roots)],
               "buildings": selected,
               "profiles": [{key: b[key] for key in ("element", "params", "offset")}
                            for b in buildings if b["element"] in roots],
               "nodes": copy.deepcopy(plan["connections"]["nodes"]),
               **materials[sorted(roots)[0]]}
        if junction["roofPolicy"] == "join" and len({
            materials[root].get("roof") for root in roots
        }) != 1:
            raise BlueprintError("joinには同じ屋根材が必要です")
        for contact in raw["contacts"]:
            for key in ("first", "second"):
                contact[key] = owners[contact[key]]
        build_operation(raw, "planning.junction")
        index = min(i for i, item in enumerate(operations) if item.get("element") in roots)
        operations = [item for item in operations if item.get("element") not in roots]
        operations.insert(index, raw)
    return operations


def junction_geometry(plan):
    """評価用の接合幾何。施工と同じ入力検証で内部接触面の除外を限定する。"""
    if not plan["junctions"]:
        return ()
    wrappers = [{"type": "translate", "offset": b["offset"],
                 "rotation": b["params"].get("angle", 0), "element": b["element"],
                 "operations": b["operations"]} for b in plan_buildings(plan)]
    def concrete(value):
        if isinstance(value, dict):
            if set(value) == {"role", "shape", "state"}:
                name = {"stairs": "stone_brick_stairs", "slab": "stone_brick_slab"}.get(
                    value["shape"], "stone_bricks")
                from mcblueprint.model import BlockState

                return BlockState(name, tuple(sorted(value["state"].items()))).to_string()
            return {key: concrete(child) for key, child in value.items()}
        if isinstance(value, list):
            return [concrete(child) for child in value]
        return value

    materials = {b["element"]: {"wall": "stone_bricks", "frame": "stone_bricks",
                                "floor": "stone_bricks", "stairs": "stone_brick_stairs",
                                "roof": "stone_brick_stairs"} for b in plan_buildings(plan)}
    operations = junction_operations({"operations": concrete(wrappers),
                                      "metadata": {"planning": plan}}, materials)
    return tuple(build_operation(raw, "critic.junction") for raw in operations
                 if raw["type"] == "building_junction")
