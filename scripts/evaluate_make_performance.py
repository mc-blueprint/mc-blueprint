"""#210の修正前後を独立プロセスで測定し、出力の一致を記録する。"""

from __future__ import annotations

import argparse
import cProfile
import gzip
import hashlib
import json
import pstats
import statistics
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import benchmark
import evaluate_structure as structure
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "benchmarks/.tmp/issue-210"
RECORD = ROOT / "evaluation/records/make-performance-210.json"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def signature(output):
    run = json.loads(output.with_suffix(output.suffix + ".run.json").read_bytes())
    scene = output.with_name(output.stem + ".scene.json.gz")
    return {
        "placement_sha256": structure.placement_hash(structure.cells(output)).hexdigest(),
        "metrics_sha256": digest(run["metrics"]),
        "structured_diagnostics_sha256": digest(run["structuredDiagnostics"]),
        "scene_json_sha256": hashlib.sha256(gzip.decompress(scene.read_bytes())).hexdigest(),
    }


def worker(source, output, profile):
    samples = []
    started = time.perf_counter()

    def hook(stage):
        samples.append({"stage": stage, "elapsed_seconds": time.perf_counter() - started,
                        "rss_at_stage_start_bytes": benchmark._process_rss()})

    profiler = cProfile.Profile() if profile else None
    if profiler:
        profiler.enable()
    result = structure.pipeline.make(source, output=output, emit=[], stage_hook=hook)
    if profiler:
        profiler.disable()
        profiler.dump_stats(profile)
    return {"code": result.code, "make_runtime_seconds": time.perf_counter() - started,
            "process_peak_rss_bytes": benchmark._process_peak_rss(),
            "stage_hook_samples": samples}


def profile_summary(path):
    stats = pstats.Stats(str(path))
    rows = []
    for (filename, line, name), (primitive, calls, own, cumulative, _) in stats.stats.items():
        rows.append({"file": filename.replace(str(ROOT), "."), "line": line,
                     "function": name, "primitive_calls": primitive, "calls": calls,
                     "own_seconds": own, "cumulative_seconds": cumulative})
    return {"total_seconds": stats.total_tt, "total_calls": stats.total_calls,
            "focus_functions": [row for row in rows if row["function"] in (
                "_spec_sources", "plan_buildspec", "_plan_contract", "plan_detail",
                "_facade_selection", "materialize", "resolve", "consume", "capture_sources",
                "write_scene", "_spool_regions", "_distance")],
            "top_cumulative": sorted(rows, key=lambda r: r["cumulative_seconds"],
                                     reverse=True)[:35],
            "top_own": sorted(rows, key=lambda r: r["own_seconds"], reverse=True)[:20]}


def run(phase):
    WORK.mkdir(parents=True, exist_ok=True)
    record = json.loads(RECORD.read_bytes()) if RECORD.exists() else {
        "schema_version": 1, "record_type": "make-performance-210",
        "base_commit": "3261ea9", "environment": benchmark._profile_environment(),
        "comparison_limits": [
            "Cはscene追加前、Dは#209前。本記録のbeforeは#209適用後。",
            "時間は子プロセス起動からworker JSON出力まで。hash検査は計時外。",
            "段開始の累積時間とRSS。隣接差分は段の時間で、段単独ピークではない。",
            "各実行は直列。別担当の読み取りreviewのCPU負荷は制御していない。",
            "NFR2達成判定はHの範囲であり、本記録では行わない。",
        ],
    }
    record[f"{phase}_provenance"] = benchmark._benchmark_provenance()
    record["dependencies"] = {name: version(name) for name in
                              ("numpy", "nbtlib", "jsonschema", "PyYAML")}
    record.setdefault(phase, [])
    fixed = phase.startswith("fixed")
    profiled = phase.startswith("profile")
    if fixed:
        plan = json.loads(structure.PLAN.read_bytes())
        for entry in plan["inputs"]:
            path = ROOT / entry["input"]["path"]
            if structure.digest(path) != entry["input"]["sha256"]:
                raise ValueError(f"固定入力が変更されています: {path}")
        cases = [(Path(e["input"]["path"]).stem, ROOT / e["input"]["path"])
                 for e in plan["inputs"]]
    else:
        cases = []
        for scale in ("medium", "large"):
            for archetype in (("tower",) if profiled else ("tower", "house")):
                name = f"{archetype}-{scale}"
                source = WORK / name / "spec.yaml"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(yaml.safe_dump(structure.performance_spec(archetype, scale),
                                                allow_unicode=True),
                                  encoding="utf-8", newline="\n")
                cases.append((name, source))
    for name, source in cases:
        if any(entry["case"] == name for entry in record[phase]):
            continue
        folder = WORK / name / phase
        folder.mkdir(parents=True, exist_ok=True)
        fmt = yaml.safe_load(source.read_bytes()).get("config", {}).get(
            "output", {}).get("format", "schem")
        output = folder / f"result.{fmt}"
        samples = []
        for index in range(3 if phase == "after" else 1):
            command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                       str(source), str(output)]
            profile = folder / "make.prof"
            if profiled:
                command.extend(["--profile", str(profile)])
            elapsed, sample = benchmark._isolated(command)
            sample["seconds"] = elapsed
            sample["signature"] = signature(output)
            samples.append(sample)
            print(f"{phase} {name} {index + 1}: {elapsed:.3f}s", flush=True)
        entry = {"case": name, "input_sha256": structure.digest(source), "samples": samples,
                 "seconds_median": statistics.median(s["seconds"] for s in samples),
                 "signature": samples[-1]["signature"]}
        if not all(s["signature"] == entry["signature"] for s in samples):
            raise ValueError(f"反復間で出力が異なります: {name}")
        if profiled:
            entry["profile"] = profile_summary(profile)
        if phase in ("after", "fixed-after", "profile-after"):
            base_phase = {"after": "before", "fixed-after": "fixed-before",
                          "profile-after": "profile"}[phase]
            before = next(e for e in record[base_phase] if e["case"] == name)
            entry["matches_before"] = entry["signature"] == before["signature"]
            if not entry["matches_before"]:
                raise ValueError(f"修正前と出力が異なります: {name}")
        if phase in ("before", "after"):
            archetype, scale = name.split("-")
            for key, path in (("c", ROOT / "benchmarks/make-baseline.json"),
                              ("d", structure.PERFORMANCE)):
                old = next(r for r in json.loads(path.read_bytes())["results"]
                           if r["archetype"] == archetype and r["scale"] == scale)
                entry[f"{key}_seconds_median"] = old["seconds_median"]
                entry[f"input_identical_to_{key}"] = (
                    all(s["input_sha256"] == entry["input_sha256"] for s in old["samples"])
                    if key == "c" else old["input_sha256"] == entry["input_sha256"])
                if key == "c":
                    crlf_hash = hashlib.sha256(source.read_bytes().replace(b"\n", b"\r\n"))
                    entry["input_matches_c_with_crlf"] = all(
                        s["input_sha256"] == crlf_hash.hexdigest() for s in old["samples"])
        record[phase].append(entry)
        record["updated_at"] = datetime.now(UTC).isoformat()
        structure.write_json(RECORD, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "profile", "after", "profile-after",
                                            "fixed-before", "fixed-after"))
    parser.add_argument("--worker", nargs=2, type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(*args.worker, args.profile)))
    elif args.phase:
        run(args.phase)
