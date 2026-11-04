"""役割参照を形状別のブロックと混色Paletteへ解決する。"""

from __future__ import annotations

import copy
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mcblueprint import components
from mcblueprint.blockdata import BlockData, BlockInfo, load_block_data
from mcblueprint.colorpack import ColorPack
from mcblueprint.colorpack_selection import (
    conditions,
    lookup,
    material_choice,
    relation_checks,
    target_color,
)
from mcblueprint.colors import face_linear, linear_to_oklab
from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState
from mcblueprint.operations.base import derive_seed
from mcblueprint.operations.registry import build_operation
from mcblueprint.traversal import FULL_BLOCKS

ATTRIBUTES = ("id", "role", "element", "owner", "source", "face", "reserved", "onConflict")
EXPAND_TYPES = {"building", "room", "roof", "stairs", "tower", "spiral_stairs", "arch", "pillar"}
WALK_ROLES = {"floor", "path", "stairs"}


@dataclass(frozen=True)
class MaterializeResult:
    """loaderへ渡す具体JSON、役割×形状表、評価用診断と試行回数。入力は変更しない。"""

    blueprint: dict[str, Any]
    roles: dict[str, dict[str, BlockState]]
    diagnostics: tuple[dict[str, Any], ...]
    attempts: int
    max_area_ratios: dict[str, float] = field(default_factory=dict)
    fixed_roles: frozenset[str] = frozenset()
    selection_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    relation_checks: tuple[dict[str, Any], ...] = ()
    building_results: dict[str, MaterializeResult] = field(default_factory=dict)


def _reference(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"role", "shape", "state"}


def _references(value: Any):
    if _reference(value):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if key not in ("metadata", "state"):
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _walking_shapes(
    operations: list[dict[str, Any]], palettes: Mapping[str, Any],
) -> set[tuple[str, str]]:
    shapes = set()
    for op in operations:
        slots = {
            "building": ("floor", "stairs"), "room": ("floor",),
            "tower": ("floor", "wall", "stairs.block"),
            "floor": ("block",),
            "stairs": ("block",), "spiral_stairs": ("block",), "path": ("block",),
        }.get(op.get("type"), ())
        for slot in slots:
            value = op
            for key in slot.split("."):
                value = value.get(key) if isinstance(value, Mapping) else None
            if isinstance(value, dict) and "palette" in value:
                value = palettes.get(value["palette"], [])
            for ref in _references(value):
                shapes.add((ref["role"], ref["shape"]))
        if "block" in slots and "palette" in op:
            for ref in _references(palettes.get(op["palette"], [])):
                shapes.add((ref["role"], ref["shape"]))
        shapes.update(_walking_shapes(op.get("operations", []), palettes))
    return shapes


def _uses_palettes(value: Any, names: frozenset[str]) -> bool:
    if not names:
        return False
    if isinstance(value, dict):
        if value.get("type") == "component":
            raw = components.load_component(value["name"], raw=True)
            with components.loading(value["name"]):
                return _uses_palettes(raw["operations"], names.difference(raw.get("palettes", {})))
        return any(
            child in names if key == "palette" else _uses_palettes(child, names)
            for key, child in value.items() if key not in ("metadata", "state")
        )
    if isinstance(value, list):
        return any(_uses_palettes(child, names) for child in value)
    return False


