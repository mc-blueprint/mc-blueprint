"""Blueprint validation: JSON Schema first, then semantic checks (docs/ARCHITECTURE.md §8)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

from mcblueprint import blockdata, components
from mcblueprint.errors import BlueprintError, ValidationError
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import MAX_DEPTH
from mcblueprint.schema import load_component_schema, load_schema
from mcblueprint.volume import MAX_VOLUME

DEFAULT_MAX_DIMENSION = 1024

PLACEMENT_TYPES = frozenset(
    {
        "set",
        "fill",
        "box",
        "wall",
        "floor",
        "line",
        "circle",
        "cylinder",
        "sphere",
        "replace",
        "stairs",
        "spiral_stairs",
        "roof",
        "pillar",
        "arch",
        "gate",
        "path",
    }
)
NESTED_TYPES = frozenset({"mirror", "repeat", "translate", "rotate"})
SAME_Y_TYPES = frozenset({"wall", "floor", "roof", "garden", "landform"})
# optional single-block keys of high-level operations, validated like ``block``
# (``a.b`` reaches into a nested object such as ``doorway.arch``)
EXTRA_BLOCK_KEYS: dict[str, tuple[str, ...]] = {
    "building": ("wall", "floor", "roof", "stairs", "window", "door"),
    "join_buildings": ("wall",),
    "stairs": ("base",),
    "spiral_stairs": ("column",),
    "roof": ("gable", "ridgeBlock"),
    "pillar": ("base", "cap"),
    "doorway": ("door",),
    "window": ("block",),
    "arch": ("trim", "fill"),
    "tower": ("windows.block", "door.block"),
    "bridge": ("stairs", "railing"),
    "gate": ("door", "portcullis.block"),
    "garden": ("crop", "fence", "gate.block", "lanterns.block"),
    "path": ("edge", "stairs", "lights.block"),
}
# keys holding a block string or a ``{"block"}`` / ``{"palette"}`` object (``arch``
# objects also carry ``trim``); ``a.b`` reaches into a nested object
SPEC_KEYS: dict[str, tuple[str, ...]] = {
    "doorway": ("arch",),
    "window": ("arch",),
    "room": ("wall", "floor", "ceiling", "corners"),
    "tower": ("wall", "floor", "stairs.block", "battlement.block", "windows.arch", "door.arch"),
    "bridge": ("deck", "piers.block"),
    "garden": ("ground", "plants"),
    "landform": ("surface", "subsurface"),
    "gate": (
        "arch",
        "battlement.block",
        "towers.wall",
        "towers.floor",
        "towers.stairs.block",
        "towers.battlement.block",
        "towers.windows.arch",
        "towers.door.arch",
    ),
}
# ``room`` openings: lists of objects with their own block keys and ``arch``
ROOM_OPENING_KEYS = {"doors": ("door",), "windows": ("block",)}


def validate(
    data: Mapping[str, Any], *, max_dimension: int = DEFAULT_MAX_DIMENSION,
    base_dir: Path | None = None,
) -> list[ValidationError]:
    """検証結果を返す。base_dirは地形sourceの相対パス基準（既定cwd）。"""
    return validate_and_load(data, max_dimension=max_dimension, base_dir=base_dir)[1]


def validate_and_load(
    data: Mapping[str, Any], *, max_dimension: int = DEFAULT_MAX_DIMENSION,
    base_dir: Path | None = None, terrain_snapshot=None,
) -> tuple[Blueprint | None, list[ValidationError]]:
    """全段階を検証し、成功時は検証に使った同じモデルと空のエラー列を返す。

    失敗時のモデルはNone。base_dirは地形sourceの相対パス基準（既定cwd）。
    地形は一度だけ読み取り、生成時まで不変snapshotとして保持する。
    terrain_snapshotはSiteで読取済みの地形を引き継ぐ内部パイプライン用。
    """
    errors = validate_schema(data)
    if errors:
        return None, errors
    errors = _validate_semantics(data)
    if errors:
        return None, errors
    try:
        blueprint = load_blueprint_dict(data, base_dir=base_dir, terrain_snapshot=terrain_snapshot)
    except BlueprintError as exc:
        return None, [ValidationError("", f"{exc}.")]
    errors = _validate_bounds(blueprint, max_dimension)
    return (None if errors else blueprint), errors


def validate_schema(data: Any, *, component: bool = False) -> list[ValidationError]:
    schema = load_component_schema() if component else load_schema()
    validator = Draft202012Validator(schema)
    errors = [_convert_schema_error(err, data) for err in validator.iter_errors(data)]
    return _dedupe(sorted(errors, key=lambda e: e.path))


def format_errors(errors: Sequence[ValidationError]) -> str:
    if not errors:
        return "Blueprint is valid."
    blocks = [err.format() for err in errors]
    noun = "error" if len(errors) == 1 else "errors"
    return "\n\n".join(blocks) + f"\n\n{len(errors)} {noun} found."


# --- schema -----------------------------------------------------------------


def _convert_schema_error(err: SchemaError, data: Any) -> ValidationError:
    path = _format_path(err.absolute_path)
    op_type = _op_type_for_path(data, list(err.absolute_path))
    value = err.instance if isinstance(err.instance, str | int | float | bool) else None
    return ValidationError(path, _schema_message(err), op_type, value, "schema")


def _schema_message(err: SchemaError) -> str:
    key = err.absolute_path[-1] if err.absolute_path else None
    name = f"'{key}'" if isinstance(key, str) else "value"
    v = err.validator_value
    match err.validator:
        case "required":
            missing = [k for k in v if k not in err.instance]
            return f"Missing required {_plural('property', missing)}: {', '.join(missing)}."
        case "additionalProperties":
            extra = sorted(set(err.instance) - set(err.schema.get("properties", {})))
            return f"Unknown {_plural('property', extra)}: {', '.join(extra)}."
        case "minimum":
            return f"{name} must be >= {v}."
        case "maximum":
            return f"{name} must be <= {v}."
        case "exclusiveMinimum":
            return f"{name} must be > {v}."
        case "const":
            return f"{name} must be {v!r}."
        case "enum":
            return f"{name} must be one of: {', '.join(repr(x) for x in v)}."
        case "type":
            return f"{name} must be of type {v}."
        case "pattern":
            return f"{name} does not match the required format."
        case "minItems":
            return f"{name} must contain at least {v} item{'s' if v != 1 else ''}."
        case "maxItems":
            return f"{name} must contain at most {v} item{'s' if v != 1 else ''}."
        case "minLength":
            return f"{name} must not be empty."
        case "multipleOf":
            return f"{name} must be a multiple of {v}."
        case _:
            return err.message.rstrip(".") + "."


def _plural(word: str, items: Sequence[Any]) -> str:
    return word if len(items) == 1 else word[:-1] + "ies"


def _format_path(parts: Iterable[str | int]) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out


def _op_type_for_path(data: Any, parts: list[str | int]) -> str | None:
    """The ``type`` of the innermost operation that contains ``parts``."""
    node = data
    op_type = None
    for i, part in enumerate(parts):
        try:
            node = node[part]
        except (KeyError, IndexError, TypeError):
            break
        if (
            isinstance(part, int)
            and i > 0
            and parts[i - 1] == "operations"
            and isinstance(node, dict)
        ):
            t = node.get("type")
            if isinstance(t, str):
                op_type = t
    return op_type


def _dedupe(errors: list[ValidationError]) -> list[ValidationError]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for err in errors:
        key = (err.path, err.message)
        if key not in seen:
            seen.add(key)
            unique.append(err)
    return unique


# --- semantics ----------------------------------------------------------------


def _validate_semantics(data: Mapping[str, Any]) -> list[ValidationError]:
    errors: list[ValidationError] = []
    version = data["minecraftVersion"]
    if not blockdata.is_supported(version):
        supported = ", ".join(blockdata.supported_versions())
        errors.append(
            ValidationError(
                "minecraftVersion",
                f"Unsupported Minecraft version. Supported versions: {supported}. "
                "Use --minecraft-version to check against one of them.",
                None,
                version,
            )
        )
        return errors
    blocks = blockdata.load_block_data(version)

    palettes = data.get("palettes", {})
    for name, entries in palettes.items():
        for index, entry in enumerate(entries):
            _check_block(entry["block"], f"palettes.{name}[{index}].block", None, blocks, errors)

    _walk_operations(data["operations"], "operations", 1, palettes, blocks, errors)
    return errors


def _walk_operations(
    ops: Sequence[Mapping[str, Any]],
    path: str,
    depth: int,
    palettes: Mapping[str, Any],
    blocks: blockdata.BlockData,
    errors: list[ValidationError],
) -> None:
    for index, op in enumerate(ops):
        op_path = f"{path}[{index}]"
        op_type = op["type"]
        if op_type == "terrain_density" and op["minecraftVersion"] != blocks.version:
            errors.append(ValidationError(
                f"{op_path}.minecraftVersion",
                "地形OperationとBlueprintの版が不一致です。", op_type,
            ))
        if op_type in PLACEMENT_TYPES:
            has_block, has_palette = "block" in op, "palette" in op
            if has_block == has_palette:
                errors.append(
                    ValidationError(
                        op_path, "Exactly one of 'block' or 'palette' must be given.", op_type
                    )
                )
            if has_block:
                _check_block(op["block"], f"{op_path}.block", op_type, blocks, errors)
            if has_palette and op["palette"] not in palettes:
                errors.append(
                    ValidationError(
                        f"{op_path}.palette", "Unknown palette name.", op_type, op["palette"]
                    )
                )
        for key in EXTRA_BLOCK_KEYS.get(op_type, ()):
            value = _nested_get(op, key)
            if value is not None:
                _check_block(value, f"{op_path}.{key}", op_type, blocks, errors)
        for key in SPEC_KEYS.get(op_type, ()):
            _check_spec(_nested_get(op, key), f"{op_path}.{key}", op_type, palettes, blocks, errors)
        if op_type == "join_buildings":
            _walk_operations(
                op["buildings"], f"{op_path}.buildings", depth, palettes, blocks, errors
            )
        if op_type == "room":
            _check_room(op, op_path, palettes, blocks, errors)
        if op_type == "replace":
            patterns = op["match"] if isinstance(op["match"], list) else [op["match"]]
            for index, pattern in enumerate(patterns):
                suffix = f"[{index}]" if isinstance(op["match"], list) else ""
                _check_block(pattern, f"{op_path}.match{suffix}", op_type, blocks, errors)
        if op_type in SAME_Y_TYPES and op["from"][1] != op["to"][1]:
            errors.append(
                ValidationError(
                    op_path,
                    "'from' and 'to' must have the same y coordinate.",
                    op_type,
                    f"from.y={op['from'][1]}, to.y={op['to'][1]}",
                )
            )
        if op_type == "component":
            _walk_component(op, op_path, depth, palettes, blocks, errors)
        if op_type in NESTED_TYPES:
            if depth + 1 > MAX_DEPTH:
                errors.append(
                    ValidationError(
                        f"{op_path}.operations",
                        f"Operations are nested deeper than {MAX_DEPTH} levels.",
                        op_type,
                    )
                )
            else:
                _walk_operations(
                    op["operations"], f"{op_path}.operations", depth + 1, palettes, blocks, errors
                )


def _walk_component(
    op: Mapping[str, Any],
    op_path: str,
    depth: int,
    palettes: Mapping[str, Any],
    blocks: blockdata.BlockData,
    errors: list[ValidationError],
) -> None:
    """Validate the referenced component file and walk its operations."""
    name = op["name"]
    try:
        raw = components.load_component(name)
    except BlueprintError as exc:
        errors.append(ValidationError(op_path, f"{exc}.", "component", name))
        return
    prefix = f"{op_path}<{name}>"
    schema_errors = validate_schema(raw, component=True)
    for err in schema_errors:
        path = f"{prefix}.{err.path}" if err.path else prefix
        errors.append(
            ValidationError(path, err.message, err.op_type or "component", err.value, err.kind)
        )
    if schema_errors:
        return
    if depth + 1 > MAX_DEPTH:
        errors.append(
            ValidationError(
                prefix, f"Operations are nested deeper than {MAX_DEPTH} levels.", "component"
            )
        )
        return
    local_palettes = raw.get("palettes", {})
    for pname, entries in local_palettes.items():
        for index, entry in enumerate(entries):
            _check_block(
                entry["block"], f"{prefix}.palettes.{pname}[{index}].block", None, blocks, errors
            )
    merged = {**palettes, **local_palettes}
    with components.loading(name):
        _walk_operations(
            raw["operations"], f"{prefix}.operations", depth + 1, merged, blocks, errors
        )


def _check_spec(
    value: Any,
    path: str,
    op_type: str,
    palettes: Mapping[str, Any],
    blocks: blockdata.BlockData,
    errors: list[ValidationError],
) -> None:
    """A block string, or an object with ``block`` / ``palette`` (and ``trim`` for arches)."""
    if isinstance(value, str):
        _check_block(value, path, op_type, blocks, errors)
    elif isinstance(value, Mapping):
        if isinstance(value.get("block"), str):
            _check_block(value["block"], f"{path}.block", op_type, blocks, errors)
        if "palette" in value and value["palette"] not in palettes:
            errors.append(
                ValidationError(
                    f"{path}.palette", "Unknown palette name.", op_type, value["palette"]
                )
            )
        if isinstance(value.get("trim"), str):
            _check_block(value["trim"], f"{path}.trim", op_type, blocks, errors)


def _check_room(
    op: Mapping[str, Any],
    op_path: str,
    palettes: Mapping[str, Any],
    blocks: blockdata.BlockData,
    errors: list[ValidationError],
) -> None:
    for list_key, block_keys in ROOM_OPENING_KEYS.items():
        for index, item in enumerate(op.get(list_key, [])):
            item_path = f"{op_path}.{list_key}[{index}]"
            for key in block_keys:
                if isinstance(item.get(key), str):
                    _check_block(item[key], f"{item_path}.{key}", "room", blocks, errors)
            _check_spec(item.get("arch"), f"{item_path}.arch", "room", palettes, blocks, errors)


def _nested_get(op: Mapping[str, Any], dotted: str) -> Any:
    value: Any = op
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _check_block(
    text: str,
    path: str,
    op_type: str | None,
    blocks: blockdata.BlockData,
    errors: list[ValidationError],
) -> None:
    try:
        state = BlockState.parse(text)
    except BlueprintError as exc:
        errors.append(ValidationError(path, f"{exc}.", op_type, text))
        return
    message = blocks.check_state(state)
    if message is not None:
        errors.append(ValidationError(path, message, op_type, text))


# --- bounds -------------------------------------------------------------------


def _validate_bounds(
    blueprint: Blueprint, max_dimension: int,
) -> list[ValidationError]:
    try:
        # composite operations expand here (and may fail, e.g. a missing component)
        bounds = blueprint.bounds()
    except BlueprintError as exc:
        return [ValidationError("", f"{exc}.")]

    size = bounds.size
    errors: list[ValidationError] = []
    if max(size.x, size.y, size.z) > max_dimension:
        errors.append(
            ValidationError(
                "operations",
                f"Structure exceeds the maximum dimension of {max_dimension} blocks per axis.",
                None,
                f"{size.x} x {size.y} x {size.z} ({bounds})",
            )
        )
    elif bounds.volume > MAX_VOLUME:
        errors.append(
            ValidationError(
                "operations",
                f"Structure bounding box exceeds {MAX_VOLUME:,} cells.",
                None,
                f"{size.x} x {size.y} x {size.z}",
            )
        )
    limit: Vec3 | None = blueprint.size
    if limit is not None and (size.x > limit.x or size.y > limit.y or size.z > limit.z):
        errors.append(
            ValidationError(
                "size",
                "Structure is larger than the declared size.",
                None,
                f"actual {size.x} x {size.y} x {size.z}, size {limit.x} x {limit.y} x {limit.z}",
            )
        )
    return errors
