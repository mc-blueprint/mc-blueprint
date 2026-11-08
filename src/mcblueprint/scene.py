"""Build a viewer scene from completed block and attribute spools."""

from __future__ import annotations

import base64
import gzip
import json
from hashlib import sha256
from pathlib import Path

import numpy as np

from mcblueprint import __version__, blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB
from mcblueprint.plan_contract import building_for_element
from mcblueprint.volume import EMPTY_ATTRIBUTES


def capture_sources(regions, bounds: AABB, path: Path, context: dict,
                    table: dict[int, dict]):
    """weather後のcore属性を一時spoolへ退避し、同じregionをblock spoolへ渡す。"""
    with path.open("w+b") as stream:
        stream.truncate(bounds.volume * 4)
        for region in regions:
            core = region.core
            low = core.min - region.volume.region.min
            size = core.size
            source_ids = {0: 0}
            for y in range(size.y):
                for z in range(size.z):
                    indices = region.volume._attrs[
                        low.y + y, low.z + z, low.x:low.x + size.x,
                    ]
                    row = np.zeros(size.x, dtype="<u4")
                    for index in np.unique(indices):
                        if index not in source_ids:
                            attr = region.volume._attribute_palette[int(index)]
                            descriptor = (_source_descriptor(attr, context)
                                          if attr != EMPTY_ATTRIBUTES else None)
                            ident = 0
                            if descriptor is not None:
                                raw = json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")).encode("utf-8")
                                ident = int.from_bytes(sha256(raw).digest()[:4], "little") or 1
                                descriptor["id"] = ident
                                collision = table.get(ident)
                                if collision is not None and collision != descriptor:
                                    raise BlueprintError("生成元IDが衝突しました")
                                table[ident] = descriptor
                            source_ids[index] = ident
                        ident = source_ids[index]
                        row[indices == index] = ident
                    offset = (((core.min.y + y - bounds.min.y) * bounds.size.z
                               + core.min.z - bounds.min.z + z) * bounds.size.x
                              + core.min.x - bounds.min.x) * 4
                    stream.seek(offset)
                    stream.write(row.tobytes())
            yield region


def _source_descriptor(attr, context: dict) -> dict | None:
    if not (attr.element or attr.source or attr.operation):
        return None
    descriptor = {}
    if attr.element:
        descriptor["element"] = attr.element
    if attr.source:
        descriptor["spec"] = attr.source
        descriptor["label"] = attr.source
    if attr.operation:
        descriptor["path"] = attr.operation
    massing = context.get("massing", {})
    building = building_for_element(massing, attr.element) if massing and attr.element else None
    if building is not None:
        descriptor["element"] = building["element"]
        descriptor["params"] = sorted(building["params"])
    return descriptor


def write_scene(path: Path, source_spool: Path, spool: RegionSpool, sources: dict,
                blueprint: Blueprint, context: dict, source: dict, preview: dict,
                biome: str) -> str:
    """完成spoolをgzip scene v1へ出力し、展開JSONのSHA-256を返す。"""
    bounds = spool.bounds
    size = bounds.size
    data = blockdata.load_block_data(blueprint.minecraft_version)
    states = [state for state in spool.palette[1:]
              if state is not None and state.id != "minecraft:air"]
    completed = {state: data.complete(state) for state in states}
    ordered = [None, *sorted(set(completed.values()), key=lambda state: state.to_string())]
    indexes = {state: index for index, state in enumerate(ordered)}
    palette_index = {None: 0, **{state: indexes[completed[state]] for state in states}}
    palette_index.update({state: 0 for state in spool.palette[1:]
                          if state is not None and state.id == "minecraft:air"})
    palette = [{"state": "minecraft:air", "render": "invisible", "emission": 0,
                "opacity": 0, "tint": "none"}]
    for state in ordered[1:]:
        emission, opacity, render, tint, tint_color = data.drawing(state)
        entry = {"state": state.to_string(), "render": render, "emission": emission,
                 "opacity": opacity, "tint": tint}
        if tint_color is not None:
            entry["tintColor"] = tint_color
        palette.append(entry)

    def chunks():
        with spool.path.open("rb") as blocks, source_spool.open("rb") as source_stream:
            for cy in range(0, size.y, 16):
                for cz in range(0, size.z, 16):
                    for cx in range(0, size.x, 16):
                        sx, sy, sz = min(16, size.x-cx), min(16, size.y-cy), min(16, size.z-cz)
                        bi = np.zeros((sy, sz, sx), dtype="<u4")
                        si = np.zeros((sy, sz, sx), dtype="<u4")
                        for y in range(sy):
                            for z in range(sz):
                                offset = (((cy + y) * size.z + cz + z) * size.x + cx) * 4
                                blocks.seek(offset)
                                indexes = np.frombuffer(blocks.read(sx * 4), dtype="<u4")
                                bi[y, z] = [palette_index[spool.palette[int(i)]] for i in indexes]
                                source_stream.seek(offset)
                                si[y, z] = np.frombuffer(source_stream.read(sx * 4), dtype="<u4")
                        if not np.any(bi):
                            continue
                        block_bytes = bi.astype(
                            "<u2" if len(palette) <= 65536 else "<u4",
                        ).tobytes()
                        yield {"at": [cx, cy, cz],
                               "blocks": base64.b64encode(block_bytes).decode("ascii"),
                               "sources": base64.b64encode(si.tobytes()).decode("ascii")}
    tint_values = {
        key: dict(zip(("l", "a", "b"), data.tints[key][biome], strict=True))
        for key in ("grass", "foliage", "water")
    }
    prefix = {
        "formatVersion": 1, "minecraftVersion": blueprint.minecraft_version,
        "engine": {"version": __version__, "commit": "working-tree"},
        "source": source, "size": size.to_list(), "origin": bounds.min.to_list(),
        "palette": palette,
    }
    suffix = {
        "indexWidth": 32 if len(palette) > 65536 else 16,
        "sources": [sources[key] for key in sorted(sources)], "blockEntities": [],
        "environment": {"biome": biome, "tints": tint_values},
        "preview": preview,
    }
    digest = sha256()
    def emit(stream, value: bytes):
        stream.write(value)
        digest.update(value)

    with path.open("wb") as output:
        with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
            emit(compressed, b"{")
            for key, value in prefix.items():
                emit(compressed, json.dumps(key).encode() + b":"
                     + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                     + b",")
            emit(compressed, b'"chunks":[')
            for index, chunk in enumerate(chunks()):
                if index:
                    emit(compressed, b",")
                emit(compressed, json.dumps(chunk, separators=(",", ":")).encode())
            emit(compressed, b"],")
            for index, (key, value) in enumerate(suffix.items()):
                if index:
                    emit(compressed, b",")
                emit(compressed, json.dumps(key).encode() + b":"
                     + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
            emit(compressed, b"}\n")
    return digest.hexdigest()
