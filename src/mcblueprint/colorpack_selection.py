"""検証済み配色パックの条件探索・抽選と実ブロックの関係検査。"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

from mcblueprint.operations.base import derive_seed


def conditions(style: str | None, building_type: str | None, biome: str) -> tuple[dict, ...]:
    """公開対応表へ写し、要求に存在する条件だけを固定の緩和順で返す。"""
    vocabulary = json.loads(resources.files("mcblueprint.data").joinpath(
        "colorpack-vocabulary.json").read_text(encoding="utf-8"))
    values = {key: value for key, value in (
        ("style", vocabulary["styleMap"].get(style)),
        ("buildingType", vocabulary["buildingTypeMap"].get(building_type)),
        ("biomeLike", vocabulary["biomeMap"].get(biome.removeprefix("minecraft:"))),
    ) if value is not None}
    result = []
    for keys in (("style", "buildingType", "biomeLike"), ("style", "biomeLike"),
                 ("style",), ()):
        when = {key: values[key] for key in keys if key in values}
        # buildingTypeだけなど、輸出契約にない組は探索しない。
        if "style" not in when:
            when = {}
        if when not in result:
            result.append(when)
    return tuple(result)


def lookup(rows: Sequence[Mapping], chain: Sequence[Mapping], key: str | tuple):
    """役割または無向pairごとに最初の分布と選択元を返す。欠損はD28。"""
    for when in chain:
        for row in rows:
            if row["when"] != when:
                continue
            if isinstance(key, tuple):
                value = row if set(row["pair"]) == set(key) else None
            elif "roles" in row:
                value = row["roles"].get(key)
            else:
                value = row if row["role"] == key else None
            if value is not None:
                source = {"source": "pack", "when": dict(when)}
                if value.get("support", {}).get("status") == "smoothed":
                    source["supportSourceWhen"] = dict(value["support"]["sourceWhen"])
                return value, source
    return None, {"source": "d28", "reason": "distribution_missing"}


def quantile(values: Sequence[float], q: float) -> float:
    """5/25/50/75/95%の分位点を外挿せず線形補間する。"""
    levels = (0.05, 0.25, 0.5, 0.75, 0.95)
    if q <= levels[0]:
        return values[0]
    for i in range(1, len(levels)):
        if q <= levels[i]:
            fraction = (q - levels[i - 1]) / (levels[i] - levels[i - 1])
            return values[i - 1] + fraction * (values[i] - values[i - 1])
    return values[-1]


def target_color(distribution: Mapping, settings: Mapping, seed: int, role: str) -> tuple:
    """用途別seedから同じ分位水準でL/a/bを抽選し、彩度設定を適用する。"""
    rng = random.Random(derive_seed(seed, "materialize", f"{role}/pack/color"))
    q = 0.5 + (rng.random() - 0.5) * 0.9 * settings["variation"]
    light, a, b = (quantile(distribution["oklab"][axis], q) for axis in ("L", "a", "b"))
    factor = (0.5 + settings["saturation"]) * (
        1 + settings["accent"] if role == "accent" else 1)
    return light, a * factor, b * factor


def material_choice(rows, chain, role: str, choices: list[str], seed: int):
    """様式候補との共通集合を再正規化して抽選する。共通集合なしは親を探す。"""
    for when in chain:
        weights, source = lookup(rows, (when,), role)
        if weights is None:
            continue
        common = sorted(name for name in set(choices) if weights.get(name, 0) > 0)
        if not common:
            continue
        rng = random.Random(derive_seed(seed, "materialize", f"{role}/pack/material"))
        boundary = rng.random() * math.fsum(weights[name] for name in common)
        cumulative = 0.0
        for name in common:
            cumulative += weights[name]
            if boundary < cumulative:
                return name, source
        return common[-1], source
    return None, {"source": "d28", "reason": "no_common_material"}


def relation_checks(selected: Mapping, rules: Sequence, chain: Sequence,
                    settings: Mapping) -> tuple[list[dict], list[dict], set[frozenset]]:
    """実ブロック中央値のpack関係を評価し、違反・適用記録・対象pairを返す。"""
    pairs = sorted({tuple(sorted(row["pair"])) for row in rules})
    errors, checks, covered = [], [], set()
    for pair in pairs:
        if not all(role in selected for role in pair):
            continue
        rule, source = lookup(rules, chain, pair)
        if rule is None:
            continue
        first, second = rule["pair"]
        covered.add(frozenset(pair))
        if not selected[first][2] or not selected[second][2]:
            record = {**source, "pair": [first, second], "metric": "color_unavailable",
                      "applied": False, "reason": "color_unavailable"}
            checks.append(record)
            errors.append({**record, "code": "palette_relation", "violation": 1.0})
            continue
        a, b = selected[first][2][2], selected[second][2][2]
        ca, cb = math.hypot(a[1], a[2]), math.hypot(b[1], b[2])
        actuals = {"dL": b[0] - a[0], "dC": cb - ca,
                   "dh": (math.atan2(b[2], b[1]) - math.atan2(a[2], a[1])
                          - rule["dh"]["origin"] + math.pi) % (2 * math.pi) - math.pi}
        for metric, actual in actuals.items():
            record: dict[str, Any] = {**source, "pair": [first, second], "metric": metric}
            if metric == "dh" and min(ca, cb) <= 0.02:
                checks.append({**record, "applied": False, "reason": "achromatic"})
                continue
            values = rule[metric]["quantiles"] if metric == "dh" else rule[metric]
            center = (values[0] + values[-1]) / 2
            factor = 1 if metric == "dh" else 0.5 + settings[
                "contrast" if metric == "dL" else "saturation"]
            low = center + (values[0] - center) * factor
            high = center + (values[-1] - center) * factor
            violation = max(low - actual, actual - high, 0)
            record.update(applied=True, actual=actual, minimum=low, maximum=high)
            checks.append(record)
            if violation:
                extent = {"dL": 2, "dC": 2 * math.sqrt(0.32), "dh": 2 * math.pi}[metric]
                errors.append({**record, "code": "palette_relation",
                               "violation": violation / (high - low or extent)})
    return errors, checks, covered
