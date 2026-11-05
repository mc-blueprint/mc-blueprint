"""#242起点と住宅・塔・接合・城郭・街並みの配置/評価/診断/sceneを比較する。"""

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
BASE = "a408105"


def baseline_module(file):
    module = types.ModuleType("underground242_old_" + Path(file).stem)
    module.__file__ = str(ROOT / "src/mcblueprint" / file)
    sys.modules[module.__name__] = module
    source = subprocess.check_output(["git", "show", BASE + ":src/mcblueprint/" + file])
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


def run():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_junction_planning import joined_plan

    baseline = baseline_module("planning.py")
    old_critic = baseline_module("critic.py")
    for name in ("pipeline.py", "castle_planning.py", "town_planning.py",
                 "site_planning.py", "materialize.py"):
        old = subprocess.check_output(["git", "show", BASE + ":src/mcblueprint/" + name])
        assert old.replace(b"\r\n", b"\n") == (ROOT / "src/mcblueprint" / name).read_bytes(
        ).replace(b"\r\n", b"\n"), name
    work = ROOT / "output/underground-regression"
    work.mkdir(parents=True, exist_ok=True)
    fixed = json.loads((ROOT / "evaluation/cases/structure-detail.json").read_bytes())["inputs"]
    cases = [(Path(e["input"]["path"]).stem, ROOT / e["input"]["path"], None) for e in fixed]
    large = json.loads((ROOT / "evaluation/cases/large-scale.json").read_bytes())["cases"]
    for version in ("1.21.11", "26.1.2", "26.2", "26.3"):
        for fmt in ("schem", "litematic"):
            for kind in ("junction", "castle", "town"):
                raw = {} if kind == "junction" else next(
                    c["buildSpec"] for c in large if c["buildSpec"]["archetype"] == kind)
                source = work / f"{kind}-{version}-{fmt}.yaml"
                source.write_text(yaml.safe_dump({**raw, "minecraftVersion": version,
                    "scale": "medium", "seed": 42,
                    "config": {"preview": {"enabled": False}, "output": {"format": fmt}}}),
                    encoding="utf-8", newline="\n")

                def joined(resolved):
                    result = joined_plan(resolved)
                    result.blueprint["minecraftVersion"] = resolved.buildspec["minecraftVersion"]
                    return result

                cases.append((source.stem, source, joined if kind == "junction" else None))
    current = pipeline.plan_buildspec
    accumulator, critic_plan = pipeline.CriticAccumulator, pipeline.plan_from_metadata
    results = []
    for name, source, planner in cases:
        fmt = yaml.safe_load(source.read_bytes()).get("config", {}).get("output", {}).get(
            "format", "schem")
        output = work / f"{name}.{fmt}"
        signatures = []
        for default, critic in ((baseline.plan_buildspec, old_critic), (current, None)):
            with (
                patch.object(pipeline, "plan_buildspec", planner or default),
                patch.object(pipeline, "CriticAccumulator",
                             critic.CriticAccumulator if critic else accumulator),
                patch.object(pipeline, "plan_from_metadata",
                             critic.plan_from_metadata if critic else critic_plan),
            ):
                result = pipeline.make(source, output=output)
            signatures.append({"code": result.code, **performance.signature(output)})
        entry = {"case": name, "base": signatures[0], "current": signatures[1],
                 "matches_base": signatures[0] == signatures[1]}
        results.append(entry)
        (ROOT / "evaluation/records/underground-242-regression.json").write_text(json.dumps(
            {"base_commit": BASE, "method": "変更前planning/criticと現行実装を同じ入力・"
             "保存先で実行。その他の共有pipeline/planner/materializeの起点との一致も確認。"
             "配置・metrics・構造化診断・展開sceneのSHA-256を比較。",
             "results": results}, ensure_ascii=False, indent=2) + "\n",
             encoding="utf-8", newline="\n")
        print(name, entry["matches_base"], flush=True)
        assert entry["matches_base"], entry


if __name__ == "__main__":
    run()
