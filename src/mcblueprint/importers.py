"""Read ``.schem`` (Sponge v2 / v3) and ``.litematic`` files into a BlockVolume, and
turn a BlockVolume back into Blueprint JSON (docs/ARCHITECTURE.md section 14)."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from io import BytesIO
from numbers import Integral
from pathlib import Path
from typing import Any

import nbtlib

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters.litematic import bits_for, iter_bit_array, unpack_bit_array
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.terrain import TerrainSnapshot
from mcblueprint.volume import BlockVolume

AIR_IDS = frozenset({"minecraft:air", "minecraft:cave_air", "minecraft:void_air"})


@dataclass
class ImportedSchematic:
    volume: BlockVolume
    offset: Vec3  # position of the volume's origin relative to the paste point
    data_version: int | None
    name: str | None = None


# --- readers ----------------------------------------------------------------


def read_schematic(path: str | Path) -> ImportedSchematic:
    path = Path(path)
    try:
        nbt = nbtlib.load(str(path))
    except FileNotFoundError:
        raise BlueprintError(f"Schematic file not found: {path}") from None
    except Exception as exc:  # nbtlib raises assorted errors on bad input
        raise BlueprintError(f"Cannot read {path}: {exc}") from exc
    suffix = path.suffix.lower()
    if suffix == ".litematic":
        return _read_litematic(nbt)
    if suffix in (".schem", ".schematic"):
        return _read_schem(nbt)
    raise BlueprintError(f"Unsupported schematic extension {suffix!r} (use .schem or .litematic)")


def _iter_varints(data: Iterable[int], *, strict: bool = False) -> Iterator[int]:
    value, shift = 0, 0
    for byte in data:
        byte = int(byte) & 0xFF
        if strict and (shift >= 35 or (shift == 28 and byte & 0xF8)):
            raise BlueprintError("地形の BlockData に不正な varint があります")
        value |= (byte & 0x7F) << shift
        if byte & 0x80:
            shift += 7
        else:
            yield value
            value, shift = 0, 0
    if strict and shift:
        raise BlueprintError("地形の BlockData の varint が途中で終了しています")


def decode_varints(data: list[int], *, strict: bool = False) -> list[int]:
    return list(_iter_varints(data, strict=strict))


@dataclass
class _SchematicData:
    size: Vec3
    offset: Vec3
    palette: dict[int, BlockState]
    indices: Iterable[int]
    data_version: int | None
    name: str | None = None

    def imported(self) -> ImportedSchematic:
        volume = _fill_volume(self.indices, self.palette, *self.size)
        return ImportedSchematic(volume, self.offset, self.data_version, self.name)


def read_terrain(path: str | Path) -> TerrainSnapshot:
    """.schem / 単一領域 .litematic を地形として読み取る（ファイルは変更しない）。

    air を含む宣言領域と元状態・座標・DataVersion・入力ハッシュを保持する。
    非空のエンティティや保留 tick、破損データ、未対応形式は BlueprintError。
    DataVersion 不明時は版を推測せず None とする。
    """
    path = Path(path)
    if path.suffix.lower() not in (".schem", ".litematic"):
        raise BlueprintError(f"未対応の地形ファイル形式: {path.suffix!r}")
    try:
        raw = path.read_bytes()
        content = gzip.decompress(raw) if raw.startswith(b"\x1f\x8b") else raw
        nbt = nbtlib.File.from_fileobj(BytesIO(content))
        decoded = (
            _decode_litematic(nbt, strict=True)
            if path.suffix.lower() == ".litematic"
            else _decode_schem(nbt, strict=True)
        )
        states = {}
        for i, index in enumerate(decoded.indices):
            state = decoded.palette[index]
            if state.id == "minecraft:air" and not state.properties:
                continue
            y, remainder = divmod(i, decoded.size.x * decoded.size.z)
            z, x = divmod(remainder, decoded.size.x)
            states[Vec3(x, y, z)] = state
        bounds = AABB(Vec3(0, 0, 0), decoded.size - Vec3(1, 1, 1))
        return TerrainSnapshot(
            bounds, decoded.offset, decoded.data_version,
            version_for_data_version(decoded.data_version), hashlib.sha256(raw).hexdigest(), states,
        )
    except BlueprintError:
        raise
    except Exception as exc:
        raise BlueprintError(f"地形ファイルを読み取れません: {path}: {exc}") from exc


def _reject_attached_data(container: Any, target: str) -> None:
    for key in (
        "BlockEntities", "TileEntities", "Entities", "PendingBlockTicks", "PendingFluidTicks",
        "BlockTicks", "FluidTicks",
    ):
        if key in container and len(container[key]):
            raise BlueprintError(f"地形として保持できない付随データ: {target}.{key}")


def _integer(value: Any, target: str, *, strict: bool) -> int:
    if strict and not isinstance(value, Integral):
        raise BlueprintError(f"地形の {target} は整数である必要があります")
    return int(value)


def _validate_cells(size: Vec3, palette: dict[int, BlockState], indices: list[int]) -> None:
    if any(axis <= 0 for axis in size):
        raise BlueprintError(f"地形の宣言寸法は正の整数が必要です: {size}")
    expected = size.x * size.y * size.z
    if len(indices) != expected:
        raise BlueprintError(f"地形のセル数が宣言領域と一致しません: {len(indices)} != {expected}")
    if not palette or any(index < 0 for index in palette):
        raise BlueprintError("地形の palette が空か、不正な index を含んでいます")
    for index in indices:
        if index not in palette:
            raise BlueprintError(f"Block index {index} is not in the palette")


def _read_schem(nbt: Any) -> ImportedSchematic:
    return _decode_schem(nbt).imported()


def _decode_schem(nbt: Any, *, strict: bool = False, compact: bool = False) -> _SchematicData:
    root = nbt["Schematic"] if "Schematic" in nbt and "Version" not in nbt else nbt
    version = _integer(root.get("Version", 2), "Version", strict=strict)
    if version == 3:
        blocks = root["Blocks"]
        palette_tag, data_tag = blocks["Palette"], blocks["Data"]
    elif version in (1, 2):
        palette_tag, data_tag = root["Palette"], root["BlockData"]
    else:
        raise BlueprintError(f"Unsupported Sponge schematic version {version}")
    if strict:
        _reject_attached_data(nbt, "root")
        _reject_attached_data(root, "Schematic")
        if version == 3:
            _reject_attached_data(blocks, "Schematic.Blocks")
    width, height, length = (
        _integer(root[key], key, strict=strict) for key in ("Width", "Height", "Length")
    )
    offset = (
        Vec3.from_seq([_integer(v, "Offset", strict=strict) for v in root["Offset"]])
        if "Offset" in root else Vec3(0, 0, 0)
    )
    palette = {
        _integer(index, f"Palette.{key}", strict=strict): BlockState.parse(str(key))
        for key, index in palette_tag.items()
    }
    if strict and not isinstance(data_tag, nbtlib.ByteArray):
        raise BlueprintError("地形の BlockData / Blocks.Data は ByteArray が必要です")
    indices = ((data_tag if int(data_tag.min(initial=0)) >= 0
                else _iter_varints(data_tag, strict=strict)) if compact
               else decode_varints(list(data_tag), strict=strict))
    expected = width * height * length
    if strict:
        if len(palette) != len(palette_tag):
            raise BlueprintError("地形の palette に重複 index があります")
        if not compact:
            _validate_cells(Vec3(width, height, length), palette, indices)
    elif len(indices) < expected:
        raise BlueprintError(f"Schematic block data is truncated ({len(indices)} < {expected})")
    data_version = (
        _integer(root["DataVersion"], "DataVersion", strict=strict)
        if "DataVersion" in root else None
    )
    return _SchematicData(Vec3(width, height, length), offset, palette, indices, data_version)


def _read_litematic(nbt: Any) -> ImportedSchematic:
    return _decode_litematic(nbt).imported()


def _decode_litematic(nbt: Any, *, strict: bool = False, compact: bool = False) -> _SchematicData:
    regions = nbt["Regions"]
    if len(regions) != 1:
        raise BlueprintError(
            f"Only single-region .litematic files are supported (found {len(regions)})"
        )
    name, region = next(iter(regions.items()))
    if strict:
        _reject_attached_data(nbt, "root")
        _reject_attached_data(region, f"Regions.{name}")
    size = Vec3(*(
        _integer(region["Size"][axis], f"Regions.{name}.Size.{axis}", strict=strict)
        for axis in ("x", "y", "z")
    ))
    position = Vec3(*(
        _integer(region["Position"][axis], f"Regions.{name}.Position.{axis}", strict=strict)
        for axis in ("x", "y", "z")
    ))
    # 負の Size は Position から負方向へ伸びる領域を表す。
    origin = Vec3(
        position.x + (size.x + 1 if size.x < 0 else 0),
        position.y + (size.y + 1 if size.y < 0 else 0),
        position.z + (size.z + 1 if size.z < 0 else 0),
    )
    width, height, length = abs(size.x), abs(size.y), abs(size.z)
    palette = {}
    for index, entry in enumerate(region["BlockStatePalette"]):
        props = entry.get("Properties") or {}
        state = BlockState(
            str(entry["Name"]), tuple(sorted((str(k), str(v)) for k, v in props.items()))
        )
        palette[index] = BlockState.parse(state.to_string()) if strict else state
    bits = bits_for(len(palette))
    if strict:
        expected_words = (width * height * length * bits + 63) // 64
        if len(region["BlockStates"]) != expected_words:
            raise BlueprintError("地形の BlockStates 長が宣言領域と一致しません")
    indices = (iter_bit_array(region["BlockStates"], bits, width * height * length) if compact
               else unpack_bit_array(list(region["BlockStates"]), bits, width * height * length))
    if strict and not compact:
        _validate_cells(Vec3(width, height, length), palette, indices)
    data_version = (
        _integer(nbt["MinecraftDataVersion"], "MinecraftDataVersion", strict=strict)
        if "MinecraftDataVersion" in nbt else None
    )
    meta_name = (
        str(nbt["Metadata"]["Name"]) if "Metadata" in nbt and "Name" in nbt["Metadata"] else None
    )
    return _SchematicData(
        Vec3(width, height, length), origin, palette, indices, data_version, meta_name or str(name)
    )


def read_terrain_regions(path: str | Path, *, max_memory_mb: float = 2048):
    """make用の不変地形spoolを返す。入力配列を含む読取予算超過は展開前に拒否する。

    NBTの版・座標・パレット検証は旧importerと共有し、添字だけを行単位で変換する。
    一時ファイルは返したsnapshotの寿命まで保持される。
    """
    from mcblueprint.generator import GenerationLimits
    from mcblueprint.terrain import RegionTerrainSnapshot

    GenerationLimits(max_memory_mb=max_memory_mb)
    path = Path(path)
    if path.suffix.lower() not in (".schem", ".litematic"):
        raise BlueprintError(f"未対応の地形ファイル形式: {path.suffix!r}")
    budget = max_memory_mb * 1024 * 1024
    try:
        digest = terrain_file_hash(path)
        with path.open("rb") as stream:
            compressed = stream.read(2) == b"\x1f\x8b"
        # nbtlibの配列読込時のbytesと配列、および変換行の余裕を含める。
        opener = gzip.open if compressed else open
        size = 0
        with opener(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                size += len(chunk)
                if size * 3 + 1024 * 1024 > budget:
                    raise BlueprintError("地形の入力配列が limits.max_memory_mb に収まりません")
        with opener(path, "rb") as stream:
            nbt = nbtlib.File.from_fileobj(stream)
        decoded = (_decode_litematic(nbt, strict=True, compact=True)
                   if path.suffix.lower() == ".litematic"
                   else _decode_schem(nbt, strict=True, compact=True))
        preparation = (size * 3 + len(decoded.palette) * 2048
                       + decoded.size.x * 128 + 1024 * 1024)
        if preparation > budget:
            raise BlueprintError("地形の入力配列・変換行が limits.max_memory_mb に収まりません")
        snapshot = RegionTerrainSnapshot(decoded, digest, max_memory_mb)
        if terrain_file_hash(path) != digest:
            raise BlueprintError("読取中に地形入力が変更されています")
        return snapshot
    except BlueprintError:
        raise
    except Exception as exc:
        raise BlueprintError(f"地形ファイルを読み取れません: {path}: {exc}") from exc


def terrain_file_hash(path: str | Path) -> str:
    """入力全体を常駐させず、圧縮されたままのSHA-256を返す。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fill_volume(
    indices: list[int], palette: dict[int, BlockState], width: int, height: int, length: int
) -> BlockVolume:
    volume = BlockVolume()
    i = 0
    for y in range(height):
        for z in range(length):
            for x in range(width):
                state = palette.get(indices[i])
                i += 1
                if state is None:
                    raise BlueprintError(f"Block index {indices[i - 1]} is not in the palette")
                if state.id in AIR_IDS:
                    continue
                volume.set(Vec3(x, y, z), state)
    return volume


