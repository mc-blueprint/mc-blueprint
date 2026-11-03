"""Maps the ``type`` field of an operation to its implementation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from mcblueprint.errors import BlueprintError
from mcblueprint.operations.base import Operation

OPERATIONS: dict[str, type[Operation]] = {}

T = TypeVar("T", bound=type[Operation])


def register(cls: T) -> T:
    """Class decorator that registers an :class:`Operation` subclass by its ``type``."""
    OPERATIONS[cls.type] = cls
    return cls


def build_operation(data: Mapping[str, Any], path: str) -> Operation:
    if not isinstance(data, Mapping):
        raise BlueprintError(f"{path}: operation must be an object")
    op_type = data.get("type")
    cls = OPERATIONS.get(op_type)
    if cls is None:
        known = ", ".join(sorted(OPERATIONS)) or "none"
        raise BlueprintError(f"{path}: unknown operation type {op_type!r} (known: {known})")
    operation = cls.from_dict(data, path)
    operation.id = data.get("id")
    operation.attributes = {
        key: data[key] for key in ("role", "element", "face", "reserved", "owner", "source")
        if key in data
    }
    operation.on_conflict = data.get("onConflict")
    return operation


def build_operations(items: Sequence[Mapping[str, Any]], path: str) -> list[Operation]:
    return [build_operation(item, f"{path}[{index}]") for index, item in enumerate(items)]
