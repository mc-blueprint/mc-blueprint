"""#241の起点と住宅・塔・接合・城郭の4種hashを同一保存先で比較する。"""

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
BASE = "f413feca6255d0f9f5859cd8c8078a6f83312d96"


def baseline_module(name, file):
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / "src/mcblueprint" / file)
    sys.modules[name] = module
    source = subprocess.check_output(["git", "show", BASE + ":src/mcblueprint/" + file])
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def run():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_junction_planning import joined_plan

    old_plan = baseline_module("town241_old_planning", "planning.py")
    old_castle = baseline_module("town241_old_castle", "castle_planning.py")
    old_pipeline = baseline_module("town241_old_pipeline", "pipeline.py")
    old_critic = baseline_module("town241_old_critic", "critic.py")
    old_pipeline.CriticAccumulator = old_critic.CriticAccumulator
    old_pipeline.plan_from_metadata = old_critic.plan_from_metadata
    old_pipeline.plan_buildspec = old_plan.plan_buildspec
    records = []
    work = ROOT / "output/town-regression"
    work.mkdir(parents=True, exist_ok=True)
    fixed = json.loads((ROOT / "evaluation/cases/structure-detail.json").read_bytes())["inputs"]
    cases = [(Path(e["input"]["path"]).stem, ROOT / e["input"]["path"], None) for e in fixed]
    large = json.loads((ROOT / "evaluation/cases/large-scale.json").read_bytes())
    castle = large["cases"][0]["buildSpec"]
    for version in ("1.21.11", "26.1.2", "26.2", "26.3"):
        for fmt in ("schem", "litematic"):
            for kind in ("junction", "castle"):
                source = work / f"{kind}-{version}-{fmt}.yaml"
                raw = {**(castle if kind == "castle" else {}), "minecraftVersion": version,
                       "scale": "medium", "seed": 42,
                       "config": {"preview": {"enabled": False}, "output": {"format": fmt}}}
                source.write_text(yaml.safe_dump(raw), encoding="utf-8", newline="\n")

                def joined(resolved):
                    result = joined_plan(resolved)
                    result.blueprint["minecraftVersion"] = resolved.buildspec["minecraftVersion"]
                    return result

                cases.append((source.stem, source, joined if kind == "junction" else None))
    for name, source, planner in cases:
        fmt = yaml.safe_load(source.read_bytes()).get("config", {}).get("output", {}).get(
            "format", "schem")
        output = work / f"{name}.{fmt}"
        signatures = []
        for implementation, default in ((old_pipeline, old_plan.plan_buildspec),
                                         (pipeline, pipeline.plan_buildspec)):
            baseline = implementation is old_pipeline
            modules = {"mcblueprint.castle_planning": old_castle} if baseline else {}
            with patch.dict(sys.modules, modules), patch.object(
                    implementation, "plan_buildspec", planner or default):
                result = implementation.make(source, output=output)
            signatures.append({"code": result.code, **performance.signature(output)})
        entry = {"case": name, "base": signatures[0], "current": signatures[1],
                 "matches_base": signatures[0] == signatures[1]}
        records.append(entry)
        (ROOT / "evaluation/records/town-241-regression.json").write_text(json.dumps(
            {"base_commit": BASE, "method": "起点のplanning/pipeline/castle/criticを"
             "同梱データ共有で実行。"
             "同一入力・保存先で配置・metrics・診断・sceneのSHA-256を比較。",
             "results": records}, ensure_ascii=False, indent=2) + "\n",
             encoding="utf-8", newline="\n")
        print(name, entry["matches_base"], flush=True)
        assert entry["matches_base"], entry


if __name__ == "__main__":
    run()
