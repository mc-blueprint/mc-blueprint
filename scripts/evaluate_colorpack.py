"""固定計画で配色パックを役割単位に測定する。人工データはGの合格証拠にしない。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import tempfile
from collections import Counter, defaultdict
from importlib.resources import files
from pathlib import Path

import numpy as np
import yaml

from mcblueprint.blockdata import load_block_data, supported_versions
from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.colorpack import parse_colorpack
from mcblueprint.colorpack_selection import conditions, lookup, material_choice, relation_checks
from mcblueprint.colorpack_selection import target_color as sample_target
from mcblueprint.colors import face_linear, linear_to_oklab
from mcblueprint.config import DEFAULTS
from mcblueprint.exporters import EXPORTERS
from mcblueprint.importers import read_schematic
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.materialize import materialize, materialize_plan
from mcblueprint.pipeline import GENERATION, generated_blueprint_regions
from mcblueprint.planning import plan_buildspec
from mcblueprint.traversal import FULL_BLOCKS
from mcblueprint.volume import BlockVolume

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
GATES = {"materialAbsolute": 0.05, "materialSigma": 3, "originalDistance": 0.08,
         "projectedDistance": 0.03, "relationRate": 0.9}


def digest(content):
    return hashlib.sha256(content).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def face_colors(info, data, role, biome):
    face = "top" if role in ("floor", "path", "roof") else "side"
    if face not in info.faces:
        return None
    return [linear_to_oklab(face_linear(info, info.faces[face], data, biome, q))
            for q in range(5)]


def candidates(data, role, materials, biome):
    """本実行器のfull形状プローブで有効なfamilyの用途面中央値を返す。"""
    result = {}
    for info in data.blocks():
        if info.shape != "full" or info.material not in materials:
            continue
        if role in ("floor", "path") and info.id.removeprefix("minecraft:") not in FULL_BLOCKS:
            continue
        colors = face_colors(info, data, role, biome)
        if colors:
            result[info.id] = (info.material, colors[2])
    return result


def require_pack(result, roles):
    """対象役割の色・材質がD28等へ戻った場合に実行を拒否する。"""
    for role in roles:
        for axis in ("color", "material"):
            source = result.selection_sources.get(role, {}).get(axis, {})
            if source.get("source") != "pack":
                raise ValueError(f"{role}.{axis}: pack未使用: {source}")


def probe(version, roles):
    return {"formatVersion": 1, "minecraftVersion": version, "name": "配色役割プローブ",
            "operations": [{"type": "fill", "from": [i * 10, 0, 0],
                            "to": [i * 10 + 7, 0, 1], "role": role,
                            "element": "probe#1", "block": {
                                "role": role, "shape": "full", "state": {}}}
                           for i, role in enumerate(roles)]}


def select(pack, case, version, roles, settings, age, seed, work):
    config = copy.deepcopy(DEFAULTS)
    config["seed"] = seed
    config["palette"].update(settings)
    config["weathering"]["age"] = age
    if case.get("scope", "roles") == "building":
        if case["buildingType"] not in ("house", "tower"):
            raise ValueError("実建築測定は住宅/塔だけに対応します")
        spec = copy.deepcopy(case["buildspec"])
        if spec.get("config", {}).get("palette", {}).get("materials"):
            raise ValueError("統計受入れでは明示色/材質を指定できません")
        spec.update(minecraftVersion=version, archetype=case["buildingType"],
                    style=case["style"], seed=seed, config=config)
        spec.setdefault("site", {})["biome"] = case["biome"]
        resolved = resolve_buildspec(spec, project_dir=work, user_config=work / "no-user.yaml")
        plan = plan_buildspec(resolved)
        result = materialize_plan(plan.blueprint, style=case["style"], biome=case["biome"],
                                  seed=seed, palette=settings, colorpack=pack,
                                  building_type=case["buildingType"])
        generation = {"mode": GENERATION, "config": resolved.config,
                      "groundHeight": plan.site.get("groundHeight", {
                          "type": "constant", "value": plan.site["floorY"]})}
    else:
        result = materialize(probe(version, roles), style=case["style"], biome=case["biome"],
                             seed=seed, palette=settings, colorpack=pack,
                             building_type=case["buildingType"])
        generation = {"mode": GENERATION, "config": config, "groundHeight": 0}
    require_pack(result, roles)
    return result, generation


def generate(result, generation, seed):
    raw = copy.deepcopy(result.blueprint)
    raw.setdefault("metadata", {})["generation"] = generation
    blueprint = load_blueprint_dict(raw)
    volume, counts = BlockVolume(), Counter()
    for region in generated_blueprint_regions(blueprint, seed=seed):
        for pos, state in region.volume:
            if region.core.contains(pos) and state.id not in (
                "minecraft:air", "minecraft:cave_air", "minecraft:void_air"
            ):
                volume.set(pos, state)
                role = region.volume.attributes(pos).role
                if role is not None:
                    counts[(role, state.id)] += 1
    return blueprint, volume, counts


def placement(volume, data, normalize_origin=False):
    cells = sorted((tuple(pos), str(data.complete(state))) for pos, state in volume
                   if state.id not in ("minecraft:air", "minecraft:cave_air", "minecraft:void_air"))
    if normalize_origin and cells:
        origin = tuple(min(pos[axis] for pos, _ in cells) for axis in range(3))
        cells = [(tuple(p - o for p, o in zip(pos, origin, strict=True)), state)
                 for pos, state in cells]
    return digest(json.dumps(cells).encode())


def summarize(values):
    return np.quantile(values, LEVELS, axis=0).tolist() if values else None


def support_inventory(pack, plan):
    rows = []
    for row in pack.palette_prior:
        for role, value in row["roles"].items():
            requested = any(role in case["roles"] and lookup(
                pack.palette_prior, conditions(case["style"], case["buildingType"], case["biome"]),
                role)[1].get("when") == row["when"] for case in plan["conditions"])
            rows.append({"when": dict(row["when"]), "role": role,
                         "subjects": value["subjects"], "nEff": value["nEff"],
                         "status": "selected" if requested else "outside_fixed_plan",
                         "eligible": value["subjects"] >= 10 and value["nEff"] >= 10})
    return rows


def measure(pack, case, version, seeds, mix, age, work):
    data = load_block_data(version)
    chain = conditions(case["style"], case["buildingType"], case["biome"])
    style = yaml.safe_load(files("mcblueprint").joinpath(
        f"data/patterns/styles/{case['style']}.yaml").read_bytes())
    pools, distributions, priors, exclusions = {}, {}, {}, []
    for role in case["roles"]:
        distribution, source = lookup(pack.palette_prior, chain, role)
        if distribution is None or distribution["subjects"] < 10 or distribution["nEff"] < 10:
            exclusions.append({"role": role, "reason": "missing_or_insufficient_direct_support",
                               "source": source})
            continue
        choices = style["roles"].get(role, [])
        choices = choices if isinstance(choices, list) else [choices]
        pool = candidates(data, role, choices, case["biome"])
        # 実装と同じ条件探索。候補不足も別記し、支持のある親分布を補造しない。
        _, material_source = material_choice(pack.material_prior, chain, role, choices, 0)
        if material_source["source"] != "pack":
            exclusions.append({"role": role, "reason": "material_prior_missing",
                               "source": material_source})
            continue
        weights, _ = lookup(pack.material_prior, (material_source["when"],), role)
        common = {m: p for m, p in weights.items() if m in choices and p > 0}
        unavailable = sorted(set(common) - {m for m, _ in pool.values()})
        if unavailable:
            exclusions.append({"role": role, "reason": "candidate_shortage",
                               "materials": unavailable, "source": material_source})
            continue
        priors[role] = {m: p / sum(common.values()) for m, p in common.items()}
        pools[role], distributions[role] = pool, distribution
    roles = sorted(distributions)
    if not roles:
        return {"excluded": exclusions, "status": "no_supported_roles"}
    settings = {"variation": 1, "contrast": 0.5, "saturation": 0.5, "accent": 1, "mix": mix}
    before, after, final_material = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    actual, targets, projected, area = (defaultdict(list) for _ in range(4))
    samples, checks = [], []
    accent_violations, repeated, format_matches = 0, True, True
    missing = Counter()
    for seed in seeds:
        result, generation = select(pack, case, version, roles, settings, age, seed, work)
        selected = {}
        for role in roles:
            material, _ = material_choice(pack.material_prior, chain, role,
                                          list(priors[role]), seed)
            before[role][material] += 1
            after[role][data.get(next(iter(result.roles[role].values())).id).material] += 1
            target = sample_target(distributions[role], settings, seed, role)
            targets[role].append(target)
            available = [(name, color) for name, (m, color) in pools[role].items()
                         if m == material and set(result.roles[role]) <= (
                             {"full"} | set(data.families.get(data.get(name).family, {})))]
            if available:
                _, color = min(available, key=lambda item: (
                    math.dist(item[1], target), item[0]))
                projected[role].append(color)
            else:
                missing[f"{role}/candidate/{material}"] += 1
        blueprint, volume, counts = generate(result, generation, seed)
        signature = placement(volume, data)
        format_signature = placement(volume, data, normalize_origin=True)
        again, generation = select(pack, case, version, roles, settings, age, seed, work)
        repeated &= signature == placement(generate(again, generation, seed)[1], data)
        total = sum(counts.values())
        for role in roles:
            n = sum(count for (r, _), count in counts.items() if r == role)
            area[role].append(n / total if total else 0)
            color_sum = np.zeros(3)
            color_missing = False
            for (r, block), count in counts.items():
                if r != role:
                    continue
                info = data.get(block)
                final_material[role][info.material] += count
                colors = face_colors(info, data, role, case["biome"])
                if colors is None:
                    missing[f"{role}/face/{block}"] += count
                    color_missing = True
                else:
                    color_sum += np.array(colors[2]) * count
            if n and not color_missing:
                representative = tuple((color_sum / n).tolist())
                actual[role].append(representative)
                selected[role] = (role, {}, (representative,) * 5)
            else:
                missing[f"{role}/final_cells"] += 1
                selected[role] = (role, {}, ())
        for role, limit in result.max_area_ratios.items():
            source = result.selection_sources.get(role, {}).get("maxAreaRatio", {})
            if source.get("source") == "pack":
                count = sum(n for (r, _), n in counts.items() if r == role)
                accent_violations += int(total > 0 and count / total > limit)
        _, final_checks, _ = relation_checks(selected, pack.relation_rule, chain, settings)
        for check in final_checks:
            rule, _ = lookup(pack.relation_rule, chain, tuple(check["pair"]))
            if rule["subjects"] < 10 or rule["nEff"] < 10:
                check.update(applied=False, reason="insufficient_direct_support")
        checks.extend(final_checks)
        formats = {}
        for fmt, exporter in EXPORTERS.items():
            path = work / f"probe.{fmt}"
            exporter().export(volume, blueprint, path)
            formats[fmt] = placement(read_schematic(path).volume, data, normalize_origin=True)
            format_matches &= formats[fmt] == format_signature
        samples.append({"seed": seed, "placement": signature,
                        "normalizedPlacement": format_signature, "formats": formats,
                        "relationChecks": final_checks, "areaLimits": result.max_area_ratios,
                        "areaRatios": {
                            role: area[role][-1] for role in roles},
                        "selectionSources": result.selection_sources})
    reports = {}
    for role in roles:
        rates = {}
        for material, p in priors[role].items():
            tolerance = max(GATES["materialAbsolute"], GATES["materialSigma"] *
                            math.sqrt(p * (1 - p) / len(seeds)))
            pre, post = before[role][material] / len(seeds), after[role][material] / len(seeds)
            rates[material] = {"expected": p, "before": pre, "after": post,
                               "tolerance": tolerance, "passed": abs(pre - p) <= tolerance
                               and abs(post - p) <= tolerance,
                               "reselectionTradeoff": abs(post - p) > tolerance}
        observed = summarize(actual[role])
        target = summarize(targets[role])
        projection = summarize(projected[role])
        original = [distributions[role]["oklab"][axis][2] for axis in ("L", "a", "b")]
        original_distance = math.dist(observed[2], original) if observed else None
        projected_distance = (math.dist(observed[2], projection[2])
                              if observed and projection else None)
        reports[role] = {
            "sourceQuantiles": {a: list(v) for a, v in distributions[role]["oklab"].items()},
            "targetQuantiles": target, "actualQuantiles": observed,
            "projectedQuantiles": projection, "originalMedianDistance": original_distance,
            "projectedMedianDistance": projected_distance, "materialRates": rates,
            "finalMaterialArea": dict(final_material[role]),
            "finalMaterialRatios": {m: n / sum(final_material[role].values())
                                    for m, n in final_material[role].items()},
            "areaRatioQuantiles": summarize(area[role]),
            "passed": original_distance is not None
            and original_distance <= GATES["originalDistance"]
            and projected_distance is not None and projected_distance <= GATES["projectedDistance"]
            and all(r["passed"] for r in rates.values()),
        }
    applied = [c for c in checks if c["applied"]]
    numerator = sum(c["minimum"] <= c["actual"] <= c["maximum"] for c in applied)
    rate = numerator / len(applied) if applied else None
    relations = {"numerator": numerator, "denominator": len(applied), "rate": rate,
                 "excluded": dict(Counter(c["reason"] for c in checks if not c["applied"])),
                 "noRuleSamples": len(seeds) if not checks else 0,
                 "deltaQuantiles": {metric: summarize([c["actual"] for c in applied
                                                        if c["metric"] == metric])
                                    for metric in ("dL", "dC", "dh")}}
    return {"scope": case.get("scope", "roles"), "roles": reports,
            "excluded": exclusions, "missing": dict(missing), "relations": relations,
            "accentViolations": accent_violations, "deterministic": repeated,
            "formatMatches": format_matches, "samples": samples,
            "passed": not missing and all(r["passed"] for r in reports.values())
            and all(e["reason"] == "missing_or_insufficient_direct_support" for e in exclusions)
            and (rate is None or rate >= GATES["relationRate"]) and not accent_violations
            and repeated and format_matches}


def validate_plan(plan):
    """正式系列を§6に固定し、人工計画だけに標本・系列の縮小を許す。"""
    if type(plan["synthetic"]) is not bool:
        raise ValueError("syntheticは真偽値で指定してください")
    seeds = plan["seeds"]
    if (not seeds or any(type(seed) is not int for seed in seeds)
            or len(seeds) != len(set(seeds))):
        raise ValueError("seed集合は重複のない空でない整数集合にしてください")
    for key in ("mix", "weatherAge"):
        values = plan[key]
        if (not values or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                or len(values) != len(set(values))):
            raise ValueError(f"{key}は重複のない空でない数値集合にしてください")
    if not plan["synthetic"]:
        if sorted(seeds) != list(range(256)):
            raise ValueError("正式系列のseedsは0〜255に固定してください")
        if sorted(plan["mix"]) != [0, DEFAULTS["palette"]["mix"]]:
            raise ValueError("正式系列のmixは0と既定値0.3に固定してください")
        if sorted(plan["weatherAge"]) != [0, DEFAULTS["weathering"]["age"]]:
            raise ValueError("正式系列のweatherAgeは0と既定値0.2に固定してください")


def evaluate(pack_path, plan_path, output):
    plan = json.loads(plan_path.read_bytes())
    validate_plan(plan)
    pack = parse_colorpack(pack_path.read_text(encoding="utf-8"))
    seeds = plan["seeds"]
    data = files("mcblueprint").joinpath("data")
    lock = json.loads(data.joinpath("lock.json").read_bytes())
    report = {"method": "colorpack-acceptance-v1", "synthetic": plan["synthetic"],
              "gAcceptanceEvidence": False, "completed": False,
              "pack": {"path": str(pack_path.resolve()),
              "sha256": digest(pack_path.read_bytes())}, "plan": plan,
              "planSha256": digest(plan_path.read_bytes()), "gates": GATES,
              "supportInventory": support_inventory(pack, plan),
              "engineSha256": digest(b"".join(p.read_bytes() for p in sorted(
                  Path(files("mcblueprint")).rglob("*.py")))),
              "blockdata": lock["blockdata"],
              "blockdataActualSha256": {name: digest(data.joinpath("blockdata", name).read_bytes())
                                        for name in lock["blockdata"]["sha256"]},
              "vocabularySha256": digest(data.joinpath("colorpack-vocabulary.json").read_bytes()),
              "runnerSha256": digest(Path(__file__).read_bytes()), "results": []}
    if report["blockdataActualSha256"] != lock["blockdata"]["sha256"]:
        raise ValueError("blockdataの実内容がlockと一致しません")
    # 固定計画を生成前に保存する。結果で閾値や対象を変更しない。
    write_json(output, report)
    with tempfile.TemporaryDirectory(prefix="colorpack-") as directory:
        for version in supported_versions():
            for case in plan["conditions"]:
                for mix in plan["mix"]:
                    for age in plan["weatherAge"]:
                        result = measure(pack, case, version, seeds, mix, age, Path(directory))
                        report["results"].append({"version": version, "condition": case,
                                                  "mix": mix, "weatherAge": age, **result})
                        write_json(output, report)
                        print(f"{version} {case['style']} mix={mix} age={age}: 記録済み",
                              flush=True)
    report["completed"] = True
    write_json(output, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.pack, args.plan, args.output)
