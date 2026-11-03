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
_module_paths: ContextVar[tuple[Path, ...]] = ContextVar("component_module_paths")


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
def component_search_paths(
    paths: Sequence[Path], *, module_paths: Sequence[Path] | None = None
) -> Iterator[None]:
    """単純名を paths の順で探索する。ドット名は同梱モジュールから解決する。

    module_paths を指定すると、ドット名の探索先だけを置き換える。
    外部 patterns の検査では、その modules/ だけを指定する。
    """
    token = _search_paths.set(tuple(Path(p) for p in paths))
    module_token = _module_paths.set(
        (PATTERNS_DIR,) if module_paths is None else tuple(Path(p) for p in module_paths)
    )
    try:
        yield
    finally:
        _module_paths.reset(module_token)
        _search_paths.reset(token)


def resolve(name: str) -> Path:
    """参照名のファイルを返す。不正な名前・未発見は BlueprintError。"""
    if MODULE_NAME_PATTERN.fullmatch(name):
        style, module = name.split(".")
        directories = _module_paths.get((PATTERNS_DIR,))
        relative = Path(style) / f"{module}.json"
    elif NAME_PATTERN.fullmatch(name):
        directories = search_paths()
        relative = Path(f"{name}.json")
    else:
        raise BlueprintError(f"Invalid component name {name!r} (use a-z, 0-9 and _)")
    for directory in directories:
        candidate = directory / relative
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in directories)
    raise BlueprintError(f"Component {name!r} not found (searched: {searched})")


def iter_components() -> Iterator[tuple[str, Path]]:
    """現在の探索先で配置可能な参照名と、その解決先を重複なく列挙する。"""
    names = dict.fromkeys(
        path.stem
        for directory in search_paths()
        for path in sorted(directory.glob("*.json"))
        if NAME_PATTERN.fullmatch(path.stem) and path.is_file()
    )
    for directory in _module_paths.get((PATTERNS_DIR,)):
        for path in sorted(directory.glob("*/*.json")):
            name = f"{path.parent.name}.{path.stem}"
            if MODULE_NAME_PATTERN.fullmatch(name) and path.is_file():
                names.setdefault(name, None)
    for name in names:
        yield name, resolve(name)


def load_component(name: str, *, raw: bool = False) -> dict[str, Any]:
    """部品を読み、循環参照を検査する。rawなら材質解決前の役割参照を保つ。

    版を持たない従来の部品経路は、同梱最古版と所属様式の既定材で解決する。
    BuildSpecのMaterializeはrawを使い、入力の版・材質指定で解決する。
    """
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
    if not raw and MODULE_NAME_PATTERN.fullmatch(name):
        from mcblueprint.blockdata import supported_versions
        from mcblueprint.materialize import materialize

        concrete = materialize(
            {**data, "minecraftVersion": supported_versions()[0]},
            style=name.split(".")[0],
        ).blueprint
        data = {key: value for key, value in concrete.items()
                if key not in ("minecraftVersion", "seed")}
    return data


@contextmanager
def loading(name: str) -> Iterator[None]:
    """Mark ``name`` as being expanded so nested includes can detect cycles."""
    token = _loading.set((*_loading.get(), name))
    try:
        yield
    finally:
        _loading.reset(token)
