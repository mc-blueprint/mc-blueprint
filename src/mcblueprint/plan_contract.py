"""内部計画 v2 の階層と要素参照。外部の scene/edits は v1 を使う。"""

from mcblueprint.errors import BlueprintError

PLAN_VERSION = 2
NON_BUILDING_ARCHETYPES = frozenset(("terrain", "cave"))


def plan_buildings(plan):
    """Site→区画→建物群→棟を走査する。旧版・重複ID・空の建物計画は拒否する。"""
    if plan.get("formatVersion") != PLAN_VERSION:
        raise BlueprintError("内部計画の版が一致しません。BuildSpecから再生成してください")
    buildings, ids = [], set()

    def register(item):
        ident = item["element"]
        if not isinstance(ident, str) or not ident or ident in ids:
            raise BlueprintError(f"内部計画の要素IDが不正または重複しています: {ident}")
        ids.add(ident)

    try:
        register(plan)
        for parcel in plan["parcels"]:
            register(parcel)
            for group in parcel["groups"]:
                register(group)
                for building in group["buildings"]:
                    register(building)
                    if not building["columns"] or not building["facades"]:
                        raise BlueprintError(
                            f"内部計画の期待柱または外壁がありません: {building['element']}"
                        )
                    for facade in building["facades"]:
                        ident = facade["id"]
                        if ident in ids or not ident.startswith(building["element"] + ".facade."):
                            raise BlueprintError(f"内部計画の外壁IDが不正です: {ident}")
                        ids.add(ident)
                    buildings.append(building)
        if not buildings and plan["archetype"] not in NON_BUILDING_ARCHETYPES:
            raise BlueprintError("内部計画に棟がありません")
        nodes = {}
        for node in plan["connections"]["nodes"]:
            if node["id"] in nodes or node["element"] not in ids:
                raise BlueprintError("内部計画の接続節点の参照が不正です")
            nodes[node["id"]] = node
        edges = set()
        adjacency = {ident: set() for ident in nodes}
        for edge in plan["connections"]["edges"]:
            if (edge["id"] in edges or edge["kind"] not in ("road", "cave", "walk")
                    or edge["from"] not in nodes or edge["to"] not in nodes):
                raise BlueprintError("内部計画の接続辺の参照が不正です")
            edges.add(edge["id"])
            adjacency[edge["from"]].add(edge["to"])
            adjacency[edge["to"]].add(edge["from"])
        for building in buildings:
            points = [building["entrance"], *building["arrivals"]]
            targets = [{ident for ident, node in nodes.items()
                        if node["element"] == building["element"] and node["position"] == point}
                       for point in points]
            if any(not ids for ids in targets):
                raise BlueprintError("内部計画の入口または階到着点の接続節点がありません")
            seen, pending = set(), list(targets[0])
            while pending:
                ident = pending.pop()
                if ident not in seen:
                    seen.add(ident)
                    pending.extend(adjacency[ident] - seen)
            if any(not seen.intersection(ids) for ids in targets):
                raise BlueprintError("内部計画の入口と階到着点が接続していません")
        parts = {b["element"] for b in buildings}
        for building in buildings:
            parts.update(item["id"] for key in ("columns", "beams", "facades")
                         for item in building.get(key, []))
            pending = list(building.get("operations", []))
            while pending:
                operation = pending.pop()
                if operation.get("element"):
                    parts.add(operation["element"])
                pending.extend(operation.get("operations", []))
        for junction in plan["junctions"]:
            register(junction)
            if (junction["owner"] != junction["element"]
                    or len(junction["parts"]) < 2
                    or any(p["element"] not in parts or p["shape"] not in
                           ("rectangle", "round", "square") for p in junction["parts"])
                    or any(c["first"] not in parts or c["second"] not in parts
                           for c in junction["contacts"])
                    or any(p["from"] not in nodes or p["to"] not in nodes
                           for p in junction["passages"])
                    or junction["roofPolicy"] not in ("join", "envelope")):
                raise BlueprintError("内部計画の接合参照が不正です")
    except (KeyError, TypeError) as exc:
        raise BlueprintError(f"内部計画 v2 が不正です: {exc}") from exc
    return tuple(buildings)


def building_for_element(plan, element):
    """セルの子要素IDから編集可能な棟を解決する。対象外なら None。"""
    return next((b for b in plan_buildings(plan) if element == b["element"]
                 or element.startswith(b["element"] + ".")), None)


def hierarchy(archetype, buildings):
    """安定した棟順から単一区画・建物群の共有計画を組み立てる。座標は世界座標。"""
    nodes, edges = [], []
    for building in buildings:
        root = building["element"]
        points = [building["entrance"], *building["arrivals"]]
        for index, point in enumerate(points):
            ident = f"{root}.walk#{index + 1}"
            nodes.append({"id": ident, "element": root, "position": point})
            if index:
                edges.append({"id": f"{root}.route#{index}", "kind": "walk",
                              "from": f"{root}.walk#{index}", "to": ident})
    plan = {"formatVersion": PLAN_VERSION, "element": "site#1", "archetype": archetype,
            "parcels": [{"element": "parcel#1", "groups": [
                {"element": "group#1", "buildings": buildings}]}],
            "connections": {"nodes": nodes, "edges": edges}, "junctions": []}
    plan_buildings(plan)
    return plan
