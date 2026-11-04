"""#234の完成spool再利用を#232と同じ独立プロセス・入力で比較する。"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch

import benchmark
import evaluate_make_performance as performance
import prepare_make_reference
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "output/issue-234"
REFERENCE = WORK / "reference"
RECORD = ROOT / "evaluation/records/spool-reuse-234.json"


def write_record(record):
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8", newline="\n")


def make_worker(source, output):
    """block spoolの論理I/Oだけを数え、OSキャッシュ・物理I/Oと区別する。"""
    from mcblueprint.exporters._spool import RegionSpool

    counters = {"spool_writes": 0, "read_bytes": 0, "write_bytes": 0}
    original_open, original_write = Path.open, RegionSpool.write

    class Stream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, *args):
            data = self.stream.read(*args)
            counters["read_bytes"] += len(data)
            return data

        def write(self, data):
            count = self.stream.write(data)
            counters["write_bytes"] += count
            return count

    def open_spool(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return Stream(stream) if path.name == "cells" else stream

    def write_spool(spool, regions):
        counters["spool_writes"] += 1
        return original_write(spool, regions)

    with patch.object(Path, "open", open_spool), patch.object(RegionSpool, "write", write_spool):
        sample = performance.worker(source, output, None)
    sample["block_spool_io"] = counters
    return sample


def core_worker():
    from mcblueprint.exporters._spool import RegionSpool
    from mcblueprint.generator import GeneratedRegion
    from mcblueprint.model import AABB, Vec3
    from mcblueprint.volume import RegionVolume

    rows = []
    for count in (128, 512, 2048):
        bounds = AABB(Vec3(-count // 2, 0, 0), Vec3(count // 2 - 1, 0, 0))

        def regions(bounds=bounds):
            for x in range(bounds.min.x, bounds.max.x + 1):
                core = AABB(Vec3(x, 0, 0), Vec3(x, 0, 0))
                yield GeneratedRegion(core, RegionVolume(core))

        started = time.perf_counter()
        RegionSpool(WORK / "core-cells", bounds).write(regions())
        rows.append({"core_count": count, "seconds": time.perf_counter() - started,
                     "write_bytes": count * 4})
    (WORK / "core-cells").unlink()
    return {"results": rows}


def isolated(phase, arguments):
    worker_args = [str(Path(__file__).resolve()), *arguments]
    paths = [str(ROOT / "scripts")]
    if phase == "before":
        paths.insert(0, str(REFERENCE / "src"))
    bootstrap = (f"import sys,runpy; sys.path[:0]={paths!r}; sys.argv={worker_args!r}; "
                 f"runpy.run_path({worker_args[0]!r},run_name='__main__')")
    return benchmark._isolated([sys.executable, "-X", "utf8", "-c", bootstrap])


def run(repetitions):
    prepare_make_reference.COMMIT = "85306dd20b0d3e96da59dafee55630486d60afaf"
    prepare_make_reference.FOLDER = REFERENCE
    prepare_make_reference.prepare()
    reference = json.loads(REFERENCE.with_suffix(".json").read_bytes())
    if reference["commit"] != "85306dd20b0d3e96da59dafee55630486d60afaf":
        raise ValueError("比較基準commitが違います")
    baseline = json.loads((ROOT / "evaluation/records/h-baseline.json").read_bytes())
    record = json.loads(RECORD.read_bytes()) if RECORD.exists() else {
        "schema_version": 1, "record_type": "spool-reuse-234",
        "reference": reference, "environment": benchmark._profile_environment(),
        "h_baseline": "evaluation/records/h-baseline.json",
        "comparison_limits": [
            "#232の入力と独立子プロセス・3反復の手順を使用する。hash検査は計時外。",
            "他タスクと全件pytestが並行稼働しCPUは隔離していない。NFR2/3の判定には使わない。",
            "I/Oはblock spoolに対するread/writeの論理byte数。物理ディスクI/Oではない。",
            "生成・評価・sceneを含む全体時間。段開始の累積時間も保存する。",
            "#230の配色identityを含む起点85306ddとの比較であり、#232当時のhashは要求しない。",
        ],
        "results": [], "core_scaling": {},
    }
    record["script_sha256"] = performance.structure.digest(Path(__file__).resolve())
    record["dependencies"] = {name: version(name) for name in
                              ("numpy", "nbtlib", "jsonschema", "PyYAML")}
    record["cpu_isolation_observation"] = (
        "他タスクが並行稼働し、前半の測定は全件pytestとも並行した。"
        "CPUを隔離していないため時間差だけで本変更の効果やNFR2/3の達成を判定しない。"
    )
    record["line_ending_normalization"] = (
        "初期のmedium試行後に変更ファイルをCRLFからLFへ統一。Python ASTは不変。"
        "各試行のenvironment.engineHashは測定時のbyteを識別する。"
    )
    for entry in baseline["results"]:
        name = entry["case"]
        folder = WORK / name
        folder.mkdir(parents=True, exist_ok=True)
        source, output = folder / "spec.yaml", folder / "result.schem"
        source.write_text(yaml.safe_dump(entry["input"], allow_unicode=True),
                          encoding="utf-8", newline="\n")
        row = next((r for r in record["results"] if r["case"] == name), None)
        if row is None:
            row = {"case": name, "input_sha256": performance.structure.digest(source),
                   "h_baseline_seconds_median": entry["seconds_median"]}
            record["results"].append(row)
        row["input_matches_h_baseline"] = row["input_sha256"] == entry["input_sha256"]
        if not row["input_matches_h_baseline"]:
            raise ValueError(f"#232と入力byteが一致しません: {name}")
        for phase in ("before", "after"):
            samples = row.setdefault(phase, [])
            while len(samples) < repetitions:
                elapsed, sample = isolated(phase, ["--worker", str(source), str(output)])
                sample["seconds"] = elapsed
                sample["signature"] = performance.signature(output)
                samples.append(sample)
                write_record(record)
                print(f"{name} {phase} {len(samples)}/{repetitions}: {elapsed:.3f}s", flush=True)
            row[f"{phase}_seconds_median"] = statistics.median(s["seconds"] for s in samples)
        expected = row["before"][0]["signature"]
        row["hashes_match"] = all(s["signature"] == expected
                                  for phase in ("before", "after") for s in row[phase])
        if not row["hashes_match"]:
            raise ValueError(f"配置・指標・診断・sceneが一致しません: {name}")
        write_record(record)
    for phase in ("before", "after"):
        if phase not in record["core_scaling"]:
            record["core_scaling"][phase] = [isolated(phase, ["--core-worker"])[1]
                                             for _ in range(repetitions)]
            write_record(record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", nargs=2, type=Path)
    parser.add_argument("--core-worker", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(make_worker(*args.worker)))
    elif args.core_worker:
        print(json.dumps(core_worker()))
    else:
        run(args.repetitions)
