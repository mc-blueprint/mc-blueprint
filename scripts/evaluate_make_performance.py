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
WORK = ROOT / "benchmarks/.tmp/issue-210-fc8fbf9"
RECORD = ROOT / "evaluation/records/make-performance-210-develop-fc8fbf9.json"
BASE_COMMIT = "fc8fbf9fea3bf05506ec42b841bf678fb0dd001e"


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
    elapsed = time.perf_counter() - started
    peak = benchmark._process_peak_rss()
    run_record = json.loads(result.record.read_bytes())
    return {"code": result.code, "make_runtime_seconds": elapsed,
            "process_peak_rss_bytes": peak, "stage_hook_samples": samples,
            "implementation": {"pipeline": str(Path(structure.pipeline.__file__).resolve()),
                               "environment": run_record["environment"]}}


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


def run(phase, baseline_root):
    baseline = phase in ("before", "fixed-before", "profile")
    if baseline and baseline_root is None:
        raise ValueError("develop単体の比較には --baseline-root が必要です")
    WORK.mkdir(parents=True, exist_ok=True)
    record = json.loads(RECORD.read_bytes()) if RECORD.exists() else {
        "schema_version": 1, "record_type": "make-performance-210",
        "base_commit": BASE_COMMIT, "environment": benchmark._profile_environment(),
        "comparison_limits": [
            "beforeは#214/#215を含むfc8fbf9そのもの、afterはfc8fbf9へ#210を加えた実装。",
            "Cはscene追加前、Dは#209前で、今回の修正前後比較とは異なる履歴。",
            "時間は子プロセス起動からworker JSON出力まで。hash検査は計時外。",
            "段開始の累積時間とRSS。隣接差分は段の時間で、段単独ピークではない。",
            "各実行は直列。別担当の読み取りreviewのCPU負荷は制御していない。",
            "NFR2達成判定はHの範囲であり、本記録では行わない。",
        ],
    }
    if baseline:
        baseline_root = baseline_root.resolve()
        reference = json.loads(baseline_root.with_suffix(".json").read_bytes())
        if reference["commit"] != BASE_COMMIT:
            raise ValueError("指定したdevelopのcommitと参照コードが一致しません")
        record["baseline_reference"] = reference
    else:
        record[f"{phase}_provenance"] = benchmark._benchmark_provenance()
    record["script"] = structure.reference(Path(__file__).resolve())
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
            worker_args = [str(Path(__file__).resolve()), "--worker", str(source), str(output)]
            profile = folder / "make.prof"
            if profiled:
                worker_args.extend(["--profile", str(profile)])
            command = [sys.executable, "-X", "utf8", *worker_args]
            if baseline:
                bootstrap = (
                    "import sys, runpy; "
                    f"sys.path[:0] = {[str(baseline_root / 'src'), str(ROOT / 'scripts')]!r}; "
                    f"sys.argv = {worker_args!r}; "
                    f"runpy.run_path({worker_args[0]!r}, run_name='__main__')"
                )
                command = [sys.executable, "-X", "utf8", "-c", bootstrap]
            elapsed, sample = benchmark._isolated(command)
            expected_root = baseline_root if baseline else ROOT
            if not Path(sample["implementation"]["pipeline"]).is_relative_to(expected_root / "src"):
                raise ValueError("比較対象と異なる実装を読み込みました")
            record[f"{phase}_implementation"] = sample["implementation"]
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


def run_h_baseline():
    """Record three current house/tower medium/large runs for H01."""
    from mcblueprint.importers import read_schematic

    record_path = ROOT / "evaluation/records/h-baseline.json"
    work = ROOT / "benchmarks/.tmp/issue-232"
    work.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "record_type": "h-baseline",
        "provenance": benchmark._benchmark_provenance(),
        "script": structure.reference(Path(__file__).resolve()),
        "environment": benchmark._profile_environment(),
        "storage": "H: NTFS; physical device model unavailable",
        "comparison_limits": [
            ("基準端末はCPU 8コア/16論理CPU、物理メモリ64GiB、GeForce RTX 4070 SUPERで、"
             "NFR1相当端末ではない。"),
            "独立子プロセス起動からmakeのscene/run確定までを3回計時し、全試行と中央値を記録する。",
            ("stage_hook値は累積経過時間と段開始時Working Setであり、"
             "段単独の時間・ピークRSSではない。"),
            "OSピークRSSは独立子プロセス全体のPeakWorkingSetSize。NFR2/3の達成判定には使わない。",
            "固定H地形ケースは未実装の期待条件であり、この住宅/塔測定で生成成功とみなさない。",
        ],
        "results": [],
    }
    for scale in ("medium", "large"):
        for archetype in ("house", "tower"):
            name = f"{archetype}-{scale}"
            folder = work / name
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / "spec.yaml"
            spec = structure.performance_spec(archetype, scale)
            spec["seed"] = 42
            source.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8",
                              newline="\n")
            output = folder / "result.schem"
            samples = []
            for index in range(3):
                command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                           "--worker", str(source), str(output)]
                elapsed, sample = benchmark._isolated(command)
                sample["seconds"] = elapsed
                sample["signature"] = signature(output)
                samples.append(sample)
                print(f"H01 {name} {index + 1}/3: {elapsed:.3f}s", flush=True)
            generated = read_schematic(output)
            run_record = json.loads(output.with_suffix(output.suffix + ".run.json").read_bytes())
            expected = samples[0]["signature"]
            result = {
                "case": name,
                "input_sha256": structure.digest(source),
                "input": spec,
                "actual_dimensions": generated.volume.bounds().size.to_list(),
                "occupied_cells": len(generated.volume),
                "core_size": run_record["config"]["limits"]["region"],
                "halo": run_record["config"]["limits"]["halo"],
                "generation": {
                    "engine": run_record["environment"]["engine"],
                    "data_hash": run_record["environment"]["dataHash"],
                    "engine_hash": run_record["environment"]["engineHash"],
                    "lock_hash": run_record["environment"]["lockHash"],
                },
                "repetitions": len(samples),
                "samples": samples,
                "seconds_median": statistics.median(item["seconds"] for item in samples),
                "process_peak_rss_bytes_median": statistics.median(
                    item["process_peak_rss_bytes"] for item in samples
                ),
                "reproducible": all(item["signature"] == expected for item in samples),
                "status": "success" if all(item["code"] == 0 for item in samples) else "failure",
            }
            if not result["reproducible"]:
                raise ValueError(f"反復間で出力が異なります: {name}")
            record["results"].append(result)
            record["updated_at"] = datetime.now(UTC).isoformat()
            structure.write_json(record_path, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "profile", "after", "profile-after",
                                            "fixed-before", "fixed-after"))
    parser.add_argument("--worker", nargs=2, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--h-baseline", action="store_true")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(*args.worker, args.profile)))
    elif args.h_baseline:
        run_h_baseline()
    elif args.phase:
        run(args.phase, args.baseline_root)
