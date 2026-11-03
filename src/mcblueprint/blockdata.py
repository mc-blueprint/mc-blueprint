"""版固定した blockdata v1 のブロック情報を読む（docs/ARCHITECTURE.md §10）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable
from typing import Any

from mcblueprint.errors import BlueprintError
from mcblueprint.model.block import BlockState


@dataclass(frozen=True)
class BlockInfo:
    id: str
    properties: dict[str, tuple[str, ...]]
    default: dict[str, str]
    material: str = "other"
    tint: str = "none"
    faces: dict[str, dict[str, Any]] = field(default_factory=dict)
    family: str | None = None
    shape: str = "full"
    weathering: dict[str, str] = field(default_factory=dict)


class BlockData:
    """Block registry of one Minecraft version."""

    def __init__(
        self, version: str, blocks: dict[str, BlockInfo],
        tints: dict[str, dict[str, list[float]]] | None = None,
        families: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.version = version
        self._blocks = blocks
        self.tints = tints if tints is not None else {}
        self.families = families if families is not None else {}

    def blocks(self) -> tuple[BlockInfo, ...]:
        """この版の全ブロック情報を登録順で返す。"""
        return tuple(self._blocks.values())

    def __contains__(self, block_id: str) -> bool:
        return block_id in self._blocks

    def __len__(self) -> int:
        return len(self._blocks)

    def get(self, block_id: str) -> BlockInfo | None:
        return self._blocks.get(block_id)

    def check_state(self, state: BlockState) -> str | None:
        """Return an error message if ``state`` is not valid for this version, else None."""
        info = self._blocks.get(state.id)
        if info is None:
            return "Unknown block id."
        for name, value in state.properties:
            allowed = info.properties.get(name)
            if allowed is None:
                known = ", ".join(sorted(info.properties)) or "none"
                return f"Unknown property {name!r} for {state.id} (valid: {known})."
            if value not in allowed:
                return (
                    f"Invalid value {value!r} for property {name!r} (valid: {', '.join(allowed)})."
                )
        return None

    def complete(self, state: BlockState) -> BlockState:
        """Fill unspecified properties with defaults; the state must be valid."""
        info = self._blocks.get(state.id)
        if info is None:
            raise BlueprintError(f"Unknown block id: {state.id}")
        return state.with_defaults(info.default)


def _data_root() -> Traversable:
    return resources.files("mcblueprint").joinpath("data")


@cache
def _read_block_data(filename: str) -> dict[str, Any]:
    return json.loads(_data_root().joinpath("blockdata", filename).read_text(encoding="utf-8"))


@cache
def _versions() -> dict[str, dict[str, int]]:
    lock = json.loads(_data_root().joinpath("lock.json").read_text(encoding="utf-8"))
    versions = {}
    for filename in lock["blockdata"]["sha256"]:
        if filename.startswith("blockdata-") and filename.endswith(".json"):
            raw = _read_block_data(filename)
            versions[raw["minecraftVersion"]] = {"dataVersion": raw["dataVersion"]}
    return dict(sorted(versions.items(), key=lambda item: item[1]["dataVersion"]))


def supported_versions() -> list[str]:
    """Minecraft versions with bundled block data."""
    return list(_versions())


def is_supported(version: str) -> bool:
    return version in _versions()


def data_version(version: str) -> int:
    """The DataVersion stored in schematics for ``version``."""
    try:
        return _versions()[version]["dataVersion"]
    except KeyError:
        raise BlueprintError(_unsupported_message(version)) from None


def version_for_data_version(data_version: int) -> str | None:
    """The bundled version whose DataVersion is exactly ``data_version``."""
    for version, info in _versions().items():
        if info["dataVersion"] == data_version:
            return version
    return None


def nearest_version(data_version: int) -> str:
    """The newest bundled version not newer than ``data_version`` (or the oldest one
    when the file predates every bundled version)."""
    older = [v for v, info in _versions().items() if info["dataVersion"] <= data_version]
    return older[-1] if older else supported_versions()[0]


@cache
def load_block_data(version: str) -> BlockData:
    """Load (once) the block registry for ``version``."""
    if not is_supported(version):
        raise BlueprintError(_unsupported_message(version))
    raw = _read_block_data(f"blockdata-{version}.json")
    blocks = {
        block_id: BlockInfo(
            id=block_id,
            properties={name: tuple(values) for name, values in entry["properties"].items()},
            default=dict(entry["default"]),
            material=entry["material"],
            tint=entry["tint"],
            faces=entry.get("faces", {}),
            family=entry.get("family"),
            shape=entry["shape"],
            weathering=entry.get("weathering", {}),
        )
        for block_id, entry in raw["blocks"].items()
    }
    return BlockData(version, blocks, raw["tints"], raw["families"])


def _unsupported_message(version: str) -> str:
    return (
        f"Unsupported Minecraft version {version!r}. "
        f"Supported versions: {', '.join(supported_versions())}"
    )
