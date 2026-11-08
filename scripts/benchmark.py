"""mc-blueprint 段階別ベンチマークを測定・記録する。"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import ctypes.wintypes
import hashlib
import io
import json
import os
import platform
import re
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
        if name.startswith("profile-"):
            continue
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


def _process_peak_rss() -> int | None:
    """Return this process's peak working set in bytes, or None when unavailable."""
    if sys.platform == "win32":

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            ctypes.wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
        if psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.PeakWorkingSetSize)
        return None
    if sys.platform == "linux":
        try:
            return int(Path("/proc/self/status").read_text().split("VmHWM:")[1].split()[0]) * 1024
        except (OSError, IndexError, ValueError):
            return None
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except (ImportError, OSError, ValueError):
        return None


def _full_worker(name: str) -> dict[str, Any]:
    """Run the complete generation path once in a process dedicated to this fixture."""
    path = FIXTURES / f"{name}.json"
    if not path.is_file():
        raise ValueError(f"Unknown benchmark fixture: {name}")
    raw = read_blueprint_json(path)
    errors = validate(raw)
    if errors:
        raise ValueError(f"Benchmark fixture is invalid: {errors}")
    blueprint = load_blueprint_dict(raw)
    volume = generate(blueprint)
    check_support(volume)
    output = FIXTURES.parent / ".tmp" / f"{name}-full.schem"
    output.parent.mkdir(parents=True, exist_ok=True)
    SchemExporter().export(volume, blueprint, output)
    result = {
        "process_peak_rss_bytes": _process_peak_rss(),
        "cells": len(volume),
        "output_bytes": output.stat().st_size,
    }
    output.unlink(missing_ok=True)
    return result


def _core_worker(name: str) -> dict[str, Any]:
    """Run the Blueprint-compatible generation core, Weather and streamed export."""
    from mcblueprint import blockdata
    from mcblueprint.config import DEFAULTS
    from mcblueprint.generator import GenerationLimits, generate_regions
    from mcblueprint.region_export import export_generated_regions
    from mcblueprint.validator import validate_and_load
    from mcblueprint.weather import apply_weather, required_halo

    path = FIXTURES / f"{name}.json"
    raw = read_blueprint_json(path)
    blueprint, errors = validate_and_load(raw, base_dir=path.parent)
    if errors:
        raise ValueError(f"Benchmark fixture is invalid: {errors}")
    config = DEFAULTS
    limits = GenerationLimits(**{**config["limits"], "region": tuple(config["limits"]["region"])})
    blocks = blockdata.load_block_data(blueprint.minecraft_version)
    bounds = blueprint.bounds()
    output = FIXTURES.parent / ".tmp" / f"{name}-core.schem"
    output.parent.mkdir(parents=True, exist_ok=True)
    cells = 0

    def regions():
        nonlocal cells
        for region in generate_regions(
            blueprint, limits=limits,
            reference_radius=required_halo(config["weathering"]),
        ):
            cells += len(region.volume)
            apply_weather(
                region, blocks, seed=blueprint.seed, config=config["weathering"],
                ground_height=blueprint.origin.y, randomness=config["randomness"],
                max_memory_mb=limits.max_memory_mb,
            )
            yield region

    export_generated_regions(regions(), blueprint, output, format="schem")
    result = {
        "process_peak_rss_bytes": _process_peak_rss(),
        "bounds": bounds.size.to_list(),
        "cells": cells,
        "output_bytes": output.stat().st_size,
    }
    output.unlink(missing_ok=True)
    return result


