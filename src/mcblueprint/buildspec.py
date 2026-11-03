"""BuildSpec の読み込み、既定値適用、Resolve API。"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from mcblueprint.errors import BlueprintError

SCALE_LIMITS = {
    "tiny": [16, 16, 16],
    "small": [32, 32, 32],
    "medium": [64, 48, 64],
    "large": [128, 96, 128],
    "huge": [256, 128, 256],
}
SUPPORTED_ARCHETYPES = ("house", "tower")
SUPPORTED_TERRAINS = ("none", "flat")


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise BlueprintError(f"重複したYAMLキー: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class ResolveResult:
    """#151 以降の処理と config show が共有する正規化済みResolve結果。"""

    buildspec: dict[str, Any] | None
    config: dict[str, Any]
    origins: dict[str, str]


def _read_buildspec(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except OSError as exc:
        raise BlueprintError(f"BuildSpecを読めません: {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BlueprintError(f"BuildSpec YAMLが不正です: {exc}") from exc
    if not isinstance(data, dict):
        raise BlueprintError("BuildSpecのルートはオブジェクトである必要があります")
    return data


def _near(value: str, choices: tuple[str, ...]) -> str:
    matches = difflib.get_close_matches(value, choices, n=3, cutoff=0.45)
    return f"（候補: {', '.join(matches)}）" if matches else f"（有効値: {', '.join(choices)}）"


def _validate_features(data: dict[str, Any]) -> None:
    archetype = data.get("archetype")
    if archetype not in SUPPORTED_ARCHETYPES:
        if archetype == "castle":
            raise BlueprintError(
                "archetype 'castle' はCのMassing段で未対応です（城郭計画はHマイルストーン）"
            )
        raise BlueprintError(
            f"未知または未対応のarchetype {archetype!r} "
            f"{_near(str(archetype), SUPPORTED_ARCHETYPES)}（CのMassing段ではhouse/towerのみ対応。"
            "その他のarchetypeはHマイルストーン）"
        )
    features = data.get("features", {})
    if not isinstance(features, dict):
        return  # JSON Schema がパス付きで型を報告する
    allowed = {
        "house": {"floors", "floor_height", "roof"},
        "tower": {"shape", "floors", "floor_height", "battlement"},
    }[archetype]
    for key in features:
        if key not in allowed:
            raise BlueprintError(
                f"features.{key}: {archetype} では未知のfeature "
                f"{_near(key, tuple(sorted(allowed)))}"
            )


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    from mcblueprint.config import load_schema, validate_schema

    result = dict(data)
    result.setdefault("formatVersion", 1)
    result.setdefault("archetype", "house")
    result.setdefault("style", "medieval")
    if "scale" not in result and "size" not in result:
        result["scale"] = "small"
    result.setdefault("seed", 0)
    result.setdefault("site", {})
    if isinstance(result["site"], dict):
        result["site"].setdefault("terrain", "none")
        result["site"].setdefault("biome", "plains")
    result.setdefault("features", {})
    result.setdefault("interior", "minimal")
    scale = result.get("scale")
    if isinstance(scale, str) and scale not in SCALE_LIMITS:
        raise BlueprintError(f"BuildSpec.scale {scale!r} {_near(scale, tuple(SCALE_LIMITS))}")
    _validate_features(result)
    site = result.get("site")
    if isinstance(site, dict):
        terrain = site.get("terrain", "none")
        unsupported_terrain = ("hill", "mountain", "island", "cave")
        if terrain in unsupported_terrain:
            raise BlueprintError(
                f"site.terrain {terrain!r} はSite段で未対応です "
                "（Cはnone/flatと既存terrain snapshotに対応。"
                "hill/mountain/island/cave生成はHマイルストーン）"
            )
        if isinstance(terrain, str) and terrain not in (*SUPPORTED_TERRAINS, *unsupported_terrain):
            choices = (*SUPPORTED_TERRAINS, *unsupported_terrain)
            if difflib.get_close_matches(terrain, choices, n=1, cutoff=0.7):
                raise BlueprintError(f"site.terrain {terrain!r} {_near(terrain, choices)}")
    if result.get("interior") == "furnished":
        raise BlueprintError("interior 'furnished' はDetail段で未対応です（Dマイルストーン）")
    schema = load_schema("buildspec.schema.json")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(result),
        key=lambda err: list(map(str, err.absolute_path)),
    )
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.absolute_path)) or "(root)"
        message = err.message
        if path == "minecraftVersion" and isinstance(result.get(path), str):
            versions = ("1.21.11", "26.1.2", "26.2", "26.3")
            message += f" {_near(result[path], versions)}"
        raise BlueprintError(f"BuildSpec.{path}: {message}")
    validate_schema(result.get("config", {}), load_schema("config.schema.json"), "BuildSpec.config")
    scale = result.get("scale")
    if "size" not in result and scale in SCALE_LIMITS:
        result["size"] = SCALE_LIMITS[scale].copy()
    if "size" in result and any(value > 256 for value in result["size"]):
        raise BlueprintError("BuildSpec.size: 各軸の上限は256です")
    features = result.get("features", {})
    floors = features.get("floors")
    floor_height = features.get("floor_height")
    if floors is not None or floor_height is not None:
        floors = floors if floors is not None else 1
        floor_height = floor_height if floor_height is not None else 4
        roof_height = 1 if result["archetype"] == "house" else 2
        if floors * floor_height + roof_height > result["size"][1]:
            raise BlueprintError("BuildSpec.features: 階数と階高がBuildSpec.sizeのy上限を超えます")
    return result


def load_buildspec(path: str | Path) -> dict[str, Any]:
    """BuildSpec YAML を読み、既定値を補ってスキーマ検証する。"""
    return _normalize(_read_buildspec(path))


def resolve_buildspec(
    source: str | Path | dict[str, Any] | None = None,
    *,
    project_dir: Path | None = None,
    user_config: Path | None = None,
    overrides: list[str] | None = None,
) -> ResolveResult:
    """BuildSpecと6層の設定を検証・合成する、生成処理とCLI共通の公開API。"""
    from mcblueprint.config import resolve_config

    spec = None
    if source is not None:
        raw = _read_buildspec(source) if isinstance(source, str | Path) else source
        spec = _normalize(raw)
        if isinstance(source, str | Path):
            base_dir = Path(source).resolve().parent
        else:
            base_dir = project_dir or Path.cwd()
        terrain = spec["site"]["terrain"]
        if terrain not in SUPPORTED_TERRAINS:
            spec["site"]["terrain"] = str((base_dir / terrain).resolve())
        spec_config = spec.get("config", {})
        config_seed = spec_config.get("seed")
        if config_seed is not None and config_seed != spec["seed"]:
            raise BlueprintError("BuildSpec.seed と BuildSpec.config.seed が異なります")
        configured = spec_config.get("palette", {}).get("materials", {})
        for role, material in spec.get("materials", {}).items():
            if role in configured and configured[role] != material:
                raise BlueprintError(
                    f"BuildSpec materials.{role} と config.palette.materials.{role} が競合します"
                )
    resolved = resolve_config(
        buildspec=spec,
        project_dir=project_dir,
        user_config=user_config,
        overrides=overrides,
    )
    origins = resolved.origins.copy()
    if spec is not None:
        spec["config"] = resolved.values
        location = str(Path(source).resolve()) if isinstance(source, str | Path) else "<memory>"
        origins = {
            path: f"buildspec:{location}" if origin == "buildspec" else origin
            for path, origin in origins.items()
        }
    return ResolveResult(spec, resolved.values, origins)
