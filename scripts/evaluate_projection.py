"""#233の同一入力を修正前後に生成し、配置・指標・診断・sceneを照合する。"""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import evaluate_make_performance as performance
import evaluate_structure as structure
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "benchmarks/.tmp/issue-233"
RECORD = ROOT / "evaluation/records/projection-233.json"


def run(phase):
    record = json.loads(RECORD.read_bytes()) if RECORD.exists() else {
        "schema_version": 1, "base_commit": "1c9ed70", "before": [], "after": [],
    }
    if record.setdefault(phase, []):
        raise ValueError(f"既存の{phase}記録は上書きしません")
    fixed = json.loads(structure.PLAN.read_bytes())["inputs"]
    cases = []
    for entry in fixed:
        source = ROOT / entry["input"]["path"]
        if structure.digest(source) != entry["input"]["sha256"]:
            raise ValueError(f"固定入力が変更されています: {source}")
        cases.append((source.stem, source, None))
    for style in ("medieval", "japanese", "modern"):
        for archetype, shape in (("house", None), ("tower", "round"), ("tower", "square")):
            for angle in (0, 90, 180, 270):
                name = f"{style}-{archetype}-{shape or 'rectangle'}-{angle}"
                source = WORK / name / "spec.yaml"
                source.parent.mkdir(parents=True, exist_ok=True)
                kind = "single-house" if archetype == "house" else "standalone-tower"
                spec = yaml.safe_load((ROOT / f"tests/data/buildspec/{kind}.yaml").read_bytes())
                spec["style"] = style
                if shape:
                    spec["features"]["shape"] = shape
                spec["elements"] = {f"{archetype}#1": {"angle": angle}}
                source.write_text(yaml.safe_dump(spec), encoding="utf-8", newline="\n")
                cases.append((name, source, None))
    for kind in ("single-house", "standalone-tower"):
        source = ROOT / next(e["input"]["path"] for e in fixed if e["case"] == kind)
        for reverse in (False, True):
            cases.append((f"{kind}-core7-{reverse}", source, reverse))
    original = structure.pipeline.generate_regions
    for name, source, reverse in cases:
        output = WORK / name / phase / "result.schem"
        output.parent.mkdir(parents=True, exist_ok=True)
        fmt = yaml.safe_load(source.read_bytes()).get("config", {}).get("output", {}).get(
            "format", "schem")
        output = output.with_suffix(f".{fmt}")
        overrides = [] if reverse is None else ["limits.region=[7,7,7]", "limits.halo=0"]

        def regions(*args, reverse=reverse, **kwargs):
            return original(*args, **kwargs, reverse=reverse)

        with patch.object(structure.pipeline, "generate_regions",
                          original if reverse is None else regions):
            result = structure.pipeline.make(source, output=output, overrides=overrides)
        entry = {"case": name, "input_sha256": structure.digest(source), "code": result.code,
                 "signature": performance.signature(output)}
        if phase != "before":
            before = next(e for e in record["before"] if e["case"] == name)
            entry["matches_before"] = all(entry[k] == before[k] for k in
                                          ("input_sha256", "code", "signature"))
            if not entry["matches_before"]:
                raise ValueError(f"修正前と異なります: {name}")
        record[phase].append(entry)
        structure.write_json(RECORD, record)
        print(f"{phase}: {name}: code={result.code}", flush=True)


