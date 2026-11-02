"""Reusable building parts stored as JSON files (docs/FORMAT.md section 12).

A component file has the same shape as a Blueprint minus ``minecraftVersion`` /
``origin`` / ``size`` / ``seed``; its coordinates are relative to the part's own
origin. Files are looked up by name in the search paths set for the current
load (the blueprint's directory and the working directory, each ``/components``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mcblueprint.errors import BlueprintError

COMPONENTS_DIR = "components"
PATTERNS_DIR = Path(__file__).parent / "data" / "patterns" / "modules"
NAME_PATTERN = re.compile(r"^[a-z0-9_]+$")
MODULE_NAME_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
COMPONENT_NAME_PATTERN = re.compile(r"^(?:[a-z0-9_]+|[a-z0-9_]+\.[a-z0-9_]+)$")

_search_paths: ContextVar[tuple[Path, ...]] = ContextVar("component_search_paths")
_loading: ContextVar[tuple[str, ...]] = ContextVar("component_loading", default=())


def default_search_paths(blueprint_path: Path | None = None) -> tuple[Path, ...]:
    paths: list[Path] = []
    if blueprint_path is not None:
        paths.append(Path(blueprint_path).resolve().parent / COMPONENTS_DIR)
    cwd = Path.cwd() / COMPONENTS_DIR
    if cwd not in paths:
        paths.append(cwd)
    return tuple(paths)


def search_paths() -> tuple[Path, ...]:
    try:
        return _search_paths.get()
    except LookupError:
        return default_search_paths()


@contextmanager
def component_search_paths(paths: Sequence[Path]) -> Iterator[None]:
    """Use ``paths`` (in order) to resolve component names inside the block."""
    token = _search_paths.set(tuple(Path(p) for p in paths))
    try:
        yield
    finally:
        _search_paths.reset(token)


def resolve(name: str) -> Path:
    if MODULE_NAME_PATTERN.fullmatch(name):
        style, module = name.split(".")
        path = PATTERNS_DIR / style / f"{module}.json"
        if path.is_file():
            return path
        raise BlueprintError(f"Component {name!r} not found (bundled pattern module)")
    if not NAME_PATTERN.match(name):
        raise BlueprintError(f"Invalid component name {name!r} (use a-z, 0-9 and _)")
    for directory in search_paths():
        candidate = directory / f"{name}.json"
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in search_paths())
    raise BlueprintError(f"Component {name!r} not found (searched: {searched})")


def load_component(name: str) -> dict[str, Any]:
    """Read and minimally check a component file; cycle detection included."""
    if name in _loading.get():
        chain = " -> ".join((*_loading.get(), name))
        raise BlueprintError(f"Component {name!r} includes itself: {chain}")
    path = resolve(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BlueprintError(f"Invalid JSON in component {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BlueprintError(f"Component root must be a JSON object: {path}")
    if data.get("formatVersion") != 1:
        raise BlueprintError(f"Component {name!r}: unsupported formatVersion")
    if not isinstance(data.get("operations"), list) or not data["operations"]:
        raise BlueprintError(f"Component {name!r}: operations must be a non-empty array")
    return data


@contextmanager
def loading(name: str) -> Iterator[None]:
    """Mark ``name`` as being expanded so nested includes can detect cycles."""
    token = _loading.set((*_loading.get(), name))
    try:
        yield
    finally:
        _loading.reset(token)
