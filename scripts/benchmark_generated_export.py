"""領域生成と単一出力の巨大負荷を1プロセス1条件で測る。"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import platform
import runpy
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

import nbtlib

from mcblueprint.generator import GenerationLimits, generate_regions
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.region_export import export_generated_regions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("castle", "mixed"))
    parser.add_argument("--format", choices=("schem", "litematic"), default="schem")
    parser.add_argument("--profile", type=Path, help="生成を除いた出力処理のcProfile保存先")
    parser.add_argument("--output", type=Path, help="測定JSONの保存先")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    metrics = runpy.run_path(str(root / "scripts/benchmark.py"))
    if args.case == "castle":
        source = (root / "benchmarks/fixtures/profile-castle-huge.json").read_bytes()
        raw = json.loads(source)
    else:
        raw = {
            "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "mixed-256-128-256",
            "seed": 0,
            "palettes": {"wall": [{"block": "stone"}, {"block": "cobblestone", "weight": 2}]},
            "operations": [{
                "type": "fill", "from": [0, 0, 0], "to": [255, 127, 255],
                "palette": "wall", "id": "wall",
            }],
        }
        source = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    started = time.perf_counter()
    bp = load_blueprint_dict(raw)
    generation_seconds = 0.0
    profile = cProfile.Profile() if args.profile else None

    def timed_regions():
        nonlocal generation_seconds
        iterator = generate_regions(bp, limits=GenerationLimits())
        while True:
            if profile:
                profile.disable()
            before = time.perf_counter()
            try:
                region = next(iterator)
            except StopIteration:
                generation_seconds += time.perf_counter() - before
                if profile:
                    profile.enable()
                return
            generation_seconds += time.perf_counter() - before
            if profile:
                profile.enable()
            yield region
            del region

    temporary_root = root / ".pytest_cache"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="region-rss-", dir=temporary_root) as tmp:
        out = Path(tmp) / f"out.{args.format}"
        if profile:
            profile.enable()
        export_generated_regions(timed_regions(), bp, out, format=args.format)
        elapsed = time.perf_counter() - started
        if profile:
            profile.disable()
            args.profile.parent.mkdir(parents=True, exist_ok=True)
            profile.dump_stats(args.profile)
        output_bytes = out.stat().st_size
        peak = metrics["_process_peak_rss"]()
        nbt = nbtlib.load(out)
        if args.format == "schem":
            cells, palette = nbt["BlockData"], nbt["Palette"]
        else:
            region = nbt["Regions"]["Main"]
            cells = region["BlockStates"]
            palette = region["BlockStatePalette"]
        block_data_hash = hashlib.sha256(cells.tobytes()).hexdigest()
        palette_hash = hashlib.sha256(nbtlib.serialize_tag(palette).encode("utf-8")).hexdigest()
    sources = ["region_export.py", "exporters/_spool.py", "exporters/base.py",
               "exporters/schem.py", "exporters/litematic.py", "generator.py", "volume.py"]
    record = {
        "case": args.case, "format": args.format,
        "input_sha256": hashlib.sha256(source).hexdigest(),
        "bounds_cells": bp.bounds().volume, "core": [64, None, 64], "halo": 16,
        "seconds": elapsed, "generation_seconds": generation_seconds,
        "export_seconds": elapsed - generation_seconds,
        "profiled_export": bool(profile),
        "process_peak_rss_bytes": peak, "output_bytes": output_bytes,
        "block_data_sha256": block_data_hash, "palette_sha256": palette_hash,
        "python": platform.python_version(), "platform": platform.platform(),
        "packages": {key: version(key) for key in ("numpy", "nbtlib")},
        "source_sha256": {
            name: hashlib.sha256((root / "src/mcblueprint" / name).read_bytes()).hexdigest()
            for name in sources
        },
    }
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    print(text, end="", flush=True)


if __name__ == "__main__":
    main()
