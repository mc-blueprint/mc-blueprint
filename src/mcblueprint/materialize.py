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


def _violations(
    selected: dict[str, tuple], contrast: float, saturation: float,
) -> list[dict[str, Any]]:
    errors = []
    pairs = (("wall_main", "roof"), ("wall_main", "frame"), ("wall_main", "wall_secondary"))
    for first, second in pairs:
        if first not in selected or second not in selected:
            continue
        if not selected[first][2] or not selected[second][2]:
            errors.append({"code": "palette_relation", "pair": [first, second],
                           "metric": "color_unavailable", "violation": 1.0})
            continue
        if second == "wall_secondary":
            continue
        a, b = selected[first][2][2], selected[second][2][2]
        difference = abs(a[0] - b[0])
        minimum = 0.04 + 0.24 * contrast
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
        if distance > maximum:
            errors.append({"code": "palette_relation", "pair": ["wall_main", "wall_secondary"],
                           "metric": "dC", "actual": distance, "maximum": maximum,
                           "violation": distance - maximum})
        if min(ca, cb) > 0.02:
            hue = abs(math.atan2(a[2], a[1]) - math.atan2(b[2], b[1]))
            hue = min(hue, 2 * math.pi - hue)
            if hue > math.pi / 3:
                errors.append({"code": "palette_relation",
                               "pair": ["wall_main", "wall_secondary"], "metric": "dh",
                               "actual": hue, "maximum": math.pi / 3,
                               "violation": hue - math.pi / 3})
    return errors


def materialize(
    blueprint: Mapping[str, Any], *, style: str | Mapping[str, Any] = "medieval",
    biome: str = "plains", seed: int | None = None,
    palette: Mapping[str, Any] | None = None,
) -> MaterializeResult:
    """計画JSONの役割参照を解決し、既存loaderが読める具体JSONを返す。

    入力計画と設定はschema検証済みを前提とする。
    paletteはResolve結果のconfig['palette']（materialsを含む）。styleは同梱名か
    検査対象の様式データ。未知役割・形状不足・状態不整合はBlueprintError。
    異なる役割または役割参照と具体値を混在したPaletteは未対応として拒否する。
    色関係が解けない場合は20回以内の最小違反解と診断を返す。固定IDは再抽選しない。
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
        required.setdefault(role, {}).setdefault(shape, []).append(ref["state"])
    chosen_seed = blueprint.get("seed", 0) if seed is None else seed
    output["seed"] = chosen_seed
    candidates: dict[str, list[tuple]] = {}
    fixed: set[str] = set()
    material_names = {info.material for info in data.blocks()}
    for role, shapes in sorted(required.items()):
        request = materials[role]
        color = request.get("color") if isinstance(request, dict) else None
        choices = request.get("material", style.get("roles", {}).get(role, [])) \
            if isinstance(request, dict) else request
        choices = choices if isinstance(choices, list) else [choices]
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
    for attempt in range(20):
        attempts += 1
        for role, options in candidates.items():
            if attempt and role not in retry_roles:
                continue
            if attempt == 0 or role in fixed or settings["variation"] == 0:
                index = 0
            else:
                rng = random.Random(derive_seed(chosen_seed, "materialize", f"{role}/{attempt}"))
                width = max(1, math.ceil(len(options) * settings["variation"]))
                index = rng.randrange(width)
            selected[role] = options[index]
        errors = _violations(selected, settings["contrast"], settings["saturation"])
        score = len(errors), sum(e["violation"] for e in errors)
        if score < best_score:
            best, best_errors, best_score = dict(selected), errors, score
        if not errors:
            break
        retry_roles = {role for error in errors for role in error["pair"]} - fixed
    roles = {role: item[1] for role, item in best.items()}
    palettes = output["palettes"] = {}

    palette_names = {}

    def resolve(value: Any) -> Any:
        if _reference(value):
            role, shape = value["role"], value["shape"]
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
            key = role, shape, tuple(sorted(value["state"].items()))
            if key not in palette_names:
                name = f"materialize_{role}_{shape}_{len(palettes)}"
                while name in palettes:
                    name += "_"
                palette_names[key] = name
                palettes[name] = entries
            name = palette_names[key]
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
    return MaterializeResult(output, roles, diagnostics, attempts,
                             {"accent": 0.08 * settings["accent"]}, frozenset(fixed))