def _make_worker(
    archetype: str, scale: str, version: str, output_format: str, output_path: str
) -> dict[str, Any]:
    """Run make in one process and capture stage-hook time/RSS snapshots."""
    import yaml

    from mcblueprint.pipeline import make

    fixture = ROOT / "tests" / "data" / "buildspec" / (
        "single-house.yaml" if archetype == "house" else "standalone-tower.yaml"
    )
    spec = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    spec["minecraftVersion"] = version
    spec["scale"] = scale
    size = {
        "tiny": [32, 32, 32], "medium": [64, 48, 64], "large": [128, 96, 128],
    }[scale]
    spec["size"] = size
    if archetype == "house" and scale != "tiny":
        edge = size[0] - 3
        spec.setdefault("elements", {})["house#1"] = {"width": edge, "depth": edge}
    elif archetype == "tower" and scale != "tiny":
        floors = 8 if scale == "medium" else 17
        radius = size[0] // 2 - 3
        spec["features"].update(shape="square", floors=floors, floor_height=5)
        spec.setdefault("elements", {})["tower#1"] = {"radius": radius}
    with tempfile.TemporaryDirectory(prefix="mcblueprint-make-bench-") as temporary:
        folder = Path(temporary)
        source = folder / "spec.yaml"
        source.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
        input_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        output = Path(output_path)
        snapshots = []
        start = time.perf_counter()

        def hook(stage: str) -> None:
            snapshots.append({
                "stage": stage,
                "elapsed_seconds": time.perf_counter() - start,
                "rss_at_stage_start_bytes": _process_rss(),
            })

        result = make(source, output=output, stage_hook=hook)
        if result.code:
            raise ValueError(f"make failed: {result.diagnostics}")
        run_record = json.loads(result.record.read_text(encoding="utf-8"))
        return {
            "process_peak_rss_bytes": _process_peak_rss(),
            "seconds": time.perf_counter() - start,
            "output_bytes": output.stat().st_size,
            "output_path": _record_path(output),
            "input_sha256": input_hash,
            "config_sha256": hashlib.sha256(json.dumps(
                run_record["config"], sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "stage_hook_samples": snapshots,
        }


def _record_path(path: Path | str) -> str:
    path = Path(path)
    if path == Path(sys.executable):
        return "<python>"
    if path.is_relative_to(ROOT):
        return path.relative_to(ROOT).as_posix()
    codex = Path.home() / ".codex"
    if path.is_relative_to(codex):
        return "<codex>/" + path.relative_to(codex).as_posix()
    if path.is_relative_to(Path(sys.base_prefix)):
        relative = path.relative_to(Path(sys.base_prefix))
        if relative.parts and relative.parts[0].lower() == "lib":
            relative = Path(*relative.parts[1:])
        return "<stdlib>/" + relative.as_posix()
    if path.is_relative_to(Path(tempfile.gettempdir())):
        relative = path.relative_to(Path(tempfile.gettempdir()))
        value = "<tmp>" if not relative.parts else "<tmp>/" + relative.as_posix()
        return _record_user(value)
    return _record_user(path.name)


def _record_user(value: str) -> str:
    return re.sub(re.escape(Path.home().name), "<user>", value, flags=re.IGNORECASE)


def _record_text(value: str) -> str:
    root = str(ROOT)
    value = value.replace(root, ".").replace(root.replace("\\", "/"), ".")

    def replace_path(match: re.Match[str]) -> str:
        path = match.group(0).rstrip(".:;")
        suffix = match.group(0)[len(path):]
        normalized = path.replace("\\", "/")
        lowered = normalized.lower()
        if lowered.endswith(("python.exe", "/python")):
            replacement = "<python>"
        elif "/appdata/local/temp/" in lowered:
            replacement = "<tmp>/" + normalized[lowered.index("/appdata/local/temp/")
                                                   + len("/appdata/local/temp/"):]
        elif "/.codex/" in lowered:
            replacement = "<codex>/" + normalized[lowered.index("/.codex/")
                                                       + len("/.codex/"):]
        elif "/lib/" in lowered and ("/python" in lowered or "/pythoncore" in lowered):
            replacement = "<stdlib>/" + normalized[lowered.index("/lib/") + len("/lib/"):]
        elif re.match(r"(?i)^[a-z]:/users/[^/]+/", normalized):
            replacement = "<home>/" + normalized.split("/", 3)[3]
        elif re.match(r"(?i)^/(?:users|home)/[^/]+/", normalized):
            replacement = "<home>/" + normalized.split("/", 3)[3]
        else:
            replacement = "<path>"
        return replacement + suffix

    value = re.sub(
        r"(?i)(?<![\w])(?:[a-z]:[\\/][^\s\"'<>|,;)]+|/(?:Users|home)/[^\s\"'<>|,;)]+)",
        replace_path,
        value,
    )
    return _record_user(value)


def _process_rss() -> int | None:
    """Return current process working set (Windows) for stage-start snapshots."""
    if sys.platform != "win32":
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD), ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_process = kernel32.GetCurrentProcess
    get_process.restype = ctypes.wintypes.HANDLE
    get_memory = psapi.GetProcessMemoryInfo
    get_memory.argtypes = [
        ctypes.wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), ctypes.wintypes.DWORD,
    ]
    get_memory.restype = ctypes.wintypes.BOOL
    if not get_memory(get_process(), ctypes.byref(counters), counters.cb):
        return None
    return int(counters.WorkingSetSize)