def _inline_components(
    operations: list[dict[str, Any]], palettes: dict[str, Any],
    *, inherit: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    result = []
    for operation in operations:
        op = copy.deepcopy(operation)
        if "operations" in op:
            op["operations"] = _inline_components(op["operations"], palettes, inherit=inherit)
        if op.get("type") == "component":
            raw = components.load_component(op["name"], raw=True)
            local = copy.deepcopy(raw.get("palettes", {}))
            inherited = inherit.difference(local)
            with components.loading(op["name"]):
                uses_inherited = _uses_palettes(raw["operations"], inherited)
                children = _inline_components(raw["operations"], local, inherit=inherited)
            if uses_inherited or list(_references([children, local])):
                # 改名対象のPaletteを実際に継承する子部品だけを展開する。
                with components.loading(op["name"]):
                    children = _inline_components(children, local, inherit=inherit.union(local))
                names = {}
                for name in local:
                    renamed = f"materialize_component_{len(palettes)}_{name}"
                    while renamed in palettes:
                        renamed += "_"
                    names[name] = renamed
                    palettes[renamed] = local[name]

                def rename(value, names=names):
                    if isinstance(value, dict):
                        return {key: names.get(child, child) if key == "palette" else rename(child)
                                for key, child in value.items()}
                    if isinstance(value, list):
                        return [rename(child) for child in value]
                    return value

                children = rename(children)
                op = {"type": "translate", "offset": op["position"],
                      **({"rotation": op["rotation"]} if op.get("rotation", 0) else {}),
                      "operations": children,
                      **{key: op[key] for key in ATTRIBUTES if key in op}}
        result.append(op)
    return result


def _expand(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, operation in enumerate(operations):
        op = copy.deepcopy(operation)
        if "operations" in op:
            op["operations"] = _expand(op["operations"])
        if op.get("type") in EXPAND_TYPES and list(_references(op)):
            model = build_operation(op, f"operations[{index}]")
            children = _expand(model.expand())
            if op["type"] == "building":
                if op.get("angle", 0):
                    children = [{"type": "rotate", "center": [0, 0, 0],
                                 "angle": op["angle"], "operations": children}]
                offset = op["position"]
            else:
                offset = [0, 0, 0]
            op = {"type": "translate", "offset": offset, "operations": children,
                  **{key: op[key] for key in ATTRIBUTES if key in op}}
        result.append(op)
    return result


def _colors(
    info: BlockInfo, data: BlockData, face: str, biome: str,
) -> tuple[tuple[float, ...], ...]:
    if face not in info.faces:
        raise BlueprintError(f"{info.id}: {face}の面色がありません")
    try:
        return tuple(linear_to_oklab(face_linear(info, info.faces[face], data, biome, q))
                     for q in range(5))
    except KeyError as exc:
        raise BlueprintError(f"{info.id}: biome {biome!r} の着色値がありません") from exc


def _distance(colors: tuple[tuple[float, ...], ...], target: tuple[float, ...]) -> float:
    return sum(sum((a - b) ** 2 for a, b in zip(color, target, strict=True))
               for color in colors) / len(colors)


def _d28_relations(
    selected: dict[str, tuple], contrast: float, saturation: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    errors, checks = [], []
    pairs = (("wall_main", "roof"), ("wall_main", "frame"), ("wall_main", "wall_secondary"))
    for first, second in pairs:
        if first not in selected or second not in selected:
            continue
        if not selected[first][2] or not selected[second][2]:
            checks.append({"source": "d28", "pair": [first, second],
                           "metric": "color_unavailable", "applied": False,
                           "reason": "color_unavailable"})
            errors.append({"code": "palette_relation", "pair": [first, second],
                           "metric": "color_unavailable", "violation": 1.0})
            continue
        if second == "wall_secondary":
            continue
        a, b = selected[first][2][2], selected[second][2][2]
        difference = abs(a[0] - b[0])
        minimum = 0.04 + 0.24 * contrast
        checks.append({"source": "d28", "pair": [first, second], "metric": "dL",
                       "applied": True, "actual": difference, "minimum": minimum,
                       "maximum": 1.0})
        if difference < minimum:
            errors.append({"code": "palette_relation", "pair": [first, second],
                           "metric": "dL", "actual": difference, "minimum": minimum,
                           "violation": minimum - difference})
    if all(role in selected and selected[role][2] for role in ("wall_main", "wall_secondary")):
        a = selected["wall_main"][2][2]
        b = selected["wall_secondary"][2][2]
        ca, cb = math.hypot(a[1], a[2]), math.hypot(b[1], b[2])
        distance = abs(ca - cb)
        maximum = 0.04 + 0.12 * saturation
        checks.append({"source": "d28", "pair": ["wall_main", "wall_secondary"],
                       "metric": "dC", "applied": True, "actual": distance,
                       "minimum": 0.0, "maximum": maximum})
        if distance > maximum:
            errors.append({"code": "palette_relation", "pair": ["wall_main", "wall_secondary"],
                           "metric": "dC", "actual": distance, "maximum": maximum,
                           "violation": distance - maximum})
        if min(ca, cb) > 0.02:
            hue = abs(math.atan2(a[2], a[1]) - math.atan2(b[2], b[1]))
            hue = min(hue, 2 * math.pi - hue)
            checks.append({"source": "d28", "pair": ["wall_main", "wall_secondary"],
                           "metric": "dh", "applied": True, "actual": hue,
                           "minimum": 0.0, "maximum": math.pi / 3})
            if hue > math.pi / 3:
                errors.append({"code": "palette_relation",
                               "pair": ["wall_main", "wall_secondary"], "metric": "dh",
                               "actual": hue, "maximum": math.pi / 3,
                               "violation": hue - math.pi / 3})
        else:
            checks.append({"source": "d28", "pair": ["wall_main", "wall_secondary"],
                           "metric": "dh", "applied": False, "reason": "achromatic"})
    return errors, checks


def _violations(
    selected: dict[str, tuple], contrast: float, saturation: float,
) -> list[dict[str, Any]]:
    return _d28_relations(selected, contrast, saturation)[0]


def materialize(
    blueprint: Mapping[str, Any], *, style: str | Mapping[str, Any] = "medieval",
    biome: str = "plains", seed: int | None = None,
    palette: Mapping[str, Any] | None = None,
    colorpack: ColorPack | None = None, building_type: str | None = None,
    element: str = "",
) -> MaterializeResult:
    """計画JSONの役割参照を解決し、既存loaderが読める具体JSONを返す。

    入力計画と設定はschema検証済みを前提とする。
    paletteはResolve結果のconfig['palette']（materialsを含む）。styleは同梱名か
    検査対象の様式データ。未知役割・形状不足・状態不整合はBlueprintError。
    異なる役割または役割参照と具体値を混在したPaletteは未対応として拒否する。
    色関係が解けない場合は20回以内の最小違反解と診断を返す。固定IDは再抽選しない。
    colorpackは検証済みColorPackを直接渡す。自動読込せず、Noneなら従来のD28を使う。
    building_typeはパック条件の建物種別。未知値は種別条件を除いて探索する。
    element指定時は世界seedと棟IDから抽選seedを導出する。単独建物の既存抽選は維持する。
    結果のselection_sourcesは役割の色・材質・面積上限の選択元、relation_checksは
    最終解の関係検査と不適用理由を保持する。
    """
    data = load_block_data(blueprint["minecraftVersion"])
    output = copy.deepcopy(dict(blueprint))
    input_palettes = copy.deepcopy(blueprint.get("palettes", {}))
    prepared = _inline_components(
        blueprint.get("operations", []), input_palettes,
        inherit=frozenset(name for name, entries in input_palettes.items()
                          if list(_references(entries))),
    )
    if not list(_references([prepared, input_palettes])):
        return MaterializeResult(copy.deepcopy(dict(blueprint)), {}, (), 0)
    style_name = style if isinstance(style, str) else style.get("name")
    chain = conditions(style_name, building_type, biome) if colorpack is not None else ()
    if isinstance(style, str):
        path = Path(__file__).parent / "data" / "patterns" / "styles" / f"{style}.yaml"
        if not path.is_file():
            raise BlueprintError(f"未知のstyle: {style!r}")
        style = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings = {"variation": 0.4, "contrast": 0.5, "saturation": 0.4,
                "accent": 0.3, "mix": 0.3, **(palette or {})}
    materials = dict(style.get("roles", {})) | dict(settings.get("materials", {}))
    operations = _expand(prepared)
    walking = _walking_shapes(prepared, input_palettes)
    required: dict[str, dict[str, list[dict[str, str]]]] = {}
    for ref in _references([operations, input_palettes]):
        role, shape = ref["role"], ref["shape"]
        if role not in materials:
            raise BlueprintError(f"Materialize: 未知のrole {role!r}")
        states = required.setdefault(role, {}).setdefault(shape, [])
        if ref["state"] not in states:
            states.append(ref["state"])
    chosen_seed = blueprint.get("seed", 0) if seed is None else seed
    if element:
        chosen_seed = derive_seed(chosen_seed, "materialize", element)
    output["seed"] = chosen_seed
    candidates: dict[str, list[tuple]] = {}
    fixed: set[str] = set()
    material_names = {info.material for info in data.blocks()}
    sources = {}
    max_area_ratios = {"accent": 0.08 * settings["accent"]}
    for role, shapes in sorted(required.items()):
        request = materials[role]
        color = request.get("color") if isinstance(request, dict) else None
        choices = request.get("material", style.get("roles", {}).get(role, [])) \
            if isinstance(request, dict) else request
        choices = choices if isinstance(choices, list) else [choices]
        explicit = settings.get("materials", {}).get(role)
        explicit_material = explicit is not None and (
            not isinstance(explicit, dict) or "material" in explicit)
        role_sources = sources[role] = {
            "material": {"source": "explicit" if explicit_material else "d28"},
            "color": {"source": "explicit" if color is not None else "d28"},
        }
        if colorpack is not None:
            if not explicit_material:
                material, role_sources["material"] = material_choice(
                    colorpack.material_prior, chain, role, choices, chosen_seed)
                if material is not None:
                    choices = [material]
            if color is None:
                distribution, role_sources["color"] = lookup(colorpack.palette_prior, chain, role)
                if distribution is not None:
                    color = target_color(distribution, settings, chosen_seed, role)
            rule, source = lookup(colorpack.accent_rule, chain, role)
            if rule is not None or role == "accent":
                role_sources["maxAreaRatio"] = source
                if rule is not None:
                    max_area_ratios[role] = rule["maxAreaRatio"] * settings["accent"]
        face = "top" if role in ("floor", "path", "roof") or any(
            (role, shape) in walking for shape in shapes
        ) else "side"
        pools = []
        specified = None
        for choice in choices:
            parsed = BlockState.parse(choice)
            if choice not in material_names and parsed.id in data:
                if len(choices) != 1:
                    raise BlueprintError(f"materials.{role}: 固定IDは単独で指定してください")
                error = data.check_state(parsed)
                if error:
                    raise BlueprintError(f"materials.{role}: 固定IDの状態が不正です: {error}")
                specified = parsed
                fixed.add(role)
                role_sources.update(material={"source": "fixed_id"},
                                    color={"source": "fixed_id"})
                info = data.get(parsed.id)
                shapes_map = dict(data.families.get(info.family, {}))
                shapes_map[info.shape] = info.id
                pools.append((info.id, shapes_map, info))
            elif ":" in choice or "[" in choice:
                raise BlueprintError(f"materials.{role}: 未知のブロックID {choice!r}")
            else:
                for info in data.blocks():
                    if info.material != choice or info.shape != "full":
                        continue
                    shapes_map = dict(data.families.get(info.family, {}))
                    shapes_map[info.shape] = info.id
                    pools.append((info.id, shapes_map, info))
        eligible = []
        for name, shapes_map, info in pools:
            if not set(shapes) <= shapes_map.keys():
                continue
            table = {}
            valid = True
            for shape, states in shapes.items():
                state = specified if specified and data.get(specified.id).shape == shape \
                    else BlockState(shapes_map[shape])
                if data.check_state(state):
                    valid = False
                    break
                for properties in states:
                    merged = state
                    for key, value in properties.items():
                        if state.get(key) is not None and state.get(key) != value:
                            valid = False
                        merged = merged.with_property(key, value)
                    if data.check_state(merged):
                        valid = False
                    if role in WALK_ROLES or (role, shape) in walking:
                        complete = data.complete(merged)
                        if shape == "full":
                            valid &= state.id.removeprefix("minecraft:") in FULL_BLOCKS
                        elif shape == "stairs":
                            valid &= all(complete.get(k) == v for k, v in (
                                ("half", "bottom"), ("shape", "straight"),
                                ("waterlogged", "false")))
                        elif shape == "slab":
                            valid &= complete.get("type") == "double"
                        else:
                            valid = False
                table[shape] = state
            if valid and (info.faces or specified):
                color_shape = "full" if "full" in shapes else (
                    "stairs" if "stairs" in shapes else min(shapes))
                color_info = data.get(table[color_shape].id)
                if face not in color_info.faces and not specified:
                    continue
                colors = _colors(color_info, data, face, biome) if face in color_info.faces else ()
                eligible.append((name, table, colors))
        if not eligible:
            raise BlueprintError(
                f"materials.{role}: 必要形状・状態・歩行条件を満たす候補がありません "
                f"({', '.join(sorted(shapes))})")
        if role in fixed:
            candidates[role] = eligible
            continue
        eligible.sort(key=lambda c: c[0])
        if color is None:
            medians = sorted(c[2][2] for c in eligible)
            rng = random.Random(derive_seed(chosen_seed, "materialize", role))
            q = 0.5 + (rng.random() - 0.5) * settings["variation"]
            base = medians[round(q * (len(medians) - 1))]
            factor = 0.5 + settings["saturation"]
            if role == "accent":
                factor *= 1 + settings["accent"]
            color = (base[0], base[1] * factor, base[2] * factor)
        target = tuple(color)
        eligible.sort(key=lambda c: (_distance(c[2], target), c[0]))
        candidates[role] = eligible
    best, best_errors, best_score = {}, [], (math.inf, math.inf)
    attempts = 0
    selected = {}
    retry_roles = set(candidates)
    failed_pairs = set()
    pack_relations = colorpack is not None and any(
        row["when"] in chain and all(role in candidates for role in row["pair"])
        for row in colorpack.relation_rule)

    def violations(selected):
        fallback = _violations(selected, settings["contrast"], settings["saturation"])
        if colorpack is None:
            return fallback
        errors, _, covered = relation_checks(selected, colorpack.relation_rule, chain, settings)
        return errors + [{**error, "source": "d28", "violation": error["violation"] / (
            (1 - error["minimum"] if "minimum" in error else error.get("maximum", 1))
            if pack_relations else 1)}
                         for error in fallback
                         if frozenset(error["pair"]) not in covered]

    for attempt in range(20):
        attempts += 1
        for role, options in candidates.items():
            if attempt and role not in retry_roles:
                continue
            if attempt == 0 or role in fixed or (not pack_relations and settings["variation"] == 0):
                index = 0
            else:
                rng = random.Random(derive_seed(chosen_seed, "materialize", f"{role}/{attempt}"))
                options = sorted(options, key=lambda option: sum(
                    (role, option[0], other, item[0]) in failed_pairs
                    for other, item in selected.items() if other != role
                ))
                ranked = []
                for option in options:
                    errors = violations({**selected, role: option})
                    ranked.append(((len(errors), sum(e["violation"] for e in errors)),
                                   option))
                ranked.sort(key=lambda item: (item[0], item[1][0]) if pack_relations
                            else (item[0],))
                options = [option for _, option in ranked]
                width = min(max(1, math.ceil(len(options) * settings["variation"])),
                            sum(score == ranked[0][0] for score, _ in ranked))
                index = rng.randrange(width)
            selected[role] = options[index]
        errors = violations(selected)
        score = len(errors), sum(e["violation"] for e in errors)
        if pack_relations:
            score = (*score, tuple(selected[role][0] for role in sorted(selected)))
        if score < best_score:
            best, best_errors, best_score = dict(selected), errors, score
        if not errors:
            break
        for error in errors:
            first, second = error["pair"]
            failed_pairs.add((first, selected[first][0], second, selected[second][0]))
            failed_pairs.add((second, selected[second][0], first, selected[first][0]))
        retry_roles = {role for error in errors for role in error["pair"]} - fixed
    roles = {role: item[1] for role, item in best.items()}
    palettes = output["palettes"] = {}

    palette_names = {}

    def resolve(value: Any) -> Any:
        if _reference(value):
            role, shape = value["role"], value["shape"]
            palette_key = role, shape, tuple(sorted(value["state"].items()))
            if palette_key in palette_names:
                return {"palette": palette_names[palette_key]}
            state = roles[role][shape]
            for key, item in value["state"].items():
                state = state.with_property(key, item)
            mix = 0 if role in fixed else settings["mix"]
            if role == "accent":
                mix *= settings["accent"]
            if not mix or len(candidates[role]) == 1:
                return state.to_string()
            alternatives = [item for item in candidates[role] if item[0] != best[role][0]]
            alternatives.sort(key=lambda c: (_distance(c[2], best[role][2][2]), c[0]))
            secondary = alternatives[0][1][shape]
            for key, item in value["state"].items():
                secondary = secondary.with_property(key, item)
            entries = [entry for entry in (
                {"block": state.to_string(), "weight": 1 - mix},
                {"block": secondary.to_string(), "weight": mix},
            ) if entry["weight"] > 0]
            name = f"materialize_{role}_{shape}_{len(palettes)}"
            while name in palettes:
                name += "_"
            palette_names[palette_key] = name
            palettes[name] = entries
            return {"palette": name}
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                converted = resolve(child) if key != "metadata" else copy.deepcopy(child)
                if key == "block" and _reference(child) and "type" in value:
                    result.setdefault("role", child["role"])
                if key == "palette" and child in palette_roles and "type" in value:
                    result.setdefault("role", palette_roles[child])
                if key == "block" and _reference(child) and isinstance(converted, dict):
                    result.update(converted)
                else:
                    result[key] = converted
            return result
        if isinstance(value, list):
            return [resolve(child) for child in value]
        return value

    palette_roles = {}
    for name, entries in input_palettes.items():
        entry_roles = {entry["block"]["role"] if _reference(entry["block"]) else None
                       for entry in entries}
        if len(entry_roles) > 1:
            raise BlueprintError(
                f"palettes.{name}: 異なる役割または役割参照と具体ブロックを"
                "混在したPaletteは未対応です"
            )
        if len(entry_roles) == 1 and None not in entry_roles:
            palette_roles[name] = next(iter(entry_roles))
        concrete = []
        for entry in entries:
            if _reference(entry["block"]):
                state = roles[entry["block"]["role"]][entry["block"]["shape"]]
                for key, item in entry["block"]["state"].items():
                    state = state.with_property(key, item)
                concrete.append({**entry, "block": state.to_string()})
            else:
                concrete.append(copy.deepcopy(entry))
        palettes[name] = concrete
    output["operations"] = resolve(operations)
    diagnostics = tuple({**e, "attempts": attempts,
                         "candidates": {r: len(candidates[r]) for r in e["pair"]}}
                        for e in best_errors)
    checks = []
    covered = set()
    if colorpack is not None:
        _, checks, covered = relation_checks(best, colorpack.relation_rule, chain, settings)
    _, fallback_checks = _d28_relations(best, settings["contrast"], settings["saturation"])
    checks.extend(check for check in fallback_checks if frozenset(check["pair"]) not in covered)
    return MaterializeResult(output, roles, diagnostics, attempts,
                             max_area_ratios, frozenset(fixed), sources, tuple(checks))


def materialize_plan(blueprint: Mapping[str, Any], **kwargs) -> MaterializeResult:
    """階層計画の各棟へ世界seed・棟ID・種別を渡す。単独棟の抽選は従来と同一。

    複数棟ではpalette名を棟ごとに分離し、診断・選択元を棟ID付きで保持する。
    固定役割はbuilding_resultsの各棟に保持し、全体のfixed_rolesには集約しない。
    kwargsはmaterializeと同じ。計画外の敷地Operationも既存の役割APIで解決する。
    """
    from mcblueprint.plan_contract import plan_buildings

    buildings = plan_buildings(blueprint["metadata"]["planning"])
    if len(buildings) <= 1:
        return materialize(blueprint, **kwargs)
    output = copy.deepcopy(dict(blueprint))
    output["operations"] = [None] * len(blueprint["operations"])
    output["palettes"] = {}
    if kwargs.get("seed") is not None:
        output["seed"] = kwargs["seed"]
    results = {}
    roots = {b["element"] for b in buildings}
    operations = blueprint["operations"]
    groups = [(b["element"], b["archetype"],
               [(index, op) for index, op in enumerate(operations)
                if op.get("element") == b["element"]])
              for b in buildings]
    groups.append(("site#1", kwargs.get("building_type"),
                   [(index, op) for index, op in enumerate(operations)
                    if op.get("element") not in roots]))
    for root, kind, entries in groups:
        if not entries:
            if root in roots:
                raise BlueprintError(f"Materialize: 棟のOperationがありません: {root}")
            continue
        result = materialize({**blueprint, "operations": [op for _, op in entries]},
                             **{**kwargs, "building_type": kind, "element": root})
        prefix = root.replace("#", "_")
        names = {name: f"{prefix}_{name}" for name in result.blueprint.get("palettes", {})}

        def rename(value, names=names):
            if isinstance(value, list):
                return [rename(child) for child in value]
            if isinstance(value, dict):
                return {key: names[child] if key == "palette" else rename(child)
                        for key, child in value.items()}
            return value

        for (index, _), op in zip(entries, result.blueprint["operations"], strict=True):
            output["operations"][index] = rename(op)
        output["palettes"].update({names[name]: entries for name, entries
                                   in result.blueprint.get("palettes", {}).items()})
        results[root] = result
    return MaterializeResult(
        output, {}, tuple({**d, "element": root} for root, r in results.items()
                          for d in r.diagnostics), max(r.attempts for r in results.values()),
        {}, frozenset(),
        {root: r.selection_sources for root, r in results.items()},
        tuple({**check, "element": root} for root, r in results.items()
              for check in r.relation_checks), results,
    )
