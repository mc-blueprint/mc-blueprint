"""パターンライブラリ v1 の構文・参照検証。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from yaml import YAMLError

from mcblueprint import blockdata, components
from mcblueprint.checking import CheckResult, check_component_data
from mcblueprint.errors import BlueprintError, ValidationError
from mcblueprint.materialize import materialize

SCHEMA_DIR = Path(__file__).parent / "schema"


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _validate(
    data: Any, schema: dict[str, Any], registry: Registry[Any] | None = None
) -> list[ValidationError]:
    validator = (
        Draft202012Validator(schema)
        if registry is None
        else Draft202012Validator(schema, registry=registry)
    )
    return [
        ValidationError(".".join(map(str, error.absolute_path)), error.message)
        for error in validator.iter_errors(data)
    ]


def _module_registry(schema: dict[str, Any]) -> Registry[Any]:
    blueprint = _schema("blueprint.schema.json")
    return (
        Registry()
        .with_resource(blueprint["$id"], Resource.from_contents(blueprint))
        .with_resource(schema["$id"], Resource.from_contents(schema))
    )


def check_patterns(root: Path, version: str | None = None) -> list[CheckResult]:
    """patterns ディレクトリ内の様式・モジュール・出典と相互参照を検証する。"""
    results: list[CheckResult] = []
    styles: dict[str, dict[str, Any]] = {}
    material_styles: dict[str, dict[str, Any]] = {}
    modules: set[str] = set()
    style_schema = _schema("pattern-style.schema.json")
    module_schema = _schema("pattern-module.schema.json")
    module_registry = _module_registry(module_schema)
    sources_schema = _schema("pattern-sources.schema.json")

    for directory in ("styles", "modules"):
        path = root / directory
        if not path.is_dir():
            results.append(
                CheckResult(path, directory, [ValidationError("", "directory is required")])
            )

    for path in sorted((root / "styles").glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            errors = _validate(data, style_schema)
            if isinstance(data, dict):
                if not errors:
                    material_styles[path.stem] = data
                if not _validate(data.get("modules", {}), style_schema["properties"]["modules"]):
                    styles[path.stem] = data
                if data.get("name") != path.stem:
                    errors.append(ValidationError("name", f"must match filename {path.stem!r}"))
        except (OSError, YAMLError) as exc:
            errors = [ValidationError("", f"Invalid YAML: {exc}")]
        results.append(CheckResult(path, "style", errors))

    for path in sorted((root / "modules").glob("*/*.json")):
        name = f"{path.parent.name}.{path.stem}"
        warnings = []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            errors = _validate(data, module_schema, module_registry)
            if isinstance(data, dict):
                modules.add(name)
                if data.get("name") != name:
                    errors.append(ValidationError("name", f"must match module path {name!r}"))
                warnings = []
                if not errors:
                    variants = [material_styles[path.parent.name]] if (
                        path.parent.name in material_styles
                    ) else (
                        list(material_styles.values()) or [{"roles": {}}]
                    )
                    for target_version in [version] if version else blockdata.supported_versions():
                        for style in variants:
                            try:
                                with components.component_search_paths(
                                    components.default_search_paths(),
                                    module_paths=(root / "modules",),
                                ):
                                    concrete = materialize(
                                        {**data, "minecraftVersion": target_version}, style=style,
                                    ).blueprint
                                concrete.pop("minecraftVersion")
                                concrete.pop("seed", None)
                                component = check_component_data(
                                    path, target_version, concrete,
                                    components.default_search_paths(),
                                    module_paths=(root / "modules",),
                                )
                                errors.extend(component.errors)
                                warnings.extend(component.warnings)
                            except BlueprintError as exc:
                                errors.append(ValidationError("operations", str(exc)))
                            if errors:
                                break
                        if errors:
                            break
        except (OSError, json.JSONDecodeError) as exc:
            errors = [ValidationError("", f"Invalid JSON: {exc}")]
        results.append(CheckResult(path, "module", errors, warnings))

    for path, style in styles.items():
        for group, names in (style.get("modules") or {}).items():
            for i, name in enumerate(names):
                if name not in modules:
                    file = root / "styles" / f"{path}.yaml"
                    row = next(r for r in results if r.path == file)
                    row.errors.append(
                        ValidationError(f"modules.{group}[{i}]", f"unknown module {name!r}")
                    )

    path = root / "sources.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            errors = _validate(data, sources_schema)
        except (OSError, YAMLError) as exc:
            errors = [ValidationError("", f"Invalid YAML: {exc}")]
        results.append(CheckResult(path, "sources", errors))
    else:
        results.append(CheckResult(path, "sources", [ValidationError("", "file is required")]))
    return results
