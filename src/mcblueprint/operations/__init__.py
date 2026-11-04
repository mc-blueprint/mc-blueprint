"""Operation implementations. Importing this package registers every built-in type."""

import importlib

from mcblueprint.operations.base import (
    BlockSpec,
    ExecutionContext,
    Operation,
    PlacementOperation,
    Transform,
)
from mcblueprint.operations.registry import OPERATIONS, build_operation, build_operations, register

# Importing the modules registers the built-in operation types.
_BUILTIN_MODULES = (
    "set",
    "fill",
    "box",
    "wall",
    "floor",
    "line",
    "circle",
    "cylinder",
    "sphere",
    "mirror",
    "repeat",
    "replace",
    "translate",
    "copy",
    "rotate",
    "stairs",
    "spiral_stairs",
    "roof",
    "pillar",
    "doorway",
    "window",
    "arch",
    "room",
    "building",
    "join_buildings",
    "tower",
    "building_junction",
    "bridge",
    "gate",
    "garden",
    "path",
    "landform",
    "terrain_density",
    "component",
)
for _name in _BUILTIN_MODULES:
    importlib.import_module(f"{__name__}.{_name}")

__all__ = [
    "OPERATIONS",
    "BlockSpec",
    "ExecutionContext",
    "Operation",
    "PlacementOperation",
    "Transform",
    "build_operation",
    "build_operations",
    "register",
]