def audit(phase="after"):
    """再生成せず記録と既存D記録を照合する。過去の絶対パス差だけを明示的に扱う。"""
    record = json.loads(RECORD.read_bytes())
    assert len(record["before"]) == len(record[phase]) == 56
    assert all(e["matches_before"] and e["code"] == 0 for e in record[phase])
    for before, after in zip(record["before"], record[phase], strict=True):
        assert all(before[key] == after[key] for key in
                   ("case", "input_sha256", "code", "signature"))
    reference = ROOT / "evaluation/records/structure-detail.json"
    historical = ROOT / "evaluation/records/make-performance-210-develop-fc8fbf9.json"
    fixed = json.loads(reference.read_bytes())["results"]
    previous = json.loads(historical.read_bytes())
    checks = []
    for entry, old in zip(record[phase][:16], fixed, strict=True):
        name = entry["case"]
        output = WORK / name / phase / f"result.{old['format']}"
        run = json.loads(output.with_suffix(output.suffix + ".run.json").read_bytes())
        source = ROOT / old["input"]["path"]
        diagnostics = structure.repository_relative(run["structuredDiagnostics"], [source])
        previous_entry = next(e for e in previous["fixed-after"] if e["case"] == name)
        previous_root = Path(previous_entry["samples"][0]["implementation"]["pipeline"]).parents[2]
        current_path = json.dumps(str(ROOT))[1:-1].encode()
        previous_path = json.dumps(str(previous_root))[1:-1].encode()
        scene = gzip.decompress(output.with_name("result.scene.json.gz").read_bytes())
        check = {
            "case": name,
            "input_identical": entry["input_sha256"] == old["input"]["sha256"],
            "placement_identical": (
                entry["signature"]["placement_sha256"] == old["placement_sha256"]),
            "metrics_identical": run["metrics"] == old["metrics"],
            "diagnostics_identical_after_path_translation": (
                diagnostics == old["structured_diagnostics"]),
            "scene_identical_after_path_translation": hashlib.sha256(
                scene.replace(current_path, previous_path)).hexdigest()
            == previous_entry["signature"]["scene_json_sha256"],
        }
        assert all(value for key, value in check.items() if key != "case"), check
        checks.append(check)
    record["existing_records"] = {
        "references": [structure.reference(reference), structure.reference(historical)],
        "checks": checks,
        "path_translation": ("過去記録のみ入力worktreeの絶対パスを合わせる。"
                             "before/afterの4種hashは無変換。"),
    }
    partition_reference = ROOT / "evaluation/records/structure-detail-partition.json"
    previous_partitions = json.loads(partition_reference.read_bytes())["results"]
    entries = {entry["case"]: entry for entry in record[phase]}
    partitions = []
    for kind in ("single-house", "standalone-tower"):
        base = entries[f"{kind}-1.21.11-schem"]["signature"]
        for reverse in (False, True):
            name = f"{kind}-core7-{reverse}"
            signature = entries[name]["signature"]
            diagnostics = []
            for case in (f"{kind}-1.21.11-schem", name):
                run = WORK / case / phase / "result.schem.run.json"
                diagnostics.append(json.loads(run.read_bytes())["structuredDiagnostics"])
            check = {"case": kind, "reverse": reverse,
                     "placement_identical": (
                         signature["placement_sha256"] == base["placement_sha256"]),
                     "metrics_identical": signature["metrics_sha256"] == base["metrics_sha256"],
                     "scene_identical": signature["scene_json_sha256"] == base["scene_json_sha256"],
                     "diagnostics_order_identical": diagnostics[0] == diagnostics[1],
                     "diagnostics_identical": sorted(map(performance.digest, diagnostics[0]))
                     == sorted(map(performance.digest, diagnostics[1]))}
            old = next(e for e in previous_partitions
                       if e["case"] == kind and e["reverse"] == reverse)
            assert check["diagnostics_order_identical"] == old["diagnostics_order_identical"]
            assert all(check[key] for key in ("placement_identical", "metrics_identical",
                                             "scene_identical", "diagnostics_identical"))
            partitions.append(check)
    record["partitions"] = {"reference": structure.reference(partition_reference),
                            "checks": partitions}
    record["script"] = structure.reference(Path(__file__).resolve())
    record["audited_phase"] = phase
    record["implementation_root_verified"] = Path(structure.pipeline.__file__).is_relative_to(
        ROOT / "src")
    assert record["implementation_root_verified"]
    structure.write_json(RECORD, record)
    print("56条件の4種hash一致、既存D固定16件との一致を確認しました", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=(
        "before", "after", "after-review", "audit", "audit-review"))
    phase = parser.parse_args().phase
    if phase in ("audit", "audit-review"):
        audit("after-review" if phase == "audit-review" else "after")
    else:
        run(phase)
