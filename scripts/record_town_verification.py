"""#241のpytest成果から固定行列の検証記録を作る。生成は再実行しない。"""

import hashlib
import json
import sys
from pathlib import Path

import benchmark
import yaml

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def run(directory):
    execution = json.loads((directory / "results.json").read_bytes())
    execution = [r for r in execution if r["node"].startswith("tests/test_town_planning.py::")]
    execution.sort(key=lambda r: r["node"])
    assert len(execution) == 36 and all(r["exitCode"] == 0 for r in execution)
    results = []
    for source in sorted(directory.rglob("town.yaml")):
        if (not source.parent.name.startswith(("test_town_fixed", "test_town_medium_fixed"))
                or "current" in source.parent.name):
            continue
        spec = yaml.safe_load(source.read_bytes())
        fmt = spec["config"].get("output", {}).get("format", "schem")
        output = source.with_suffix("." + fmt)
        record = json.loads(output.with_suffix(output.suffix + ".run.json").read_bytes())
        routes = record["metrics"]["walkability"]["routes"]
        assert output.is_file()
        assert routes and all(route["status"] == "reachable" for route in routes)
        assert not any(d["severity"] == "fail" for d in record["structuredDiagnostics"])
        results.append({"test": source.parent.name, "minecraftVersion": spec["minecraftVersion"],
                        "seed": spec["seed"], "scale": spec["scale"], "format": fmt,
                        "status": "passed", "reachableRoutes": len(routes),
                        "environment": record["environment"],
                        "metricsSha256": digest(record["metrics"]),
                        "diagnosticsSha256": digest(record["structuredDiagnostics"])})
    assert len(results) == 13, len(results)
    inputs = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        "tests/test_town_planning.py", "evaluation/cases/large-scale.json",
        "src/mcblueprint/town_planning.py", "src/mcblueprint/site_planning.py",
        "src/mcblueprint/critic.py",
        "src/mcblueprint/parcels.py", "src/mcblueprint/data/lock.json")}
    record = {"schemaVersion": 1, "recordType": "town-site-verification", "issue": 241,
              "baseCommit": "f413feca6255d0f9f5859cd8c8078a6f83312d96",
              "execution": "pytestの独立した36ノードを最大4プロセスで実行。入力・出力先を分離。",
              "commands": [benchmark._record_text(r["command"]) for r in execution],
              "testSummary": {"passed": len(execution), "failed": 0},
              "matrixCoverage": {"versionFormat": {"scale": "huge", "seed": 42,
                 "conditions": 8}, "scaleSeed": {"scale": "huge", "minecraftVersion": "1.21.11",
                 "format": "schem", "conditions": 3},
                 "mediumScaleSeed": {"scale": "medium", "minecraftVersion": "1.21.11",
                                     "format": "schem", "conditions": 3}},
              "inputs": inputs, "results": results}
    (ROOT / "evaluation/records/town-241.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(results)}件の成功成果から記録しました")


if __name__ == "__main__":
    run(Path(sys.argv[1]))
