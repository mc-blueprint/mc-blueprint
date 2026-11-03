"""Dの固定課題を事前固定し、品質不合格も含めてローカル証跡を保存する。"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import benchmark
import yaml

from mcblueprint import pipeline
from mcblueprint.cli import main
from mcblueprint.critic import CriticAccumulator, evaluate, plan_from_metadata
from mcblueprint.importers import read_schematic
from mcblueprint.model import BlockState
from mcblueprint.validator import validate_and_load

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "evaluation/cases/structure-detail.json"
RECORD = ROOT / "evaluation/records/structure-detail.json"
PERFORMANCE = ROOT / "evaluation/records/structure-detail-performance.json"
METHOD = "structure-detail-v1"


def write_json(path, value, extra_inputs=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = [ROOT / item["input"]["path"]
              for item in json.loads(PLAN.read_bytes())["inputs"]] if PLAN.exists() else []
    inputs.extend(extra_inputs)
    content = json.dumps(repository_relative(value, inputs), ensure_ascii=False, indent=2) + "\n"
    path.write_text(content, encoding="utf-8", newline="\n")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path):
    return {"path": path.relative_to(ROOT).as_posix(), "sha256": digest(path)}


def repository_relative(value, inputs=(), key=None):
    if isinstance(value, str):
        for source in inputs:
            try:
                relative = source.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                relative = source.name
            value = value.replace(str(source), relative)
            value = value.replace(source.as_posix(), relative)
        if key in ("path", "basePath", "base_path") and Path(value).is_absolute():
            try:
                return Path(value).resolve().relative_to(ROOT).as_posix()
            except ValueError:
                return value
        return value
    if isinstance(value, list):
        return [repository_relative(item, inputs, key) for item in value]
    if isinstance(value, dict):
        return {name: repository_relative(item, inputs, name) for name, item in value.items()}
    return value


def freeze():
    """結果を実行する前だけに、入力byteと閾値を新規固定する。"""
    if PLAN.exists():
        raise ValueError("固定済みの計画は上書きしません")
    inputs = []
    for case in ("single-house", "standalone-tower"):
        fixture = ROOT / f"tests/data/buildspec/{case}.yaml"
        for version in ("1.21.11", "26.1.2", "26.2", "26.3"):
            for fmt in ("schem", "litematic"):
                spec = yaml.safe_load(fixture.read_text(encoding="utf-8"))
                spec["minecraftVersion"] = version
                spec["config"] = {"output": {"format": fmt}}
                path = ROOT / f"evaluation/cases/structure-detail/{case}-{version}-{fmt}.yaml"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(spec, allow_unicode=True),
                                encoding="utf-8", newline="\n")
                inputs.append({"case": case, "version": version, "format": fmt,
                               "seed": spec["seed"], "source": reference(fixture),
                               "input": reference(path)})
    write_json(PLAN, {
        "schema_version": 1, "method": METHOD,
        "frozen_at": datetime.now(UTC).isoformat(),
        "base_commit": "b416651866bd96fd45f574810c1d89c030d7cf9e",
        "design_commit": "b2f7ab6",
        "case_definition": reference(ROOT / "evaluation/cases/fixed-cases.json"),
        "thresholds": {"column_continuity": 1.0, "flat_ratio_max": 0.35,
                       "local_flat_fail_allowed": False, "structural_errors_allowed": False,
                       "c_requirements_must_pass": True, "facade_mix_max": 0.85},
        "inputs": inputs,
        "regression": {"seeds": [42, 73], "cores": [16, 32, 64],
                       "reverse": True, "resume": list(pipeline.STAGES), "angle": 90,
                       "styles": ["medieval", "japanese", "modern"],
                       "tower_shapes": ["round", "square"],
                       "negative": "最初の期待柱の先頭セルをairへ置換"},
        "performance": {"reference": reference(ROOT / "benchmarks/make-baseline.json"),
                        "scales": ["tiny", "medium", "large"], "repetitions": 3,
                        "version": "1.21.11", "format": "schem",
                        "input_rule": "scripts/benchmark.py _make_workerと同じsize/要素寸法"},
    })


def cells(path):
    imported = read_schematic(path)
    return {tuple((pos + imported.offset).to_list()): state.to_string()
            for pos, state in imported.volume if state.id != "minecraft:air"}


def placement_hash(value):
    return hashlib.sha256(json.dumps(sorted(value.items()), separators=(",", ":")).encode())


def accepted(metrics, diagnostics, requirements, thresholds):
    column = metrics["column_continuity"]
    flat = metrics["flat_ratio"]
    return {
        "column_continuity": column["denominator"] > 0
        and column["value"] == thresholds["column_continuity"],
        "flat_ratio": flat["denominator"] > 0 and flat["value"] is not None
        and flat["value"] <= thresholds["flat_ratio_max"]
        and not any(d["code"] == "E201" for d in diagnostics),
        "structure": not any(d["severity"] == "error" or d["code"] in ("E101", "E203")
                             for d in diagnostics),
        "c_requirements": all(r["result"] == "success" for r in requirements),
    }


def critic_context(blueprint_path, source):
    blueprint, errors = validate_and_load(json.loads(blueprint_path.read_bytes()),
                                         base_dir=source.parent)
    if errors:
        raise ValueError([e.format() for e in errors])
    sidecar = json.loads(blueprint_path.with_suffix(".json.meta.json").read_bytes())
    context = sidecar["context"]
    style_name = context["resolved"]["buildspec"]["style"]
    style = yaml.safe_load((ROOT / f"src/mcblueprint/data/patterns/styles/{style_name}.yaml")
                           .read_text(encoding="utf-8"))
    plan = plan_from_metadata(blueprint.metadata["planning"], massing=context["massing"],
                              offset=context["site"]["offset"],
                              decoration_range=style["decoration"]["density"],
                              fixed_roles=context["fixedRoles"])
    return blueprint, plan


def face_audit(blueprint_path, source):
    blueprint, plan = critic_context(blueprint_path, source)
    accumulator = CriticAccumulator(plan)
    for region in pipeline.generated_blueprint_regions(blueprint):
        accumulator.consume(region)
    measured = accumulator.finalize()
    faces = defaultdict(list)
    # 既存Criticが確定した投影窓を記録する。セルから外壁を推定し直さない。
    for (element, face, segment, u, v), values in accumulator.flat_windows.items():
        faces[f"{element}:{face}"].append({
            "segment": segment, "u": u, "v": v, "samples": len(values),
            "min_depth": min(values), "max_depth": max(values),
            "flat": max(values) == min(values),
        })
    return {"critic_metrics": measured.metrics, "faces": {
        face: {"windows": windows,
               "flat_ratio": {"numerator": sum(w["flat"] for w in windows),
                              "denominator": len(windows),
                              "value": sum(w["flat"] for w in windows) / len(windows)}}
        for face, windows in faces.items()
    }}


def worker(source, output, *, emit=True):
    samples = []
    started = time.perf_counter()

    def hook(stage):
        samples.append({"stage": stage, "elapsed_seconds": time.perf_counter() - started,
                        "rss_at_stage_start_bytes": benchmark._process_rss()})

    try:
        result = pipeline.make(source, output=output, emit=["blueprint"] if emit else [],
                               stage_hook=hook)
    except pipeline.PipelineError as exc:
        if emit:
            raise
        return {"code": exc.code, "error": str(exc),
                "make_runtime_seconds": time.perf_counter() - started,
                "process_peak_rss_bytes": benchmark._process_peak_rss(),
                "stage_hook_samples": samples}
    elapsed = time.perf_counter() - started
    peak = benchmark._process_peak_rss()
    return {"code": result.code, "make_runtime_seconds": elapsed,
            "process_peak_rss_bytes": peak, "stage_hook_samples": samples}


def metadata():
    return {"method": METHOD, "script": reference(Path(__file__).resolve()),
            "benchmark_helpers": reference(ROOT / "scripts/benchmark.py"),
            "plan": reference(PLAN), "environment": benchmark._profile_environment(),
            "provenance": benchmark._benchmark_provenance(),
            "measured_at": datetime.now(UTC).isoformat(),
            "time_scope": "子プロセス起動からmake完了・worker JSON出力まで。build比較は除外。",
            "memory_metric": "独立子プロセスのmake直後のOS最大RSS/PeakWorkingSetSize。",
            "stage_sample_definition": "段開始時の累積時間と現在RSS。段単独ピークではない。",
            "unconfirmed": ["Minecraft実画面・人間の外観評価は未実施",
                            "NFR1の内蔵GPU・8GB想定端末は未測定",
                            "並行タスクのCPU/メモリ競合を制御していない"]}


def run_matrix(plan):
    cases = json.loads((ROOT / "evaluation/cases/fixed-cases.json").read_bytes())["cases"]
    record = {"schema_version": 1, "record_type": "structure-detail-evaluation",
              **metadata(), "results": []}
    extra_inputs = []
    with tempfile.TemporaryDirectory(prefix="structure-detail-") as temporary:
        folder = Path(temporary)
        for entry in plan["inputs"]:
            source = ROOT / entry["input"]["path"]
            if digest(source) != entry["input"]["sha256"]:
                raise ValueError(f"固定入力が変更されています: {source}")
            output = folder / f"make.{entry['format']}"
            seconds, measured = benchmark._isolated([
                sys.executable, str(Path(__file__).resolve()), "--worker", str(source), str(output),
            ])
            run = json.loads(output.with_suffix(output.suffix + ".run.json").read_bytes())
            blueprint = output.with_name("make.blueprint.json")
            rebuilt = folder / f"build.{entry['format']}"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["build", str(blueprint), "--format", entry["format"],
                             "-o", str(rebuilt)])
            actual, built = cells(output), cells(rebuilt)
            case = next(c for c in cases if c["id"] == entry["case"])
            requirements = benchmark._fixed_checks(case, output, run["diagnostics"])
            checks = accepted(run["metrics"], run["structuredDiagnostics"], requirements,
                              plan["thresholds"])
            audit = face_audit(blueprint, source)
            record["results"].append({
                **entry, **measured, "seconds": seconds,
                "engine_environment": run["environment"], "resolved_input": run["input"],
                "config": run["config"], "run_status": run["status"],
                "metrics": run["metrics"], "diagnostics": run["diagnostics"],
                "face_audit": audit,
                "structured_diagnostics": run["structuredDiagnostics"],
                "blueprint_sha256": digest(blueprint), "build_code": code,
                "placement_identical": actual == built,
                "placement_sha256": placement_hash(actual).hexdigest(),
                "build_placement_sha256": placement_hash(built).hexdigest(),
                "block_count": len(actual), "requirements": requirements,
                "build_requirements": benchmark._fixed_checks(case, rebuilt, run["diagnostics"]),
                "completion": checks, "completion_passed": all(checks.values()),
                "quality_passed": measured["code"] == 0,
            })
            write_json(RECORD, record)
            print(f"{entry['case']} {entry['version']} {entry['format']}: {checks}", flush=True)
        record["regression_checks"] = regressions(plan, folder)
        extra_inputs = list(folder.glob("*.yaml"))
    record["completion_passed"] = all(r["completion_passed"] for r in record["results"])
    record["quality_passed"] = all(r["quality_passed"] for r in record["results"])
    write_json(RECORD, record, extra_inputs)


def regressions(plan, folder):
    checks = {}
    for case in ("single-house", "standalone-tower"):
        entry = next(e for e in plan["inputs"] if e["case"] == case)
        source = ROOT / entry["input"]["path"]
        base = pipeline.make(source, output=folder / "base.schem", emit=["blueprint"])
        expected = cells(base.output)
        base_record = json.loads(base.record.read_bytes())
        expected_metrics = base_record["metrics"]

        def compare(name, source=source, expected=expected, expected_metrics=expected_metrics,
                    base_record=base_record, **kwargs):
            result = pipeline.make(source, output=folder / f"{name}.schem", **kwargs)
            run = json.loads(result.record.read_bytes())
            return {"code": result.code, "placement_identical": cells(result.output) == expected,
                    "metrics_identical": run["metrics"] == expected_metrics,
                    "diagnostics_identical": run["structuredDiagnostics"]
                    == base_record["structuredDiagnostics"],
                    "placement_sha256": placement_hash(cells(result.output)).hexdigest()}

        item = {"base_code": base.code, "repeat": compare("repeat"),
                "changed_seed": compare("seed73", overrides=["seed=73"]),
                "cores": {}, "resume": {}}
        for core in plan["regression"]["cores"]:
            overrides = [f"limits.region=[{core},{core},{core}]", "limits.halo=0"]
            item["cores"][str(core)] = compare(f"core{core}", overrides=overrides)
            original = pipeline.generate_regions
            with patch.object(pipeline, "generate_regions",
                              side_effect=lambda *a, original=original, **kw:
                              original(*a, **kw, reverse=True)):
                item["cores"][f"{core}-reverse"] = compare(f"reverse{core}", overrides=overrides)
        for stage in pipeline.STAGES:
            item["resume"][stage] = compare(stage, resume=folder / f"base.{stage}.json")
        blueprint_path = folder / "base.blueprint.json"
        blueprint, critic_plan = critic_context(blueprint_path, source)
        missing = critic_plan.columns[0].cells[0]

        def broken_regions(blueprint=blueprint, missing=missing):
            for region in pipeline.generated_blueprint_regions(blueprint):
                if region.core.contains(missing):
                    region.volume.set(missing, BlockState.of("air"),
                                      region.volume.attributes(missing))
                yield region

        negative = evaluate(critic_plan, broken_regions())
        item["negative_column"] = {"removed": missing.to_list(), "metrics": negative.metrics,
                                   "diagnostics": list(negative.diagnostics),
                                   "detected": any(d["code"] == "E101"
                                                   for d in negative.diagnostics)}
        # 回転前後のローカル投影値を比較する。状態の向きは配置比較とは別に検証する。
        rotated_spec = yaml.safe_load(source.read_text(encoding="utf-8"))
        element = blueprint.metadata["planning"]["element"]
        rotated_spec["elements"] = {element: {"angle": 90}}
        rotated_source = folder / "rotated.yaml"
        rotated_source.write_text(yaml.safe_dump(rotated_spec), encoding="utf-8", newline="\n")
        rotated = pipeline.make(rotated_source, output=folder / "rotated.schem")
        rotation_record = json.loads(rotated.record.read_bytes())
        item["rotation90"] = {"code": rotated.code, "input": rotated_spec,
                              "metrics": rotation_record["metrics"],
                              "diagnostics": rotation_record["structuredDiagnostics"]}
        checks[case] = item
    functional = []
    for entry in (plan["inputs"][0], plan["inputs"][8]):
        spec = yaml.safe_load((ROOT / entry["input"]["path"]).read_text(encoding="utf-8"))
        for style in plan["regression"]["styles"]:
            for shape in ([None] if spec["archetype"] == "house" else ["round", "square"]):
                spec["style"] = style
                if shape:
                    spec["features"]["shape"] = shape
                source = folder / "functional.yaml"
                source.write_text(yaml.safe_dump(spec), encoding="utf-8", newline="\n")
                result = pipeline.make(source, output=folder / "functional.schem")
                run = json.loads(result.record.read_bytes())
                functional.append({"archetype": spec["archetype"], "style": style, "shape": shape,
                                   "input": yaml.safe_load(source.read_bytes()),
                                   "input_sha256": digest(source),
                                   "code": result.code,
                                   "metrics": run["metrics"],
                                   "diagnostics": run["structuredDiagnostics"]})
    checks["styles_and_shapes"] = functional
    # Cの旧例検証のみ再利用し、Cの課題記録自体は更新しない。
    from mcblueprint.generator import generate
    from mcblueprint.loader import load_blueprint_dict, read_blueprint_json
    from mcblueprint.validator import validate

    legacy = []
    for path in sorted((ROOT / "examples").glob("*.json")):
        raw = read_blueprint_json(path)
        errors = validate(raw)
        volume = generate(load_blueprint_dict(raw)) if not errors else None
        legacy.append({**reference(path), "validation_errors": [e.format() for e in errors],
                       "cells": len(volume) if volume is not None else None})
    checks["legacy_examples"] = legacy
    return checks


def performance_spec(archetype, scale):
    name = "single-house" if archetype == "house" else "standalone-tower"
    spec = yaml.safe_load((ROOT / f"tests/data/buildspec/{name}.yaml").read_text(encoding="utf-8"))
    size = {"tiny": [32, 32, 32], "medium": [64, 48, 64], "large": [128, 96, 128]}[scale]
    spec.update(minecraftVersion="1.21.11", scale=scale, size=size)
    if archetype == "house" and scale != "tiny":
        spec["elements"] = {"house#1": {"width": size[0] - 3, "depth": size[0] - 3}}
    elif archetype == "tower" and scale != "tiny":
        spec["features"].update(shape="square", floors=8 if scale == "medium" else 17,
                                floor_height=5)
        spec["elements"] = {"tower#1": {"radius": size[0] // 2 - 3}}
    return spec


def run_performance(plan):
    baseline = json.loads((ROOT / plan["performance"]["reference"]["path"]).read_bytes())
    record = {"schema_version": 1, "record_type": "structure-detail-performance",
              **metadata(), "reference": plan["performance"]["reference"],
              "baseline_environment": baseline["environment"], "results": [],
              "comparison_limits": [
                  "Cと入力byte・Python版・CPU・メモリ総量を照合。実装と依存版は同一ではない。",
                  "Cの時間境界はschem export、Dは追加されたscene出力も含むmake全体。",
                  "起動からworker JSON出力までを測定。runnerのimport量も時間に含む。",
                  "巨大地形を未測定のため、NFR3全体の達成は判定しない。",
              ]}
    with tempfile.TemporaryDirectory(prefix="structure-performance-") as temporary:
        folder = Path(temporary)
        for scale in plan["performance"]["scales"]:
            for archetype in ("house", "tower"):
                source = folder / "spec.yaml"
                source.write_text(yaml.safe_dump(performance_spec(archetype, scale),
                                                allow_unicode=True), encoding="utf-8", newline="\n")
                old = next(r for r in baseline["results"] if r["archetype"] == archetype
                           and r["scale"] == scale)
                samples = []
                for index in range(plan["performance"]["repetitions"]):
                    elapsed, sample = benchmark._isolated([
                        sys.executable, str(Path(__file__).resolve()), "--performance-worker",
                        str(source), str(folder / "result.schem"),
                    ])
                    sample["seconds"] = elapsed
                    samples.append(sample)
                    print(f"performance {archetype}-{scale} {index + 1}/3 {elapsed:.3f}s",
                          flush=True)
                seconds = statistics.median(s["seconds"] for s in samples)
                rss = statistics.median(s["process_peak_rss_bytes"] for s in samples)
                record["results"].append({
                    "archetype": archetype, "scale": scale, "input_sha256": digest(source),
                    "input": performance_spec(archetype, scale),
                    "input_identical_to_c": all(s["input_sha256"] == digest(source)
                                                for s in old["samples"]),
                    "samples": samples, "seconds_median": seconds,
                    "process_peak_rss_bytes_median": rss,
                    "seconds_delta": seconds - old["seconds_median"],
                    "rss_delta_bytes": rss - old["process_peak_rss_bytes_median"],
                    "baseline_seconds_median": old["seconds_median"],
                    "baseline_rss_bytes_median": old["process_peak_rss_bytes_median"],
                    "measurement_completed": all("error" not in s for s in samples),
                    "nfr2_target_seconds": {"tiny": None, "medium": 10, "large": 120}[scale],
                })
                write_json(PERFORMANCE, record)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--performance", action="store_true")
    parser.add_argument("--worker", nargs=2, metavar=("SOURCE", "OUTPUT"))
    parser.add_argument("--performance-worker", nargs=2, metavar=("SOURCE", "OUTPUT"))
    args = parser.parse_args()
    if args.freeze:
        freeze()
        return
    if args.worker:
        print(json.dumps(worker(*(Path(p) for p in args.worker))))
        return
    if args.performance_worker:
        print(json.dumps(worker(*(Path(p) for p in args.performance_worker), emit=False)))
        return
    plan = json.loads(PLAN.read_bytes())
    if args.matrix:
        run_matrix(plan)
    if args.performance:
        run_performance(plan)


if __name__ == "__main__":
    run()
