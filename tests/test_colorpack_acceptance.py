"""人工パックの受入れ測定と、pack未使用を見逃さないことを確認する。"""

import copy
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mcblueprint.blockdata import supported_versions
from mcblueprint.colorpack import parse_colorpack
from mcblueprint.colorpack_selection import quantile, target_color
from mcblueprint.materialize import materialize

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/data/colorpack"
spec = importlib.util.spec_from_file_location(
    "evaluate_colorpack", ROOT / "scripts/evaluate_colorpack.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.mark.parametrize("q,expected", [(0.05, 0), (0.15, 1), (0.375, 3),
                                        (0.625, 5), (0.85, 7), (0.95, 8)])
def test_sampler_hand_calculation(q, expected):
    assert quantile([0, 2, 4, 6, 8], q) == pytest.approx(expected)
    distribution = {"oklab": {"L": [0, 2, 4, 6, 8], "a": [0, 4, 8, 12, 16],
                                "b": [0, 6, 12, 18, 24]}}
    settings = {"variation": 1, "saturation": 0.5, "accent": 1}
    with patch("mcblueprint.colorpack_selection.random.Random.random",
               return_value=(q - 0.5) / 0.9 + 0.5):
        assert target_color(distribution, settings, 42, "wall_main") == pytest.approx(
            (expected, expected * 2, expected * 3))
        assert target_color(distribution, settings, 42, "accent") == pytest.approx(
            (expected, expected * 4, expected * 6))


def test_reject_d28_even_when_output_is_valid():
    result = materialize(runner.probe("1.21.11", ["wall_main"]))
    with pytest.raises(ValueError, match="pack未使用"):
        runner.require_pack(result, ["wall_main"])


@pytest.mark.parametrize("version", supported_versions())
def test_report_all_metrics_and_formats(version, tmp_path):
    pack = parse_colorpack((DATA / "acceptance.json").read_text(encoding="utf-8"))
    case = json.loads((DATA / "acceptance-plan.json").read_bytes())["conditions"][0]
    result = runner.measure(pack, case, version, [0, 1], 0.3, 0.2, tmp_path)
    assert result["deterministic"] and result["formatMatches"]
    assert result["relations"]["denominator"] > 0
    assert set(result["relations"]["deltaQuantiles"]) == {"dL", "dC", "dh"}
    assert result["excluded"][0]["role"] == "trim"
    assert result["missing"] == {}
    assert result["accentViolations"] == 0
    for role in result["roles"].values():
        assert len(role["actualQuantiles"]) == len(role["projectedQuantiles"]) == 5
        assert role["originalMedianDistance"] >= 0
        assert role["materialRates"] and role["finalMaterialArea"]
    assert set(result["samples"][0]["formats"]) == {"schem", "litematic"}


def test_unsupported_pack_cannot_be_reported_as_passed(tmp_path):
    raw = json.loads((DATA / "acceptance.json").read_bytes())
    raw["materialPrior"] = []
    pack = parse_colorpack(json.dumps(raw))
    case = json.loads((DATA / "acceptance-plan.json").read_bytes())["conditions"][0]
    result = runner.measure(pack, case, "1.21.11", [0], 0, 0, tmp_path)
    assert result["status"] == "no_supported_roles"
    assert "passed" not in result


@pytest.mark.parametrize("kind", ["house", "tower"])
def test_building_scope_measures_final_core(kind, tmp_path):
    pack = parse_colorpack((DATA / "acceptance.json").read_text(encoding="utf-8"))
    case = {"scope": "building", "buildspec": {"scale": "tiny"}, "style": "medieval",
            "buildingType": kind, "biome": "plains", "roles": ["wall_main", "frame"]}
    if kind == "house":
        case["roles"].append("roof")
    result = runner.measure(pack, case, "1.21.11", [0, 1], 0.3, 0.2, tmp_path)
    assert result["scope"] == "building"
    assert result["formatMatches"] and result["deterministic"]
    assert result["roles"]["wall_main"]["areaRatioQuantiles"][2] != pytest.approx(1 / 3)


