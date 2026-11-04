"""起点commitの直接依存実装と、同じ入力・同じ保存先で4種hashを比較する。"""

import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import evaluate_make_performance as performance
import yaml

from mcblueprint import pipeline

ROOT = Path(__file__).resolve().parents[1]


def baseline_module(name, file):
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / "src/mcblueprint" / file)
    sys.modules[name] = module
    source = subprocess.check_output(["git", "show", "ec5a878:src/mcblueprint/" + file])
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def run():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_junction_planning import joined_plan

    old_plan = baseline_module("castle240_old_planning", "planning.py")
    old_pipeline = baseline_module("castle240_old_pipeline", "pipeline.py")
    old_pipeline.plan_buildspec = old_plan.plan_buildspec
    records = []
    work = ROOT / "output/castle-regression"
    work.mkdir(exist_ok=True)
    fixed = json.loads((ROOT / "evaluation/cases/structure-detail.json").read_bytes())["inputs"]
    cases = [(Path(e["input"]["path"]).stem, ROOT / e["input"]["path"], None) for e in fixed]
    for version in ("1.21.11", "26.1.2", "26.2", "26.3"):
        for fmt in ("schem", "litematic"):
            source = work / f"junction-{version}-{fmt}.yaml"
            source.write_text(
                yaml.safe_dump(
                    {
                        "minecraftVersion": version,
                        "seed": 42,
                        "config": {"preview": {"enabled": False}, "output": {"format": fmt}},
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )

            def joined(resolved):
                result = joined_plan(resolved)
                result.blueprint["minecraftVersion"] = resolved.buildspec["minecraftVersion"]
                return result

            cases.append((source.stem, source, joined))

    for name, source, planner in cases:
        fmt = (
            yaml.safe_load(source.read_bytes())
            .get("config", {})
            .get("output", {})
            .get("format", "schem")
        )
        output = work / f"{name}.{fmt}"
        signatures = []
        for implementation, default in (
            (old_pipeline, old_plan.plan_buildspec),
            (pipeline, pipeline.plan_buildspec),
        ):
            with patch.object(implementation, "plan_buildspec", planner or default):
                result = implementation.make(source, output=output)
            signatures.append({"code": result.code, **performance.signature(output)})
        entry = {
            "case": name,
            "base": signatures[0],
            "current": signatures[1],
            "matches_base": signatures[0] == signatures[1],
        }
        records.append(entry)
        path = ROOT / "evaluation/records/castle-240-regression.json"
        path.write_text(
            json.dumps(
                {
                    "base_commit": "ec5a878",
                    "method": ("起点のplanning.py/pipeline.pyを読み込み、変更のない依存実装と"
                               "現在の同梱データを共有。同じ入力・保存先で実行。"),
                    "results": records,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(name, entry["matches_base"], flush=True)
        assert entry["matches_base"], entry


if __name__ == "__main__":
    run()