# --- volume -> blueprint ------------------------------------------------------


def greedy_boxes(volume: BlockVolume) -> list[tuple[Vec3, Vec3, BlockState]]:
    """Cover every set cell with axis-aligned boxes of one block state (greedy merge)."""
    cells = {pos: state for pos, state in volume}
    done: set[Vec3] = set()
    boxes = []
    for start in sorted(cells, key=lambda p: (p.y, p.z, p.x)):
        if start in done:
            continue
        state = cells[start]
        end = start
        while _same(cells, done, state, end + Vec3(1, 0, 0)):
            end = end + Vec3(1, 0, 0)
        while all(
            _same(cells, done, state, Vec3(x, start.y, end.z + 1))
            for x in range(start.x, end.x + 1)
        ):
            end = end + Vec3(0, 0, 1)
        while all(
            _same(cells, done, state, Vec3(x, end.y + 1, z))
            for x in range(start.x, end.x + 1)
            for z in range(start.z, end.z + 1)
        ):
            end = end + Vec3(0, 1, 0)
        for x in range(start.x, end.x + 1):
            for y in range(start.y, end.y + 1):
                for z in range(start.z, end.z + 1):
                    done.add(Vec3(x, y, z))
        boxes.append((start, end, state))
    return boxes


def _same(cells: dict[Vec3, BlockState], done: set[Vec3], state: BlockState, pos: Vec3) -> bool:
    return pos not in done and cells.get(pos) == state