def test_executor_rejects_d28_and_counts_cap_violation(tmp_path, monkeypatch):
    pack = parse_colorpack((DATA / "acceptance.json").read_text(encoding="utf-8"))
    case = json.loads((DATA / "acceptance-plan.json").read_bytes())["conditions"][0]
    original = runner.materialize

    def fallback(*args, **kwargs):
        kwargs.pop("colorpack")
        return original(*args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(runner, "materialize", fallback)
        with pytest.raises(ValueError, match="pack未使用"):
            runner.measure(pack, case, "1.21.11", [0], 0, 0, tmp_path)
    raw = json.loads((DATA / "acceptance.json").read_bytes())
    raw["accentRule"][0]["maxAreaRatio"] = 0.1
    result = runner.measure(parse_colorpack(json.dumps(raw)), case, "1.21.11", [0], 0, 0, tmp_path)
    assert result["accentViolations"] == 1
    assert not result["passed"]


@pytest.mark.parametrize("low_support", [False, True])
def test_missing_or_unsupported_relations_do_not_inflate_denominator(tmp_path, low_support):
    raw = json.loads((DATA / "acceptance.json").read_bytes())
    if low_support:
        for rule in raw["relationRule"]:
            rule.update(n=4, subjects=2, nEff=1.5)
    else:
        raw["relationRule"] = []
    case = json.loads((DATA / "acceptance-plan.json").read_bytes())["conditions"][0]
    result = runner.measure(parse_colorpack(json.dumps(raw)), case, "1.21.11", [0], 0, 0, tmp_path)
    assert result["relations"]["denominator"] == 0
    assert result["relations"]["rate"] is None
    if low_support:
        assert result["relations"]["excluded"]["insufficient_direct_support"] > 0
    else:
        assert result["relations"]["noRuleSamples"] == 1


@pytest.mark.parametrize("key,value", [
    ("seeds", [0]), ("seeds", list(range(255))), ("seeds", list(range(1, 257))),
    ("seeds", [True, *range(1, 256)]), ("mix", [0]), ("mix", [0, 0.4]),
    ("mix", [0, 0.3, 0.3]), ("weatherAge", [0]), ("weatherAge", [0, 0.3]),
    ("synthetic", "false"),
])
def test_formal_plan_rejects_reduced_or_changed_series(key, value, tmp_path, monkeypatch):
    plan = json.loads((DATA / "acceptance-plan.json").read_bytes())
    plan["synthetic"] = False
    plan[key] = value
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")

    def unexpected(*args, **kwargs):
        pytest.fail("不正な正式計画で測定を開始しました")

    monkeypatch.setattr(runner, "measure", unexpected)
    with pytest.raises(ValueError):
        runner.evaluate(DATA / "acceptance.json", path, tmp_path / "report.json")
    assert not (tmp_path / "report.json").exists()


def test_formal_plan_runs_exact_full_series(tmp_path, monkeypatch):
    plan = json.loads((DATA / "acceptance-plan.json").read_bytes())
    plan["synthetic"] = False
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    observed = []

    def measure(pack, case, version, seeds, mix, age, work):
        observed.append((version, copy.copy(seeds), mix, age))
        return {"passed": True}

    monkeypatch.setattr(runner, "measure", measure)
    result = runner.evaluate(DATA / "acceptance.json", path, tmp_path / "report.json")
    assert result["completed"]
    assert len(observed) == 16
    assert all(seeds == list(range(256)) for _, seeds, _, _ in observed)
    assert {(mix, age) for _, _, mix, age in observed} == {(0, 0), (0, 0.2), (0.3, 0), (0.3, 0.2)}


def test_synthetic_plan_allows_small_actual_measurement(tmp_path):
    plan = json.loads((DATA / "acceptance-plan.json").read_bytes())
    plan.update(seeds=[0], mix=[0], weatherAge=[0])
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8", newline="\n")
    result = runner.evaluate(DATA / "acceptance.json", path, tmp_path / "report.json")
    assert result["synthetic"] and result["completed"]
    assert len(result["results"]) == 4
    assert all(len(case["samples"]) == 1 and case["formatMatches"] for case in result["results"])
