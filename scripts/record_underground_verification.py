"""#242の完了したpytest成果から地下の固定行列を記録する。再生成は行わない。"""

import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def run(directory):
    suite = json.loads((directory / "summary.json").read_bytes())
    assert suite["tests"] == suite["passed"] and not any(
        suite[key] for key in ("failures", "errors", "skipped"))
    execution = json.loads((directory / "results.json").read_bytes())
    supplemental = directory / "supplemental-results.json"
    if supplemental.is_file():
        execution.extend(json.loads(supplemental.read_bytes()))
    execution = [r for r in execution if r["node"].startswith(
        "tests/test_underground_planning.py::")]
    assert len(execution) == 49 and all(r["exitCode"] == 0 for r in execution)
    results = []
    for source in sorted(directory.rglob("underground.yaml")):
        if not source.parent.name.startswith(("test_underground_fixed_version",
                                              "test_underground_other_seeds")):
            continue
        spec = yaml.safe_load(source.read_bytes())
        fmt = spec["config"].get("output", {}).get("format", "schem")
        output = source.with_suffix("." + fmt)
        record = json.loads(output.with_suffix(output.suffix + ".run.json").read_bytes())
        routes = record["metrics"]["walkability"]["routes"]
        assert output.is_file() and record["status"] == "passed"
        assert routes and all(r["status"] == "reachable" for r in routes)
        results.append({"archetype": spec["archetype"], "scale": spec["scale"],
                        "minecraftVersion": spec["minecraftVersion"], "format": fmt,
                        "seed": spec["seed"], "status": record["status"],
                        "reachableRoutes": len(routes), "environment": record["environment"],
                        "metricsSha256": digest(record["metrics"]),
                        "diagnosticsSha256": digest(record["structuredDiagnostics"])})
    assert len(results) == 20, len(results)
    inputs = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
        "tests/test_underground_planning.py", "src/mcblueprint/underground_planning.py",
        "src/mcblueprint/critic.py", "src/mcblueprint/plan_contract.py",
        "evaluation/cases/large-scale.json", "src/mcblueprint/data/lock.json",
        "src/mcblueprint/data/patterns/layouts/medieval/underground_city.yaml")}
    record = {"schemaVersion": 1, "recordType": "underground-site-verification", "issue": 242,
              "baseCommit": "a408105", "designCommit": "b9129db",
              "method": "Python 3.12の独立した49ノードを担当venvで実行。"
              "本記録はpytestの成功成果を読み取り、生成は繰り返さない。性能判定は対象外。",
              "commands": [r["command"] for r in execution],
              "suiteSummary": suite,
              "testSummary": {"passed": len(execution), "failed": 0},
              "matrixCoverage": {"versionFormat": {"archetypes": ["cave", "underground_city"],
                  "scale": "huge", "seed": 42, "conditions": 16},
                  "otherSeeds": {"seeds": [73, 101], "conditions": 4,
                                 "additionalBranches": 1}},
              "inputs": inputs, "results": results}
    (ROOT / "evaluation/records/underground-242.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(results)}件の成功成果から記録しました")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
