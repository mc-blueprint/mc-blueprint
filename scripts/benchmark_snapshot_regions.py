"""合成snapshotの入力デコード・施工計画・全core生成を独立プロセスでRSS測定する。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import platform
import runpy
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path

import nbtlib as nbt
import numpy as np

from mcblueprint.config import DEFAULTS
from mcblueprint.exporters.litematic import pack_bit_array
from mcblueprint.generator import GenerationLimits, generate_regions
from mcblueprint.importers import read_terrain_regions
from mcblueprint.validator import validate_and_load


def fixture(path: Path, size: tuple[int, int, int]) -> None:
    width, height, length = size
    cells = np.zeros((height, length, width), dtype=np.int8)
    cells[:height // 2] = 1
    offset = (-width // 2, -16, -length // 2)
    if path.suffix == ".schem":
        root = nbt.Compound({
            "Version": nbt.Int(2), "DataVersion": nbt.Int(4671),
            "Width": nbt.Short(width), "Height": nbt.Short(height), "Length": nbt.Short(length),
            "Offset": nbt.IntArray(offset),
            "Palette": nbt.Compound({"minecraft:air": nbt.Int(0), "minecraft:stone": nbt.Int(1)}),
            "BlockData": nbt.ByteArray(cells.reshape(-1)),
        })
    else:
        def vec(values):
            return nbt.Compound({k: nbt.Int(v) for k, v in zip("xyz", values, strict=True)})

        root = nbt.Compound({
            "MinecraftDataVersion": nbt.Int(4671), "Regions": nbt.Compound({
                "Main": nbt.Compound({
                    "Size": vec(size), "Position": vec(offset),
                    "BlockStatePalette": nbt.List[nbt.Compound]([
                        nbt.Compound({"Name": nbt.String("minecraft:air")}),
                        nbt.Compound({"Name": nbt.String("minecraft:stone")}),
                    ]), "BlockStates": nbt.LongArray(pack_bit_array(cells.reshape(-1), 2)),
                }),
            }),
        })
    with path.open("wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", mtime=0,
                                                  filename="") as compressed:
        nbt.File(root).write(compressed)


def worker(path: Path, budget: int) -> dict:
    started = time.perf_counter()
    snapshot = read_terrain_regions(path, max_memory_mb=budget)
    size = snapshot.bounds.size
    floor = snapshot.offset.y + size.y // 2
    raw = {
        "from": [-8, floor, -8], "to": [7, floor, 7],
        "scan": {"minY": snapshot.offset.y, "maxY": snapshot.source_bounds.max.y},
        "maxCut": 4, "maxFill": 4, "maxSupportDepth": 4,
        "ground": ["stone", "cobblestone"], "block": "cobblestone",
    }
    road = {
        **{k: v for k, v in raw.items() if k not in ("from", "to")},
        "points": [[-16, -10], [7, -10]], "entrance": [8, floor + 1, -10],
        "width": 3, "clearance": 2, "stairs": "stone_brick_stairs",
    }
    config = deepcopy(DEFAULTS)
    config["limits"]["max_memory_mb"] = budget
    blueprint, errors = validate_and_load({
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "snapshot-rss",
        "terrain": {"source": str(path), "region": {
            "from": snapshot.source_bounds.min.to_list(),
            "to": snapshot.source_bounds.max.to_list(),
        }}, "foundation": raw, "surfacePath": road,
        "metadata": {"generation": {"mode": "buildspec-v1", "config": config,
                                    "terrainHash": snapshot.input_sha256}},
        "operations": [{"type": "set", "position": snapshot.source_bounds.min.to_list(),
                        "block": "stone"}],
    }, terrain_snapshot=snapshot)
    assert not errors, errors
    prepared = time.perf_counter() - started
    cores = cells = 0
    for region in generate_regions(blueprint, limits=GenerationLimits((32, 16, 32), 4, budget)):
        cores += 1
        cells += region.core.volume
        del region
    assert cells == snapshot.bounds.volume
    metrics = runpy.run_path(str(Path(__file__).with_name("benchmark.py")))
    return {
        "format": path.suffix[1:], "dimensions": list(size), "offset": list(snapshot.offset),
        "input_sha256": snapshot.input_sha256, "max_memory_mb": budget,
        "core": [32, 16, 32], "halo": 4, "cores": cores, "cells": cells,
        "prepare_seconds": prepared, "seconds": time.perf_counter() - started,
        "process_peak_rss_bytes": metrics["_process_peak_rss"](),
        "foundation_changed_cells": blueprint.foundation.result.changed_cells,
        "road_changed_cells": blueprint.surface_path.result.changed_cells,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--max-memory-mb", type=int, default=128)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker, args.max_memory_mb)))
        return
    records = []
    with tempfile.TemporaryDirectory(prefix="snapshot-rss-") as tmp:
        for size in ((64, 48, 64), (256, 128, 256)):
            for fmt in ("schem", "litematic"):
                path = Path(tmp) / f"source.{fmt}"
                fixture(path, size)
                result = subprocess.run([
                    sys.executable, "-X", "utf8", __file__, "--worker", str(path),
                    "--max-memory-mb", str(args.max_memory_mb),
                ], check=True, capture_output=True, text=True, encoding="utf-8")
                record = json.loads(result.stdout)
                records.append(record)
                print(json.dumps(record), flush=True)
    result = {
        "definition": "snapshot-regions-v1", "python": platform.python_version(),
        "platform": platform.platform(), "numpy": np.__version__, "nbtlib": nbt.__version__,
        "scope": ("圧縮入力の検査・NBTデコード・spool・造成/道路計画・全core生成。"
                  "Weather/出力は除外。"),
        "records": records,
        "source_sha256": {
            name: hashlib.sha256((Path(__file__).resolve().parents[1]
                                  / "src/mcblueprint" / name).read_bytes()).hexdigest()
            for name in ("importers.py", "terrain.py", "foundation.py", "surface_path.py",
                         "generator.py", "loader.py", "validator.py", "exporters/litematic.py")
        },
    }
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