def _profile_environment() -> dict[str, Any]:
    memory = None
    gpu = None
    gpu_reason = None
    cpu = platform.processor() or None
    cpu_reason = None if cpu else "platform.processor()がCPU名を返しませんでした。"
    if sys.platform == "win32":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.wintypes.DWORD),
                ("dwMemoryLoad", ctypes.wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory = int(status.ullTotalPhys)
        try:
            device_result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "@(Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name; "
                    "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) "
                    "-join '|'",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            devices = device_result.stdout.strip().split("|", maxsplit=1)
            cpu = devices[0].strip() or cpu
            gpu = devices[1].strip() if len(devices) > 1 else None
            cpu_reason = None if cpu else "Win32_ProcessorからCPU名を取得できませんでした。"
            if gpu is None:
                gpu_reason = "Win32_VideoControllerからGPU名を取得できませんでした。"
        except (OSError, subprocess.CalledProcessError) as exc:
            gpu_reason = f"GPU名取得に失敗しました: {type(exc).__name__}"
            cpu_reason = f"CPU名取得に失敗しました: {type(exc).__name__}"
    else:
        gpu_reason = "このOS向けのGPU名取得を実装していません。"
    return {
        "os": platform.platform(),
        "cpu": cpu,
        "cpu_reason": cpu_reason,
        "logical_cpu_count": os.cpu_count(),
        "memory_total_bytes": memory,
        "memory_reason": None
        if memory is not None
        else "OSから物理メモリ総量を取得できませんでした。",
        "gpu": gpu,
        "gpu_reason": gpu_reason,
        "python": sys.version,
        "python_version": platform.python_version(),
    }


def _run_profile_benchmark(output: Path) -> int:
    record_path = ROOT / "benchmarks" / "generation-profiles.json"
    definition = json.loads(record_path.read_text(encoding="utf-8"))
    results = []
    for profile in definition["profiles"]:
        fixture = FIXTURES / f"{profile['name']}.json"
        actual_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
        if actual_hash != profile["input_sha256"]:
            raise ValueError(f"Fixture hash mismatch: {profile['name']}")
        samples = []
        for index in range(3):
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--full-worker",
                profile["name"],
            ]
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            started = time.perf_counter()
            response = subprocess.run(command, capture_output=True, text=True, env=env)
            elapsed = time.perf_counter() - started
            try:
                worker = json.loads(response.stdout) if response.returncode == 0 else {}
                reason = None if response.returncode == 0 else _record_text(response.stderr[-2000:])
            except json.JSONDecodeError:
                worker = {}
                reason = _record_text(
                    f"worker出力を解析できませんでした: {response.stderr[-2000:]}"
                )
            samples.append(
                {
                    "seconds": elapsed,
                    "process_peak_rss_bytes": worker.get("process_peak_rss_bytes"),
                    "cells": worker.get("cells"),
                    "output_bytes": worker.get("output_bytes"),
                    "error_reason": reason,
                    "rss_reason": None
                    if worker.get("process_peak_rss_bytes") is not None
                    else (reason or "workerからRSS値を取得できませんでした。"),
                }
            )
            print(f"{profile['name']} {index + 1}/3: {elapsed:.3f}s", file=sys.stderr, flush=True)
        rss_values = [
            sample["process_peak_rss_bytes"]
            for sample in samples
            if sample["process_peak_rss_bytes"] is not None
        ]
        results.append(
            {
                **profile,
                "repetitions": 3,
                "samples": samples,
                "seconds_median": statistics.median(sample["seconds"] for sample in samples),
                "process_peak_rss_bytes_median": statistics.median(rss_values)
                if rss_values
                else None,
                "process_peak_rss_mib_median": statistics.median(rss_values) / (1024 * 1024)
                if rss_values
                else None,
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "definition_version": definition["definition_version"],
        "environment": _profile_environment(),
        "provenance": _benchmark_provenance(),
        "time_scope": (
            "独立子プロセスの起動から読込・検証・モデル構築・生成・support検査・"
            "schem出力完了まで。"
        ),
        "memory_metric": (
            "独立子プロセス全体のPeakWorkingSetSize（Windows）またはOSの最大RSS。"
            "tracemallocは使用しない。"
        ),
        "viewer_reference_note": (
            "viewer参照は負荷種別の対応だけを示す。scene形式・時間境界・"
            "RSS集計方法が異なるため数値は直接比較しない。"
        ),
        "results": results,
    }
    output.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(output)
    return 0


def _isolated(command: list[str]) -> tuple[float, dict[str, Any]]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    started = time.perf_counter()
    response = subprocess.run(command, capture_output=True, text=True, env=env)
    elapsed = time.perf_counter() - started
    if response.returncode:
        raise RuntimeError(response.stderr[-4000:] or response.stdout[-4000:])
    return elapsed, json.loads(response.stdout)


def _benchmark_provenance() -> dict[str, str]:
    from mcblueprint.config import DEFAULTS

    engine_root = ROOT / "src" / "mcblueprint"
    engine_hash = hashlib.sha256()
    for path in sorted(engine_root.rglob("*.py")):
        engine_hash.update(path.relative_to(engine_root).as_posix().encode())
        engine_hash.update(path.read_bytes())
    return {
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
            check=True,
        ).stdout.strip(),
        "engine_sha256": engine_hash.hexdigest(),
        "lock_sha256": hashlib.sha256(
            (engine_root / "data" / "lock.json").read_bytes()
        ).hexdigest(),
        "default_config_sha256": hashlib.sha256(
            json.dumps(DEFAULTS, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _run_core_profile_benchmark(output: Path) -> int:
    record_path = ROOT / "benchmarks" / "generation-profiles.json"
    definition = json.loads(record_path.read_text(encoding="utf-8"))
    results = []
    for profile in definition["profiles"]:
        fixture = FIXTURES / f"{profile['name']}.json"
        actual_hash = hashlib.sha256(fixture.read_bytes()).hexdigest()
        if actual_hash != profile["input_sha256"]:
            raise ValueError(f"Fixture hash mismatch: {profile['name']}")
        samples = []
        for attempt in range(3):
            seconds, worker = _isolated([
                sys.executable, str(Path(__file__).resolve()), "--core-worker", profile["name"],
            ])
            samples.append({"seconds": seconds, **worker})
            print(f"{profile['name']} core {attempt + 1}/3: {seconds:.3f}s", file=sys.stderr)
        results.append({
            **profile, "input_sha256": actual_hash, "repetitions": 3,
            "samples": samples,
            "seconds_median": statistics.median(sample["seconds"] for sample in samples),
            "process_peak_rss_bytes_median": statistics.median(
                sample["process_peak_rss_bytes"] for sample in samples
                if sample["process_peak_rss_bytes"] is not None
            ),
        })
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "schema_version": 1, "series": "generation-core-blueprint-v1",
            "definition_version": definition["definition_version"],
            "environment": _profile_environment(),
            "provenance": _benchmark_provenance(),
            "time_scope": (
                "独立子プロセス起動からBlueprint読込・検証・generate_regions・"
                "既定Weather・export_generated_regions完了まで。"
            ),
            "memory_metric": "独立子プロセス全体のPeakWorkingSetSize（Windows）またはOS最大RSS。",
            "comparison": (
                "同名fixtureの旧エンジン値はgeneration-baseline.jsonを参照。"
                "処理範囲が異なるため速度差の因果比較ではない。"
            ),
            "results": results,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


def _run_make_benchmark(output: Path) -> int:
    cases = [("house", "tiny"), ("tower", "tiny"),
             ("house", "medium"), ("tower", "medium"),
             ("house", "large"), ("tower", "large")]
    results = []
    for archetype, scale in cases:
        samples = []
        for attempt in range(3):
            with tempfile.TemporaryDirectory(prefix="mcblueprint-make-bench-parent-") as temporary:
                output_path = Path(temporary) / "result.schem"
                elapsed, worker = _isolated([
                    sys.executable, str(Path(__file__).resolve()), "--make-worker",
                    archetype, scale, "1.21.11", "schem", str(output_path),
                ])
                from mcblueprint.importers import read_schematic

                generated = read_schematic(output_path)
                worker["actual_dimensions"] = generated.volume.bounds().size.to_list()
                worker["block_states"] = sorted({state.id for _, state in generated.volume})
            worker["make_runtime_seconds"] = worker["seconds"]
            worker["seconds"] = elapsed
            samples.append(worker)
            print(f"{archetype}-{scale} {attempt + 1}/3: {worker['seconds']:.3f}s", file=sys.stderr)
        results.append({
            "archetype": archetype, "scale": scale, "minecraft_version": "1.21.11",
            "format": "schem", "requested_size": {
                "tiny": [32, 32, 32], "medium": [64, 48, 64], "large": [128, 96, 128],
            }[scale],
            "repetitions": 3, "samples": samples,
            "seconds_median": statistics.median(sample["seconds"] for sample in samples),
            "process_peak_rss_bytes_median": statistics.median(
                sample["process_peak_rss_bytes"] for sample in samples
            ),
        })
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({
            "schema_version": 1, "series": "make-buildspec-v1",
            "environment": _profile_environment(),
            "provenance": _benchmark_provenance(),
            "time_scope": "独立子プロセス起動からmakeの評価・schem export完了まで。",
            "memory_metric": "独立子プロセス全体のPeakWorkingSetSize（Windows）またはOS最大RSS。",
            "stage_sample_definition": (
                "stage_hook呼出時の累積経過秒と、その瞬間のプロセスWorking Set。"
                "段単独の時間・ピークRSSではない。"
            ),
            "results": results,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "schema_version": 1, "series": "make-buildspec-v1",
        "environment": _profile_environment(),
        "provenance": _benchmark_provenance(),
        "time_scope": "独立子プロセス起動からmakeの評価・schem export完了まで。",
        "memory_metric": "独立子プロセス全体のPeakWorkingSetSize（Windows）またはOS最大RSS。",
        "stage_sample_definition": (
            "stage_hook呼出時の累積経過秒と、その瞬間のプロセスWorking Set。"
            "段単独の時間・ピークRSSではない。"
        ),
        "results": results,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


def _world_cells(path: Path) -> tuple[dict[tuple[int, int, int], str], list[int]]:
    from mcblueprint.importers import read_schematic

    imported = read_schematic(path)
    cells = {
        tuple((pos + imported.offset).to_list()): state.id
        for pos, state in imported.volume if state.id != "minecraft:air"
    }
    bounds = imported.volume.bounds()
    return cells, bounds.size.to_list()


def _fixed_checks(case: dict[str, Any], path: Path, diagnostics: list[str]) -> list[dict[str, Any]]:
    cells, dimensions = _world_cells(path)
    xs, ys, zs = zip(*cells, strict=False)
    minimum = (min(xs), min(ys), min(zs))
    maximum = (max(xs), max(ys), max(zs))
    occupied = set(cells)
    route_lines = [line for line in diagnostics if "walkability=" in line]
    reachable = len(route_lines) > 0 and all(
        "walkability=reachable" in line for line in route_lines
    )
    door = any(state.endswith("_door") for state in cells.values())
    glass = any(state.endswith(("glass", "_glass_pane")) for state in cells.values())
    floor_blocks = [pos for pos, state in cells.items()
                    if state.endswith(("_planks", "_slab", "_stairs"))]
    perimeter_ratios = []
    for y in range(minimum[1] + 1, maximum[1]):
        perimeter = ([(x, y, minimum[2]) for x in range(minimum[0], maximum[0] + 1)]
                     + [(x, y, maximum[2]) for x in range(minimum[0], maximum[0] + 1)]
                     + [(minimum[0], y, z) for z in range(minimum[2] + 1, maximum[2])]
                     + [(maximum[0], y, z) for z in range(minimum[2] + 1, maximum[2])])
        perimeter_ratios.append(sum(pos in occupied for pos in perimeter) / len(perimeter))
    wall_coverage = max(perimeter_ratios, default=0)
    roof_over_room = any(
        (x, y, z) not in occupied and (x, y + 1, z) in occupied
        for x in range(minimum[0] + 1, maximum[0])
        for z in range(minimum[2] + 1, maximum[2])
        for y in range(minimum[1] + 1, maximum[1] - 1)
    )
    floor_levels = sorted({y for _, y, _ in floor_blocks})
    wall_coords = (
        [(minimum[0], z, 1, 0) for z in range(minimum[2] + 1, maximum[2])]
        + [(maximum[0], z, -1, 0) for z in range(minimum[2] + 1, maximum[2])]
        + [(x, minimum[2], 0, 1) for x in range(minimum[0] + 1, maximum[0])]
        + [(x, maximum[2], 0, -1) for x in range(minimum[0] + 1, maximum[0])]
    )
    wall_opening = any(
        (x, y, z) not in occupied and (x, y - 1, z) in occupied
        and (x, y + 1, z) in occupied
        and (x + dx, y, z + dz) not in occupied
        for x, z, dx, dz in wall_coords
        for y in range(minimum[1] + 2, maximum[1] - 1)
    )
    if case["id"] == "single-house":
        checks = [door and reachable, bool(floor_blocks) and wall_coverage >= 0.5,
                  roof_over_room]
    else:
        checks = [reachable and len(route_lines) >= 4,
                  len(floor_levels) >= 3,
                  glass or wall_opening]
    return [{
        "requirement": requirement,
        "result": "success" if passed else "failure",
        "evidence": {
            "reachable_routes": len(route_lines), "door_present": door,
            "floor_block_cells": len(floor_blocks), "floor_levels": floor_levels,
            "wall_coverage_ratio": wall_coverage, "roof_over_room": roof_over_room,
            "window_glass_present": glass, "wall_opening_candidate": wall_opening,
            "dimensions": dimensions,
        },
    } for requirement, passed in zip(case["requirements"], checks, strict=True)]


def _run_fixed_matrix(output: Path) -> int:
    import yaml

    from mcblueprint.cli import main
    from mcblueprint.pipeline import make

    cases = json.loads(
        (ROOT / "evaluation/cases/fixed-cases.json").read_text(encoding="utf-8")
    )["cases"][:2]
    results = []
    for case in cases:
        archetype = "house" if case["id"] == "single-house" else "tower"
        fixture = ROOT / "tests/data/buildspec" / (
            "single-house.yaml" if archetype == "house" else "standalone-tower.yaml"
        )
        for version in ("1.21.11", "26.1.2", "26.2", "26.3"):
            for output_format in ("schem", "litematic"):
                with tempfile.TemporaryDirectory(prefix="mcblueprint-fixed-matrix-") as temporary:
                    folder = Path(temporary)
                    spec = yaml.safe_load(fixture.read_text(encoding="utf-8"))
                    spec["minecraftVersion"] = version
                    spec["config"] = {"output": {"format": output_format}}
                    source = folder / "spec.yaml"
                    source.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8")
                    generated = folder / f"make.{output_format}"
                    made = make(source, output=generated, emit=["blueprint"])
                    emitted = generated.with_name("make.blueprint.json")
                    rebuilt = folder / f"legacy.{output_format}"
                    legacy_code = main([
                        "build", str(emitted), "--format", output_format, "-o", str(rebuilt),
                    ])
                    make_cells, _ = _world_cells(generated)
                    legacy_cells, _ = _world_cells(rebuilt)
                    results.append({
                        "case": case["id"], "minecraft_version": version,
                        "buildspec_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "blueprint_sha256": hashlib.sha256(emitted.read_bytes()).hexdigest(),
                        "format": output_format,
                        "buildspec_status": "success" if made.code == 0 else "failure",
                        "blueprint_build_status": "success" if legacy_code == 0 else "failure",
                        "placement_identical": make_cells == legacy_cells,
                        "buildspec_requirements": _fixed_checks(
                            case, generated, list(made.diagnostics)
                        ),
                        "blueprint_requirements": _fixed_checks(
                            case, rebuilt, list(made.diagnostics)
                        ),
                    })
                    print(f"fixed {case['id']} {version} {output_format}", file=sys.stderr)
    regression = _run_core_regressions()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({
        "schema_version": 1, "record_type": "generation-core-evaluation",
        "source_case_sha256": hashlib.sha256(
            (ROOT / "evaluation/cases/fixed-cases.json").read_bytes()
        ).hexdigest(),
        "provenance": _benchmark_provenance(),
        "regression_checks": regression,
        "results": results,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


def _run_core_regressions() -> dict[str, Any]:
    from mcblueprint.importers import read_schematic
    from mcblueprint.pipeline import STAGES, make

    fixture = ROOT / "tests/data/buildspec/single-house.yaml"
    with tempfile.TemporaryDirectory(prefix="mcblueprint-core-regression-") as temporary:
        folder = Path(temporary)
        source = folder / "house.yaml"
        source.write_bytes(fixture.read_bytes())
        base = make(source, output=folder / "base.schem", emit=["blueprint"])
        repeat = make(source, output=folder / "repeat.schem")
        changed = make(source, output=folder / "changed.schem", overrides=["seed=73"])
        def cells(path):
            imported = read_schematic(path)
            return {
                tuple((pos + imported.offset).to_list()): state.to_string()
                for pos, state in imported.volume
            }
        reproducible = cells(base.output) == cells(repeat.output)
        seed_differs = cells(base.output) != cells(changed.output)
        core_outputs = []
        for core in (16, 32, 64):
            result = make(
                source, output=folder / f"core-{core}.schem",
                overrides=[f"limits.region=[{core},{core},{core}]", "limits.halo=0"],
            )
            core_outputs.append(cells(result.output))
        core_invariant = core_outputs[0] == core_outputs[1] == core_outputs[2]
        resume_results = {}
        for stage in STAGES:
            output_path = folder / f"{stage}.schem"
            first = make(source, output=output_path, emit=[stage])
            checkpoint = output_path.with_name(f"{output_path.stem}.{stage}.json")
            resumed = make(source, output=folder / f"{stage}-resumed.schem", resume=checkpoint)
            resume_results[stage] = cells(first.output) == cells(resumed.output)
    legacy = []
    from mcblueprint.generator import generate
    from mcblueprint.loader import load_blueprint_dict, read_blueprint_json
    from mcblueprint.validator import validate

    for path in sorted((ROOT / "examples").glob("*.json")):
        raw = read_blueprint_json(path)
        errors = validate(raw)
        blueprint = load_blueprint_dict(raw)
        volume = generate(blueprint) if not errors else None
        legacy.append({
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "validation": "success" if not errors else "failure",
            "generated_cells": len(volume) if volume else 0,
        })
    legacy_fixtures = []
    for path in sorted(FIXTURES.glob("*.json")):
        if path.stem.startswith("profile-"):
            continue
        raw = read_blueprint_json(path)
        errors = validate(raw)
        blueprint = load_blueprint_dict(raw)
        volume = generate(blueprint) if not errors else None
        legacy_fixtures.append({
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "validation": "success" if not errors else "failure",
            "generated_cells": len(volume) if volume else 0,
        })
    return {
        "seed_reproducible": reproducible, "seed_variation_changes_placement": seed_differs,
        "core_sizes_16_32_64_identical": core_invariant,
        "resume_each_stage_identical": resume_results,
        "legacy_examples": legacy,
        "legacy_benchmark_fixtures": legacy_fixtures,
    }


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RECORD)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--generate-fixtures", action="store_true")
    parser.add_argument("--worker", nargs=3, metavar=("CASE", "STAGE", "REPETITIONS"))
    parser.add_argument("--full-worker", metavar="CASE")
    parser.add_argument("--core-worker", metavar="CASE")
    parser.add_argument(
        "--make-worker", nargs=5,
        metavar=("ARCHETYPE", "SCALE", "VERSION", "FORMAT", "OUTPUT"),
    )
    parser.add_argument("--profile-benchmark", action="store_true")
    parser.add_argument("--core-profile-benchmark", action="store_true")
    parser.add_argument("--make-benchmark", action="store_true")
    parser.add_argument("--fixed-matrix", action="store_true")
    parser.add_argument(
        "--profile-output", type=Path, default=ROOT / "benchmarks" / "generation-baseline.json"
    )
    parser.add_argument("--generate-profiles", action="store_true")
    args = parser.parse_args()
    if args.generate_fixtures:
        _generate_fixtures()
        return 0
    if args.generate_profiles:
        from generate_benchmark_profiles import generate_profiles

        generate_profiles()
        return 0
    if args.worker:
        print(json.dumps(_worker(args.worker[0], args.worker[1], int(args.worker[2]))))
        return 0
    if args.full_worker:
        print(json.dumps(_full_worker(args.full_worker)))
        return 0
    if args.core_worker:
        print(json.dumps(_core_worker(args.core_worker)))
        return 0
    if args.make_worker:
        print(json.dumps(_make_worker(*args.make_worker)))
        return 0
    if args.profile_benchmark:
        return _run_profile_benchmark(args.profile_output)
    if args.core_profile_benchmark:
        return _run_core_profile_benchmark(
            ROOT / "benchmarks" / "generation-core-baseline.json"
        )
    if args.make_benchmark:
        return _run_make_benchmark(ROOT / "benchmarks" / "make-baseline.json")
    if args.fixed_matrix:
        return _run_fixed_matrix(ROOT / "evaluation/records" / "generation-core.json")
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
