"""Load Blueprint JSON into the :class:`~mcblueprint.model.Blueprint` model.

The loader expects data that already passed the JSON Schema (validator.py wires
that in); shape problems it still encounters are reported as
:class:`~mcblueprint.errors.BlueprintError` with a JSON path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcblueprint import blockdata, components
from mcblueprint.errors import BlueprintError
from mcblueprint.foundation import FoundationSpec, plan_foundation
from mcblueprint.importers import read_terrain, read_terrain_regions
from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import FORMAT_VERSION, Blueprint
from mcblueprint.model.palette import Palette, PaletteEntry
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.registry import build_operations
from mcblueprint.surface_path import SurfacePathSpec, plan_surface_path
from mcblueprint.terrain import RegionTerrainSnapshot, TerrainPlacement


def read_blueprint_json(path: str | Path) -> dict[str, Any]:
    """Read and parse a Blueprint JSON file without converting it."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BlueprintError(f"Blueprint file not found: {path}") from None
    except OSError as exc:
        raise BlueprintError(f"Cannot read blueprint file {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlueprintError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BlueprintError(f"Blueprint root must be a JSON object: {path}")
    return data


def load_blueprint(path: str | Path) -> Blueprint:
    """Read a Blueprint JSON file and convert it to the model.

    Components are resolved relative to the file's directory (then the cwd).
    """
    with components.component_search_paths(components.default_search_paths(Path(path))):
        return load_blueprint_dict(read_blueprint_json(path), base_dir=Path(path).parent)


def load_blueprint_dict(
    data: dict[str, Any], *, base_dir: Path | None = None, terrain_snapshot=None,
) -> Blueprint:
    """検証済みJSONをモデルへ変換する。地形sourceの相対パスはbase_dir（既定cwd）基準。

    地形ファイルはこの呼出し時に読み取り、不変のsnapshotとして保持する。
    terrain_snapshot指定時はSiteで読取済みの同じ地形を再利用し、版・保存hashを照合する。
    読取不能・保持不能・施工条件違反はBlueprintErrorになる。
    """
    format_version = data.get("formatVersion")
    if format_version != FORMAT_VERSION:
        raise BlueprintError(
            f"Unsupported formatVersion {format_version!r} (expected {FORMAT_VERSION})"
        )
    operations = data.get("operations")
    if not isinstance(operations, list) or not operations:
        raise BlueprintError("operations must be a non-empty array")

    blueprint = Blueprint(
        format_version=format_version,
        minecraft_version=_require_str(data, "minecraftVersion"),
        name=_require_str(data, "name"),
        operations=build_operations(operations, "operations"),
        description=_optional_str(data, "description"),
        author=_optional_str(data, "author"),
        seed=int(data.get("seed", 0)),
        origin=_vec(data.get("origin", [0, 0, 0]), "origin"),
        size=_vec(data["size"], "size") if "size" in data else None,
        palettes=build_palettes(data.get("palettes", {})),
        metadata=data.get("metadata"),
        terrain=_load_terrain(data, base_dir, terrain_snapshot) if "terrain" in data else None,
    )
    if blueprint.terrain is not None:
        region = blueprint.terrain.region
        for op in blueprint.operations:
            bounds = op.bounds()
            if not region.contains(bounds.min) or not region.contains(bounds.max):
                raise BlueprintError(f"{op.path}: Operation の範囲が terrain.region 外です")
    if "foundation" in data:
        if blueprint.terrain is None:
            raise BlueprintError("foundation には terrain が必要です")
        blueprint.foundation = plan_foundation(
            FoundationSpec.from_dict(data["foundation"]), blueprint.terrain,
        )
    if "surfacePath" in data:
        if blueprint.terrain is None:
            raise BlueprintError("surfacePath には terrain が必要です")
        blueprint.surface_path = plan_surface_path(
            SurfacePathSpec.from_dict(data["surfacePath"]), blueprint.terrain, blueprint.foundation,
        )
    return blueprint


def _load_terrain(data: dict[str, Any], base_dir: Path | None, snapshot=None) -> TerrainPlacement:
    raw = data["terrain"]
    generation = (data.get("metadata") or {}).get("generation", {})
    if not isinstance(generation, dict):
        generation = {}
    if snapshot is None:
        source = (base_dir or Path.cwd()) / raw["source"]
        if generation.get("mode") == "buildspec-v1":
            try:
                budget = generation["config"]["limits"]["max_memory_mb"]
            except (KeyError, TypeError) as exc:
                raise BlueprintError("生成metadataに合成済みのメモリ上限が必要です") from exc
            snapshot = read_terrain_regions(source, max_memory_mb=budget)
        else:
            snapshot = read_terrain(source)
    expected = generation.get("terrainHash")
    if expected is not None and snapshot.input_sha256 != expected:
        raise BlueprintError("terrain.source の入力hashが保存された地形と一致しません")
    if snapshot.minecraft_version != data["minecraftVersion"]:
        raise BlueprintError("terrain.source のDataVersionがBlueprintのMinecraft版と一致しません")

    def box(value: dict[str, Any]) -> AABB:
        return AABB.of(_vec(value["from"], "terrain.from"), _vec(value["to"], "terrain.to"))

    try:
        terrain = TerrainPlacement(
            snapshot, box(raw["region"]), tuple(box(value) for value in raw.get("protected", [])),
        )
    except ValueError as exc:
        raise BlueprintError(str(exc)) from exc
    blocks = blockdata.load_block_data(data["minecraftVersion"])
    if isinstance(snapshot, RegionTerrainSnapshot):
        for position, state in snapshot.states_in(terrain.region):
            problem = blocks.check_state(state)
            if problem is not None:
                raise BlueprintError(f"terrain.source {position}: {problem}")
        return terrain
    for local, state in snapshot.blocks.items():
        if terrain.region.contains(snapshot.to_source(local)):
            problem = blocks.check_state(state)
            if problem is not None:
                raise BlueprintError(f"terrain.source {snapshot.to_source(local)}: {problem}")
    return terrain


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise BlueprintError(f"{key} must be a non-empty string")
    return value


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise BlueprintError(f"{key} must be a string")
    return value


def _vec(value: Any, path: str) -> Vec3:
    if not isinstance(value, list):
        raise BlueprintError(f"{path} must be an array of 3 integers")
    try:
        return Vec3.from_seq(value)
    except (ValueError, TypeError):
        raise BlueprintError(f"{path} must be an array of 3 integers") from None


def build_palettes(value: Any) -> dict[str, Palette]:
    """Convert the ``palettes`` object of a blueprint or component."""
    if not isinstance(value, dict):
        raise BlueprintError("palettes must be an object")
    palettes: dict[str, Palette] = {}
    for name, raw_entries in value.items():
        if not isinstance(raw_entries, list) or not raw_entries:
            raise BlueprintError(f"palettes.{name} must be a non-empty array")
        entries = []
        for index, raw in enumerate(raw_entries):
            path = f"palettes.{name}[{index}]"
            if not isinstance(raw, dict) or "block" not in raw:
                raise BlueprintError(f"{path} must be an object with a block")
            try:
                block = BlockState.parse(raw["block"])
            except BlueprintError as exc:
                raise BlueprintError(f"{path}.block: {exc}") from None
            weight = raw.get("weight", 1)
            if not isinstance(weight, int | float) or weight <= 0:
                raise BlueprintError(f"{path}.weight must be a positive number")
            entries.append(PaletteEntry(block, float(weight)))
        palettes[name] = Palette(name, tuple(entries))
    return palettes
