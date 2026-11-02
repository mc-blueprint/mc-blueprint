"""``mcblueprint check``: validate design presets (``designs/*.md``) and component
files (``components/*.json``) that are not Blueprints themselves.

A preset is Markdown; every fenced ```json block that carries ``palettes`` (or a
``type`` / ``operations`` snippet) is validated like the matching part of a
Blueprint. A component is validated by placing it at ``[0, 0, 0]`` in a throwaway
Blueprint, which also runs the support check on the generated blocks.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcblueprint import blockdata, components
from mcblueprint.errors import BlueprintError, ValidationError
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model.vec import Vec3
from mcblueprint.support import SupportWarning, check_support
from mcblueprint.validator import validate

FENCED_JSON = re.compile(r"```json[^\n]*\n(.*?)```", re.S)
DEFAULT_TARGETS = ("designs", "components")


@dataclass
class CheckResult:
    path: Path
    kind: str  # "preset" | "component"
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[SupportWarning] = field(default_factory=list)
    size: Vec3 | None = None  # generated extent of a component
    description: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def _blueprint(version: str, name: str, **extra: Any) -> dict[str, Any]:
    return {"formatVersion": 1, "minecraftVersion": version, "name": name, **extra}


# --- presets ------------------------------------------------------------------


def preset_snippets(text: str) -> list[tuple[int, Any | None, str | None]]:
    """``(block index, parsed JSON, parse error)`` of every fenced json block."""
    snippets: list[tuple[int, Any | None, str | None]] = []
    for index, match in enumerate(FENCED_JSON.finditer(text)):
        try:
            snippets.append((index, json.loads(match.group(1)), None))
        except json.JSONDecodeError as exc:
            snippets.append((index, None, str(exc)))
    return snippets


def check_preset(path: Path, version: str) -> CheckResult:
    result = CheckResult(path, "preset")
    for index, data, parse_error in preset_snippets(path.read_text(encoding="utf-8")):
        prefix = f"json[{index}]"
        if parse_error is not None:
            result.errors.append(ValidationError(prefix, f"Invalid JSON: {parse_error}."))
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("palettes"), dict) and data["palettes"]:
            palettes = data["palettes"]
            operations = [
                {"type": "set", "position": [i, 0, 0], "palette": name}
                for i, name in enumerate(palettes)
            ]
            probe = _blueprint(version, path.stem, palettes=palettes, operations=operations)
        elif isinstance(data.get("operations"), list) and data["operations"]:
            probe = _blueprint(version, path.stem, **{k: v for k, v in data.items() if k != "name"})
            probe.setdefault("palettes", {})
        elif "type" in data:
            probe = _blueprint(version, path.stem, operations=[data])
        else:
            continue
        for err in validate(probe):
            joined = f"{prefix}.{err.path}" if err.path else prefix
            result.errors.append(ValidationError(joined, err.message, err.op_type, err.value))
    return result


# --- components ---------------------------------------------------------------


def check_component(path: Path, version: str) -> CheckResult:
    name = path.stem
    probe = _blueprint(
        version,
        name,
        operations=[{"type": "component", "name": name, "position": [0, 0, 0]}],
    )
    search = (path.resolve().parent, *components.default_search_paths())
    return _check_component_probe(
        path,
        probe,
        lambda: components.load_component(name),
        search,
        f"operations[0]<{name}>",
    )


def check_component_data(path: Path, version: str, raw: dict[str, Any]) -> CheckResult:
    """Inspect component-shaped data directly without resolving it by filename."""
    name = path.stem
    probe = _blueprint(
        version,
        name,
        **{key: raw[key] for key in ("palettes", "operations") if key in raw},
    )
    return _check_component_probe(
        path,
        probe,
        lambda: raw,
        components.default_search_paths(),
        None,
    )


def _check_component_probe(
    path: Path,
    probe: dict[str, Any],
    load_component: Callable[[], dict[str, Any]],
    search_paths: tuple[Path, ...],
    error_prefix: str | None,
) -> CheckResult:
    result = CheckResult(path, "component")
    with components.component_search_paths(search_paths):
        for error in validate(probe):
            error_path = error.path
            if error_prefix is not None:
                if error_path.startswith(error_prefix):
                    error_path = error_path[len(error_prefix) :].lstrip(".")
                elif error_path.startswith("operations[0]"):
                    error_path = error_path[len("operations[0]") :].lstrip(".")
            result.errors.append(
                ValidationError(error_path, error.message, error.op_type, error.value)
            )
        if result.errors:
            return result
        try:
            raw = load_component()
            volume = generate(load_blueprint_dict(probe))
        except BlueprintError as exc:
            result.errors.append(ValidationError("", f"{exc}."))
            return result
    result.description = raw.get("description")
    bounds = volume.bounds()
    result.size = bounds.size if bounds is not None else None
    result.warnings = check_support(volume)
    return result


# --- driver -------------------------------------------------------------------


def iter_targets(paths: Iterable[Path]) -> Iterator[tuple[Path, str]]:
    """``(file, kind)`` for every preset / component under ``paths`` (directories are
    searched recursively; README files are skipped)."""
    for path in paths:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.name.lower() != "readme.md":
                    kind = _kind_of(child)
                    if kind is not None:
                        yield child, kind
        elif path.is_file():
            kind = _kind_of(path)
            if kind is None:
                raise BlueprintError(f"{path}: not a preset (.md) or component (.json)")
            yield path, kind
        else:
            raise BlueprintError(f"{path}: no such file or directory")


def _kind_of(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "preset"
    if suffix == ".json":
        return "component"
    return None


def check_paths(paths: Iterable[Path], version: str | None = None) -> list[CheckResult]:
    version = version or blockdata.supported_versions()[0]
    if not blockdata.is_supported(version):
        raise BlueprintError(
            f"Unsupported Minecraft version {version!r}. "
            f"Supported versions: {', '.join(blockdata.supported_versions())}"
        )
    results = []
    for path, kind in iter_targets(paths):
        if kind == "preset":
            results.append(check_preset(path, version))
        else:
            results.append(check_component(path, version))
    return results


def list_components(paths: Iterable[Path], version: str | None = None) -> list[CheckResult]:
    """Every ``*.json`` directly inside ``paths`` (first hit per name wins), checked."""
    version = version or blockdata.supported_versions()[0]
    seen: set[str] = set()
    results = []
    for directory in paths:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.stem in seen:
                continue
            seen.add(path.stem)
            results.append(check_component(path, version))
    return results
