"""固定課題を実際に複数coreへ分割し、逆順でも配置・指標が一致するか記録する。"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import evaluate_structure as evaluation

from mcblueprint import pipeline


def generate(source, output, *, reverse):
    cores = []
    original = pipeline.generate_regions

    def regions(*args, **kwargs):
        for region in original(*args, **kwargs, reverse=reverse):
            cores.append([region.core.min.to_list(), region.core.max.to_list()])
            yield region

    with patch.object(pipeline, "generate_regions", side_effect=regions):
        result = pipeline.make(source, output=output,
                               overrides=["limits.region=[7,7,7]", "limits.halo=0"])
    return result, cores


def run():
    plan = json.loads(evaluation.PLAN.read_bytes())
    record = {"schema_version": 1, "record_type": "structure-detail-partition",
              **evaluation.metadata(),
              "partition_script": evaluation.reference(Path(__file__).resolve()),
              "reason": "固定課題は各辺16未満なので16/32/64だけではcore境界を跨がない。",
              "core": 7, "results": []}
    with tempfile.TemporaryDirectory(prefix="structure-partition-") as temporary:
        folder = Path(temporary)
        for case in ("single-house", "standalone-tower"):
            entry = next(e for e in plan["inputs"] if e["case"] == case)
            source = evaluation.ROOT / entry["input"]["path"]
            base = pipeline.make(source, output=folder / "base.schem")
            expected = evaluation.cells(base.output)
            expected_run = json.loads(base.record.read_bytes())
            for reverse in (False, True):
                result, cores = generate(source, folder / "split.schem", reverse=reverse)
                measured = json.loads(result.record.read_bytes())
                expected_diagnostics = expected_run["structuredDiagnostics"]
                diagnostics = measured["structuredDiagnostics"]
                record["results"].append({
                    "case": case, "input": entry["input"], "reverse": reverse,
                    "code": result.code, "core_count": len(cores), "cores": cores,
                    "placement_identical": evaluation.cells(result.output) == expected,
                    "metrics_identical": measured["metrics"] == expected_run["metrics"],
                    "diagnostics_order_identical": diagnostics == expected_diagnostics,
                    "diagnostics_identical": sorted(json.dumps(d, sort_keys=True)
                                                    for d in diagnostics)
                    == sorted(json.dumps(d, sort_keys=True) for d in expected_diagnostics),
                    "base_diagnostics": expected_diagnostics, "diagnostics": diagnostics,
                })
    evaluation.write_json(evaluation.ROOT / "evaluation/records/structure-detail-partition.json",
                          record)
    print(json.dumps(record["results"], ensure_ascii=False))


if __name__ == "__main__":
    run()
