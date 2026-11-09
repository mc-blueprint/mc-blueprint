"""生成地形を棟なしの内部計画と既存の密度Operationへ接続する。"""

import copy

from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, Vec3
from mcblueprint.plan_contract import hierarchy
from mcblueprint.planning import PlanningResult, _plan_overrides
from mcblueprint.terrain_density import plan_density
from mcblueprint.terrain_field import plan_terrain


def plan_terrain_buildspec(resolved) -> PlanningResult:
    """Resolve済みの生成地形を計画する。入力の変更やセル展開は行わない。

    noneはflatとして扱う。既存地形ファイル・cave・elementsの編集は
    BlueprintError。Weatherの基準には密度変位前の地表場を保持する。
    """
    spec, config = resolved.buildspec, resolved.config
    kind = spec["site"]["terrain"]
    if kind == "none":
        kind = "flat"
    if kind == "cave":
        raise BlueprintError("Site: 空洞の生成にはcave/underground_cityを指定してください")
    if kind not in ("flat", "hill", "mountain", "plateau", "valley", "island"):
        raise BlueprintError(
            "Site: terrain単独の既存地形編集は未対応です。Blueprintを使用してください")
    if spec.get("elements"):
        raise BlueprintError("Site: terrainはelementsの編集に対応していません")
    bounds = AABB(Vec3(0, 0, 0), Vec3(*(v - 1 for v in spec["size"])))
    field = plan_terrain(bounds, kind, config["terrain"], spec["seed"], clearance=0)
    features = spec["features"]
    density = plan_density(field, overhangs=features["overhangs"], arches=features["arches"],
                           main_cave=False)
    plan = hierarchy("terrain", [])
    operations = [{"type": "terrain_density", "plan": density.to_dict(),
                   "minecraftVersion": spec["minecraftVersion"], "id": "terrain#1",
                   "element": "terrain#1", "source": "site.terrain"}]
    operations.extend(_plan_overrides(spec, bounds))
    blueprint = {"formatVersion": 1, "minecraftVersion": spec["minecraftVersion"],
                 "name": "terrain", "seed": spec["seed"], "operations": operations,
                 "metadata": {"planning": copy.deepcopy(plan)}}
    site = {**copy.deepcopy(plan), "stage": "site", "offset": [0, 0, 0],
            "floorY": bounds.min.y, "terrain": kind,
            "groundHeight": {"type": "heightfield", "field": field.to_dict()},
            "density": density.to_dict()}
    return PlanningResult(site, {**copy.deepcopy(plan), "stage": "massing"},
        {**copy.deepcopy(plan), "stage": "detail", "diagnostics": []},
        blueprint, copy.deepcopy(config["palette"]), None, ())
