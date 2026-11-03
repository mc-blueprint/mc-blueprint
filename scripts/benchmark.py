"""mc-blueprint 段階別ベンチマークを測定・記録する。"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from mcblueprint import __version__
from mcblueprint.exporters.schem import SchemExporter
from mcblueprint.generator import generate
from mcblueprint.loader import load_blueprint_dict, read_blueprint_json
from mcblueprint.model.vec import AABB
from mcblueprint.preview import Options, render
from mcblueprint.support import check_support
from mcblueprint.validator import validate

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures"
RECORD = ROOT / "benchmarks" / "baseline.json"
STAGES = (
    "read_parse", "validation", "model_load", "bounds_expansion", "generation",
    "support", "render", "output", "cli_validate", "cli_preview", "cli_build",
    "single_pass",
)


def _cases() -> list[dict[str, Any]]:
    cases = []
    kinds = ("sparse", "terrain", "air", "repeat")
    for path in sorted(FIXTURES.glob("*.json")):
        name = path.stem
        kind = next((kind for kind in kinds if name.startswith(kind + "-")), "unknown")
        cases.append(
            {
                "name": name,
                "kind": {
                    "sparse": "sparse",
                    "terrain": "dense_terrain",
                    "air": "air_region",
                    "repeat": "repeated_operation",
                }.get(kind, kind),
                "scale": int(name.rsplit("-", 1)[1]),
            }
        )
    return cases


def _generate_fixtures() -> None:
    cases: list[dict[str, Any]] = []
    for count in (16, 64, 256):
        cases.append(
            {
                "formatVersion": 1, "minecraftVersion": "1.21.11",
                "name": f"sparse-{count}",
            }
        )
        cases[-1]["operations"] = [
            {
                "type": "set", "position": [3 * (index % 16), 0, 3 * (index // 16)],
                "block": "minecraft:stone",
            }
            for index in range(count)
        ]
    for side, height in ((8, 4), (16, 4), (24, 8)):
        cases.append(
            {
                "formatVersion": 1, "minecraftVersion": "1.21.11",
                "name": f"terrain-{side}",
                "operations": [{
                    "type": "fill", "from": [0, 0, 0], "to": [side - 1, height - 1, side - 1],
                    "block": "minecraft:stone",
                }],
            }
        )
    for side in (8, 16, 24):
        cases.append(
            {
                "formatVersion": 1, "minecraftVersion": "1.21.11",
                "name": f"air-{side}",
                "operations": [{
                    "type": "fill", "from": [0, 0, 0], "to": [side - 1] * 3,
                    "block": "minecraft:air",
                }],
            }
        )
    for count in (8, 32, 64):
        cases.append(
            {
                "formatVersion": 1, "minecraftVersion": "1.21.11",
                "name": f"repeat-{count}",
                "operations": [{
                    "type": "repeat", "count": count, "offset": [3, 0, 0],
                    "operations": [{
                        "type": "fill", "from": [0, 0, 0], "to": [1, 1, 1],
                        "block": "minecraft:stone",
                    }],
                }],
            }
        )
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for case in cases:
        (FIXTURES / f"{case['name']}.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


def _bounds(blueprint: Any) -> AABB:
    from functools import reduce

    return reduce(AABB.union, (operation.bounds() for operation in blueprint.operations))


def _operation_count(operations: list[Any]) -> int:
    total = 0
    for operation in operations:
        total += 1
        children = getattr(operation, "operations", None)
        if children:
            total += _operation_count(children)
    return total


def _measure(data: dict[str, Any], stage: str, memory: bool) -> tuple[float, int | None]:
    """Measure one stage in isolation; return seconds and peak traced Python bytes."""
    path = FIXTURES / f"{data['name']}.json"
    temporary_root = FIXTURES.parent / ".tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    output_path = temporary_root / f"{data['name']}-{stage}.schem"
    output_dir = temporary_root / f"{data['name']}-{stage}-output"
    if stage == "read_parse":
        action: Callable[[], Any] = partial(read_blueprint_json, path)
    elif stage == "validation":
        raw = read_blueprint_json(path)
        action = partial(validate, raw)
    elif stage == "model_load":
        raw = read_blueprint_json(path)
        action = partial(load_blueprint_dict, raw)
    elif stage == "bounds_expansion":
        raw = read_blueprint_json(path)

        def action() -> AABB:
            return _bounds(load_blueprint_dict(raw))

    elif stage in {"generation", "support", "render", "output"}:
        raw = read_blueprint_json(path)
        blueprint = load_blueprint_dict(raw)
        volume = generate(blueprint)
        if stage == "generation":
            cold_blueprint = load_blueprint_dict(raw)
            action = partial(generate, cold_blueprint)
        elif stage == "support":
            action = partial(check_support, volume)
        elif stage == "render":
            action = partial(
                render, volume, "top",
                options=Options(scale=1, version=blueprint.minecraft_version),
            )
        else:
            action = partial(SchemExporter().export, volume, blueprint, output_path)
    elif stage in {"cli_validate", "cli_preview", "cli_build", "single_pass"}:
        from mcblueprint.cli import main

        def action() -> None:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                if stage == "cli_validate":
                    code = main(["validate", str(path)])
                elif stage == "cli_preview":
                    code = main(
                        [
                            "preview", str(path), "--views", "top", "--scale", "1",
                            "-o", str(output_dir),
                        ]
                    )
                elif stage == "cli_build":
                    code = main(["build", str(path), "-o", str(output_dir)])
                else:
                    _single_pass(path, output_dir)
                    return
            if code != 0:
                raise ValueError(f"CLI benchmark command returned {code}: {stage}")
    else:
        raise ValueError(f"Unknown stage: {stage}")

    if memory:
        tracemalloc.start()
    started = time.perf_counter()
    action()
    elapsed = time.perf_counter() - started
    peak = None
    if memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    output_path.unlink(missing_ok=True)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    return elapsed, peak


def _reset_caches() -> None:
    from mcblueprint import blockdata, preview, schema

    blockdata._versions.cache_clear()
    blockdata._read_block_data.cache_clear()
    blockdata.load_block_data.cache_clear()
    preview._block_color.cache_clear()
    preview._material_colors.cache_clear()
    schema.load_schema.cache_clear()
    schema.load_component_schema.cache_clear()


def _single_pass(path: Path, output_dir: Path) -> None:
    raw = read_blueprint_json(path)
    errors = validate(raw)
    if errors:
        raise ValueError(f"Benchmark fixture is invalid: {errors}")
    blueprint = load_blueprint_dict(raw)
    volume = generate(blueprint)
    check_support(volume)
    image = render(
        volume, "top", options=Options(
            scale=1, origin=blueprint.origin, version=blueprint.minecraft_version,
        ),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    image.save(output_dir / f"{blueprint.name}-top.png")
    SchemExporter().export(volume, blueprint, output_dir / f"{blueprint.name}.schem")


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    path = FIXTURES / f"{data['name']}.json"
    raw = read_blueprint_json(path)
    blueprint = load_blueprint_dict(raw)
    bounds = _bounds(blueprint)
    volume = generate(blueprint)
    temporary_root = FIXTURES.parent / ".tmp"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="mcblueprint-benchmark-metadata-", dir=temporary_root
    ) as directory:
        output = Path(directory) / "benchmark.schem"
        SchemExporter().export(volume, blueprint, output)
        output_bytes = output.stat().st_size
    return {
        "name": data["name"],
        "kind": data["kind"],
        "scale": data["scale"],
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "input_bytes": path.stat().st_size,
        "cells": len(volume),
        "bounds_size": bounds.size.to_list(),
        "bounds_volume": bounds.volume,
        "operation_count": _operation_count(blueprint.operations),
        "output_bytes": output_bytes,
    }


def _worker(name: str, stage: str, repetitions: int) -> dict[str, Any]:
    data = next(case for case in _cases() if case["name"] == name)
    samples = []
    for _ in range(repetitions):
        _reset_caches()
        seconds, _ = _measure(data, stage, memory=False)
        samples.append(seconds)
    _reset_caches()
    _, peak = _measure(data, stage, memory=True)
    return {
        "seconds_median": statistics.median(samples),
        "samples_seconds": samples,
        "python_peak_bytes": peak,
    }


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RECORD)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--generate-fixtures", action="store_true")
    parser.add_argument("--worker", nargs=3, metavar=("CASE", "STAGE", "REPETITIONS"))
    args = parser.parse_args()
    if args.generate_fixtures:
        _generate_fixtures()
        return 0
    if args.worker:
        print(json.dumps(_worker(args.worker[0], args.worker[1], int(args.worker[2]))))
        return 0
    if args.repetitions < 1:
        parser.error("--repetitions は1以上を指定してください")

    results = []
    for data in _cases():
        case_result = _metadata(data)
        case_result["stages"] = {}
        for stage in STAGES:
            print(f"測定中: {data['name']} / {stage}", file=sys.stderr, flush=True)
            command = [
                sys.executable, str(Path(__file__).resolve()), "--worker", data["name"],
                stage, str(args.repetitions),
            ]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            response = subprocess.run(
                command, check=True, capture_output=True, text=True, env=env
            )
            case_result["stages"][stage] = json.loads(response.stdout)
            stage_result = case_result["stages"][stage]
            print(
                f"完了: {data['name']} / {stage} "
                f"({stage_result['seconds_median']:.4f}s)",
                file=sys.stderr,
                flush=True,
            )
        results.append(case_result)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "environment": {
                    "os": platform.platform(),
                    "python": platform.python_version(),
                    "mcblueprint": __version__,
                    "repetitions": args.repetitions,
                    "memory_metric": (
                        "tracemalloc peak Python allocations per isolated stage process"
                    ),
                    "note": "段階ごとに独立実行。各段階は準備処理を計時外に行う。",
                },
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
