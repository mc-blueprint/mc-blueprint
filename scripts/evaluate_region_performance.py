"""#243の領域処理をH01の測定器・固定課題で比較する。"""

from __future__ import annotations

import argparse
import importlib.abc
import importlib.util
import json
import pstats
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "benchmarks/.tmp/issue-243"
RECORD = ROOT / "evaluation/records/region-performance-243.json"
BASE = "5f0518a"
MODULES = ("volume", "generator", "weather", "critic", "traversal", "pipeline",
           "operations.randomness", "region_export", "exporters.schem", "exporters.litematic")


class BaselineLoader(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """対象モジュールだけを確定commitから読み、他のコード・データは共用する。"""

    def __init__(self, phase="before"):
        self.phase = phase

    def find_spec(self, fullname, path, target=None):
        if fullname in {f"mcblueprint.{name}" for name in MODULES}:
            return importlib.util.spec_from_loader(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__name__.removeprefix("mcblueprint.").replace(".", "/")
        path = ROOT / "src/mcblueprint" / f"{name}.py"
        source = subprocess.check_output(["git", "show", f"{BASE}:src/mcblueprint/{name}.py"],
                                         cwd=ROOT)
        if self.phase == "after":
            source = path.read_bytes()
        module.__file__ = str(path)
        exec(compile(source, str(path), "exec"), module.__dict__)


def cases(suite):
    import evaluate_structure as structure
    import yaml

    large = json.loads((ROOT / "evaluation/cases/large-scale.json").read_bytes())
    entries = []
    if suite == "matrix":
        fixed = json.loads(structure.PLAN.read_bytes())["inputs"]
        entries.extend((Path(e["input"]["path"]).stem,
                        yaml.safe_load((ROOT / e["input"]["path"]).read_bytes())) for e in fixed)
    else:
        for kind in ("house", "tower"):
            for scale in ("medium", "large"):
                entries.append((f"{kind}-{scale}", structure.performance_spec(kind, scale)))
    for kind in ("junction", "castle", "town", "cave", "underground_city"):
        raw = {} if kind == "junction" else next(
            c["buildSpec"] for c in large["cases"] if c["buildSpec"]["archetype"] == kind)
        variants = ((v, f, "huge" if kind == "underground_city" else "medium")
                    for v in ("1.21.11", "26.1.2", "26.2", "26.3")
                    for f in ("schem", "litematic")) if suite == "matrix" else (
                        ("1.21.11", "schem", scale)
                        for scale in (("medium",) if kind == "junction" else ("medium", "huge")))
        for version, fmt, scale in variants:
            spec = {**raw, "minecraftVersion": version, "scale": scale, "seed": 42,
                    "config": {"preview": {"enabled": False}, "output": {"format": fmt}}}
            if kind != "junction":
                spec["size"] = large["scales"][scale]
            entries.append((f"{kind}-{scale}-{version}-{fmt}", spec))
    for name, spec in entries:
        source = WORK / suite / name / "spec.yaml"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(yaml.safe_dump(spec, allow_unicode=True), encoding="utf-8", newline="\n")
        yield name, source, spec.get("config", {}).get("output", {}).get("format", "schem")


def worker(source, output, phase, profile):
    # 両側で同じgit読取・ロード経路を使い、起動時間の比較条件をそろえる。
    sys.meta_path.insert(0, BaselineLoader(phase))
    import evaluate_make_performance as performance

    if source.parent.name.startswith("junction-"):
        sys.path.insert(0, str(ROOT / "tests"))
        from unittest.mock import patch

        from test_junction_planning import joined_plan

        def joined(resolved):
            result = joined_plan(resolved)
            result.blueprint["minecraftVersion"] = resolved.buildspec["minecraftVersion"]
            return result

        with patch.object(performance.structure.pipeline, "plan_buildspec", joined):
            result = performance.worker(source, output, profile)
    else:
        try:
            result = performance.worker(source, output, profile)
        except performance.structure.pipeline.PipelineError as exc:
            if not (source.parent.name.startswith("underground_city-medium-")
                    and exc.stage == "Resolve" and "主空洞に収まりません" in str(exc)):
                raise
            import benchmark

            result = {"code": exc.code, "failure": {"stage": exc.stage, "message": str(exc)},
                      "process_peak_rss_bytes": benchmark._process_peak_rss(),
                      "stage_hook_samples": []}
    return result


def run(args):
    import benchmark
    import evaluate_make_performance as performance
    import evaluate_structure as structure

    record = json.loads(RECORD.read_bytes()) if RECORD.exists() else {
        "base_commit": BASE, "environment": benchmark._profile_environment(),
        "method": "H01 worker/独立プロセス/3反復。matrixは各1反復、cProfileは別実行。"
                  "入力と出力先は前後同一。stage_hookの隣接差分は段時間、RSSは段開始時。"
                  "beforeは対象モジュールを起点commitからロードする。基準端末のNFR判定ではない。",
        "results": {},
    }
    implementation = benchmark._benchmark_provenance()
    script = structure.reference(Path(__file__))
    h01_script = structure.reference(Path(performance.__file__))
    provenance = {"implementation": implementation, "base_commit": BASE, "python": sys.version,
                  "script_sha256": script["sha256"], "h01_script_sha256": h01_script["sha256"],
                  "common_scripts_sha256": {
                      module.__name__: structure.reference(Path(module.__file__))["sha256"]
                      for module in (benchmark, structure)}}
    key = args.suite + ("-profile" if args.profile else "")
    entries = record["results"].setdefault(key, {}).setdefault(args.phase, {})
    if any(entry.get("provenance") != provenance for entry in entries.values()):
        raise ValueError("既存結果の実装provenanceが一致しません。別の記録へ測定してください")
    selected = [(name, source, fmt) for name, source, fmt in cases(args.suite)
                if not args.case or any(token in name for token in args.case)]
    for name, source, _ in selected:
        if name in entries and entries[name]["input_sha256"] != structure.digest(source):
            raise ValueError(f"既存結果の入力hashが一致しません: {name}")
    record[args.phase + "_provenance"] = implementation
    record["script"] = script
    record["h01_script"] = h01_script
    for name, source, fmt in selected:
        if name in entries:
            continue
        output = source.parent / f"result.{fmt}"
        samples = []
        for index in range(1 if args.suite == "matrix" or args.profile else 3):
            command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                       "--worker", str(source), str(output), "--phase", args.phase]
            profile = source.parent / f"{args.phase}.prof"
            if args.profile:
                command.append("--profile")
            elapsed, sample = benchmark._isolated(command)
            sample["seconds"] = elapsed
            sample["signature"] = (None if "failure" in sample else performance.signature(output))
            samples.append(sample)
            print(f"{key} {args.phase} {name} {index + 1}: {elapsed:.3f}s", flush=True)
        entry = {"input_sha256": structure.digest(source), "provenance": provenance,
                 "samples": samples,
                 "seconds_median": statistics.median(s["seconds"] for s in samples),
                 "rss_median": statistics.median(s["process_peak_rss_bytes"] for s in samples)}
        expected = samples[0]["signature"]
        failure = samples[0].get("failure")
        if failure:
            entry["expected_failure"] = failure
        if not all(s["signature"] == expected and s.get("failure") == failure for s in samples):
            raise ValueError(f"反復間で出力が異なります: {name}")
        if args.phase == "after":
            before = record["results"][key]["before"][name]
            entry["matches_before"] = (before["samples"][0]["signature"] == expected
                                       and before["samples"][0].get("failure") == failure)
            if not entry["matches_before"]:
                raise ValueError(f"修正前と出力が異なります: {name}")
        if args.profile:
            entry["profile"] = performance.profile_summary(profile)
            entry["profile"]["region_functions"] = [
                {"file": Path(file).name, "function": function, "calls": values[1],
                 "own_seconds": values[2], "cumulative_seconds": values[3]}
                for (file, _, function), values in pstats.Stats(str(profile)).stats.items()
                if Path(file).name in {"weather.py", "critic.py", "traversal.py", "randomness.py",
                                       "pipeline.py", "support.py"}
                and function in {"apply_weather", "_water_distances", "_weather_fields",
                                 "__init__", "consume", "finalize", "route", "stand", "_stand",
                                 "tiles", "_sample", "first_draws", "_support", "check_support",
                                 "__iter__", "_row", "get", "_working_budget"}
            ]
        entries[name] = entry
        structure.write_json(RECORD, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--suite", choices=("timing", "matrix"), default="timing")
    parser.add_argument("--case", action="append")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--worker", nargs=2, type=Path)
    args = parser.parse_args()
    if args.worker:
        source, output = args.worker
        profile = source.parent / f"{args.phase}.prof" if args.profile else None
        print(json.dumps(worker(source, output, args.phase, profile)))
    else:
        run(args)
