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
from mcblueprint.errors import ValidationError

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


def _role_references(value: Any, path: str = "") -> list[str]:
    references = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key == "block" and isinstance(child, dict) and "role" in child:
                references.append(child_path)
            else:
                references.extend(_role_references(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            references.extend(_role_references(child, f"{path}[{index}]"))
    return references


def check_patterns(root: Path, version: str | None = None) -> list[CheckResult]:
    """patterns ディレクトリ内の様式・モジュール・出典と相互参照を検証する。"""
    results: list[CheckResult] = []
    styles: dict[str, dict[str, Any]] = {}
    modules: set[str] = set()
    style_schema = _schema("pattern-style.schema.json")
    module_schema = _schema("pattern-module.schema.json")
    module_registry = _module_registry(module_schema)
    sources_schema = _schema("pattern-sources.schema.json")

    for path in sorted((root / "styles").glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            errors = _validate(data, style_schema)
            if isinstance(data, dict) and isinstance(data.get("modules", {}), dict):
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
                for role_path in _role_references(data.get("operations", []), "operations"):
                    errors.append(
                        ValidationError(
                            role_path, "role references must be materialized before validation"
                        )
                    )
                warnings = []
                if not errors:
                    search = (root / "modules", *components.default_search_paths())
                    with components.component_search_paths(search):
                        component = check_component_data(
                            path,
                            version or blockdata.supported_versions()[0],
                            data,
                            search,
                            bundled_patterns=False,
                        )
                    errors.extend(component.errors)
                    warnings = component.warnings
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