def to_blueprint(
    imported: ImportedSchematic, *, name: str, minecraft_version: str, description: str | None
) -> dict[str, Any]:
    """Blueprint JSON that regenerates the imported volume at the same paste offset."""
    blocks = (
        blockdata.load_block_data(minecraft_version)
        if blockdata.is_supported(minecraft_version)
        else None
    )
    operations = []
    for start, end, state in greedy_boxes(imported.volume):
        text = _compact(state, blocks)
        a, b = start + imported.offset, end + imported.offset
        if a == b:
            operations.append({"type": "set", "position": a.to_list(), "block": text})
        else:
            operations.append(
                {"type": "fill", "from": a.to_list(), "to": b.to_list(), "block": text}
            )
    if not operations:
        raise BlueprintError("The schematic contains no blocks (only air)")
    data: dict[str, Any] = {
        "formatVersion": 1,
        "minecraftVersion": minecraft_version,
        "name": name,
    }
    if description:
        data["description"] = description
    data["origin"] = [0, 0, 0]
    data["operations"] = operations
    return data


def to_component(
    imported: ImportedSchematic, *, name: str, minecraft_version: str, description: str | None
) -> dict[str, Any]:
    """Component JSON (``components/<name>.json``) whose origin is the volume's minimum
    corner; the paste offset of the file is ignored."""
    blocks = (
        blockdata.load_block_data(minecraft_version)
        if blockdata.is_supported(minecraft_version)
        else None
    )
    bounds = imported.volume.bounds()
    if bounds is None:
        raise BlueprintError("The schematic contains no blocks (only air)")
    operations = []
    for start, end, state in greedy_boxes(imported.volume):
        text = _compact(state, blocks)
        a, b = start - bounds.min, end - bounds.min
        if a == b:
            operations.append({"type": "set", "position": a.to_list(), "block": text})
        else:
            operations.append(
                {"type": "fill", "from": a.to_list(), "to": b.to_list(), "block": text}
            )
    data: dict[str, Any] = {"formatVersion": 1, "name": name}
    if description:
        data["description"] = description
    data["operations"] = operations
    return data


def _compact(state: BlockState, blocks: blockdata.BlockData | None) -> str:
    """Drop properties equal to the block's default so the JSON stays short."""
    if blocks is None:
        return state.to_string()
    info = blocks.get(state.id)
    if info is None:
        return state.to_string()
    props = tuple((k, v) for k, v in state.properties if info.default.get(k) != v)
    return BlockState(state.id, props).to_string()


def version_for_data_version(data_version: int | None) -> str | None:
    if data_version is None:
        return None
    return blockdata.version_for_data_version(data_version)
