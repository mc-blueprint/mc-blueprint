"""設定の読込・合成と値の出所追跡。"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from mcblueprint.buildspec import _UniqueLoader
from mcblueprint.errors import BlueprintError

DEFAULTS: dict[str, Any] = {
    "formatVersion": 1,
    "seed": 0,
    "randomness": 0.3,
    "weathering": {
        "age": 0.2,
        "moss": {
            "amount": 0.3,
            "water_proximity": {"weight": 0.6, "radius": 8},
            "ground_proximity": {"weight": 0.3, "height": 4},
            "north_facing": 0.2,
        },
        "cracks": {"amount": 0.2, "exposure": 0.3},
        "missing_blocks": 0.0,
        "vegetation": 0.2,
    },
    "decoration": {"density": 0.6, "depth": 0.5, "symmetry": 0.7},
    "palette": {
        "variation": 0.4,
        "contrast": 0.5,
        "saturation": 0.4,
        "accent": 0.3,
        "mix": 0.3,
        "materials": {},
    },
    "terrain": {
        "roughness": 0.5,
        "amplitude": 0.5,
        "noise": {"scale": 64, "octaves": 5, "persistence": 0.5},
        "erosion": 0.3,
        "caves": 0.0,
        "water_level": None,
        "vegetation": 0.5,
    },
    "preview": {
        "enabled": True,
        "mode": "3d",
        "quality": "auto",
        "shadows": True,
        "brightness": 0.5,
        "time_of_day": 6000,
        "block_light": True,
        "shader": "vanilla",
        "open_browser": True,
        "biome": None,
    },
    "assets": {"minecraft_dir": None, "download": "ask"},
    "render": {"browser": "chromium", "gpu": "auto", "timeout_seconds": 120},
    "output": {"format": "schem", "directory": "output", "emit": []},
    "limits": {"region": [64, None, 64], "halo": 16, "max_memory_mb": 2048},
}


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "schema" / name).read_text(encoding="utf-8"))


def validate_schema(data: Any, schema: dict[str, Any], label: str = "設定") -> None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(data),
        key=lambda e: list(map(str, e.absolute_path)),
    )
    if errors:
        err = errors[0]
        path = ".".join(map(str, err.absolute_path)) or "(root)"
        raise BlueprintError(f"{label}.{path}: {err.message}")


def read_config(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except FileNotFoundError:
        return {}
    except (OSError, yaml.YAMLError) as exc:
        raise BlueprintError(f"設定を読めません: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BlueprintError(f"設定のルートはオブジェクトである必要があります: {path}")
    validate_schema(value, load_schema("config.schema.json"), str(path))
    return value


def _style_defaults(name: str) -> dict[str, Any]:
    path = Path(__file__).parent / "data" / "patterns" / "styles" / f"{name}.yaml"
    if not path.is_file():
        available = ", ".join(p.stem for p in sorted(path.parent.glob("*.yaml")))
        raise BlueprintError(f"未知のstyle {name!r}（候補: {available}）")
    try:
        style = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise BlueprintError(f"styleを読めません: {path}: {exc}") from exc
    roles = style.get("roles", {})
    materials = {
        role: choices if isinstance(choices, list) else [choices] for role, choices in roles.items()
    }
    return {"palette": {"materials": materials}}


def _merge(
    target: dict[str, Any],
    incoming: dict[str, Any],
    source: str,
    origins: dict[str, str],
    prefix: str = "",
) -> None:
    for key, value in incoming.items():
        path = f"{prefix}.{key}" if prefix else key
        if path == "seed" and value is None:
            continue
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value, source, origins, path)
        else:
            target[key] = copy.deepcopy(value)
            if isinstance(value, dict):

                def mark(obj: dict, base: str) -> None:
                    for child, item in obj.items():
                        p = f"{base}.{child}"
                        if isinstance(item, dict):
                            mark(item, p)
                        else:
                            origins[p] = source

                mark(value, path)
            else:
                origins[path] = source


@dataclass(frozen=True)
class ResolvedConfig:
    values: dict[str, Any]
    origins: dict[str, str]


def resolve_config(
    *,
    buildspec: dict[str, Any] | None = None,
    project_dir: Path | None = None,
    user_config: Path | None = None,
    style_defaults: dict[str, Any] | None = None,
    overrides: list[str] | None = None,
) -> ResolvedConfig:
    """6層を優先順に合成し、各葉の出所を返す。"""
    values = copy.deepcopy(DEFAULTS)
    origins: dict[str, str] = {}

    def mark_defaults(obj: dict[str, Any], prefix: str = "") -> None:
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                mark_defaults(v, p)
            else:
                origins[p] = "builtin"

    mark_defaults(values)
    layers: list[tuple[str, dict[str, Any]]] = []
    style_source = "style"
    if buildspec and buildspec.get("style"):
        style_defaults = _style_defaults(buildspec["style"])
        style_source = f"style:{buildspec['style']}"
    if style_defaults:
        layers.append((style_source, style_defaults))
    if user_config is None:
        user_config = (
            Path(os.environ.get("APPDATA", Path.home() / ".config"))
            / "mc-blueprint"
            / "config.yaml"
        )
    layers.append((f"user:{user_config}", read_config(user_config)))
    project_config = (project_dir or Path.cwd()) / "mc-blueprint.yaml"
    layers.append((f"project:{project_config}", read_config(project_config)))
    if buildspec:
        spec_config = copy.deepcopy(buildspec.get("config", {}))
        if "materials" in buildspec:
            materials = spec_config.setdefault("palette", {}).setdefault("materials", {})
            for role, material in buildspec["materials"].items():
                if role in materials and materials[role] != material:
                    raise BlueprintError(
                        f"BuildSpec materials.{role} と "
                        f"config.palette.materials.{role} が競合します"
                    )
                materials[role] = material
        if "seed" in buildspec and buildspec["seed"] is not None:
            spec_config["seed"] = buildspec["seed"]
        layers.append(("buildspec", spec_config))
    for source, layer in layers:
        validate_schema(layer, load_schema("config.schema.json"), source)
        _merge(values, layer, source, origins)
    for assignment in overrides or []:
        if "=" not in assignment:
            raise BlueprintError(f"--set は key=value を指定してください: {assignment}")
        path, raw = assignment.split("=", 1)
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise BlueprintError(f"--set の値が不正です: {assignment}: {exc}") from exc
        parts = path.split(".")
        node = values
        prefix = []
        for key in parts[:-1]:
            prefix.append(key)
            if key not in node and prefix == ["palette", "materials"]:
                node[key] = {}
            if key not in node or not isinstance(node[key], dict):
                raise BlueprintError(f"未知の設定項目: {path}")
            node = node[key]
        if not parts[-1] or (parts[-1] not in node and parts[:-1] != ["palette", "materials"]):
            raise BlueprintError(f"未知の設定項目: {path}")
        node[parts[-1]] = value
        origins[path] = "cli"
    validate_schema(values, load_schema("config.schema.json"))
    return ResolvedConfig(values, origins)
