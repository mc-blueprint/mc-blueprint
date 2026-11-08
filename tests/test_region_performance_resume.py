"""性能記録の再開で実装と入力を混在させない。"""

import argparse
import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def measurement(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    region = importlib.import_module("evaluate_region_performance")
    benchmark = importlib.import_module("benchmark")
    performance = importlib.import_module("evaluate_make_performance")
    source = tmp_path / "spec.yaml"
    source.write_text("seed: 42\n", encoding="utf-8", newline="\n")
    provenance = {"engine_sha256": "engine", "lock_sha256": "lock",
                  "default_config_sha256": "config", "commit": "commit"}
    monkeypatch.setattr(region, "RECORD", tmp_path / "record.json")
    monkeypatch.setattr(region, "cases", lambda suite: iter([("house", source, "schem")]))
    monkeypatch.setattr(benchmark, "_benchmark_provenance", lambda: dict(provenance))
    monkeypatch.setattr(benchmark, "_profile_environment", lambda: {})
    isolated = Mock(side_effect=lambda command: (0.1, {"process_peak_rss_bytes": 64}))
    monkeypatch.setattr(benchmark, "_isolated", isolated)
    monkeypatch.setattr(performance, "signature", lambda output: {"hash": "fixed"})
    args = argparse.Namespace(phase="before", suite="timing", profile=False, case=None,
                              repetitions=3)
    region.run(args)
    return region, args, source, provenance, isolated


def test_resume_reuses_only_matching_implementation_and_input(measurement):
    region, args, _, _, isolated = measurement
    region.run(args)
    assert isolated.call_count == 3


def test_resume_adds_only_missing_repetitions(measurement):
    region, args, _, _, isolated = measurement
    record = json.loads(region.RECORD.read_bytes())
    record["results"]["timing"]["before"]["house"]["samples"] = (
        record["results"]["timing"]["before"]["house"]["samples"][:1]
    )
    region.RECORD.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    isolated.reset_mock()
    region.run(args)
    assert isolated.call_count == 2
    result = json.loads(region.RECORD.read_bytes())["results"]["timing"]["before"]["house"]
    assert len(result["samples"]) == 3


def test_after_compares_matching_before(measurement):
    region, args, _, _, isolated = measurement
    args.phase = "after"
    region.run(args)
    result = json.loads(region.RECORD.read_bytes())["results"]["timing"]["after"]["house"]
    assert result["matches_before"]
    assert isolated.call_count == 6


@pytest.mark.parametrize("field", ["base_commit", "python", "script_sha256",
                                 "h01_script_sha256", "common_scripts_sha256",
                                 "implementation", "input_sha256", "missing_before"])
def test_after_rejects_mismatched_before_before_measurement(measurement, monkeypatch, field):
    region, args, source, _, isolated = measurement
    record = json.loads(region.RECORD.read_bytes())
    before = record["results"]["timing"]["before"]["house"]
    if field == "input_sha256":
        source.write_text("seed: 73\n", encoding="utf-8", newline="\n")
    elif field == "base_commit":
        monkeypatch.setattr(region, "BASE", "changed")
    elif field == "missing_before":
        record["results"]["timing"].pop("before")
    else:
        before["provenance"][field] = "changed"
    region.RECORD.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    saved = region.RECORD.read_bytes()
    isolated.reset_mock()
    args.phase = "after"
    with pytest.raises(ValueError, match="before側"):
        region.run(args)
    isolated.assert_not_called()
    assert region.RECORD.read_bytes() == saved


@pytest.mark.parametrize("key", ["engine_sha256", "lock_sha256", "default_config_sha256", "commit"])
def test_resume_rejects_changed_provenance_without_overwriting(measurement, key):
    region, args, _, provenance, isolated = measurement
    saved = region.RECORD.read_bytes()
    provenance[key] = "changed"
    with pytest.raises(ValueError, match="provenance"):
        region.run(args)
    assert isolated.call_count == 3
    assert region.RECORD.read_bytes() == saved


def test_resume_rejects_changed_input_without_overwriting(measurement):
    region, args, source, _, isolated = measurement
    saved = region.RECORD.read_bytes()
    source.write_text("seed: 73\n", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="入力hash"):
        region.run(args)
    assert isolated.call_count == 3
    assert region.RECORD.read_bytes() == saved


@pytest.mark.parametrize("missing_common", [False, True])
def test_resume_rejects_legacy_results_without_provenance(measurement, missing_common):
    region, args, _, _, isolated = measurement
    record = json.loads(region.RECORD.read_bytes())
    entry = record["results"]["timing"]["before"]["house"]
    if missing_common:
        entry["provenance"].pop("common_scripts_sha256")
    else:
        entry.pop("provenance")
    region.RECORD.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    saved = region.RECORD.read_bytes()
    with pytest.raises(ValueError, match="provenance"):
        region.run(args)
    assert isolated.call_count == 3
    assert region.RECORD.read_bytes() == saved


@pytest.mark.parametrize("module", ["evaluate_region_performance", "evaluate_make_performance",
                                    "benchmark", "evaluate_structure"])
def test_resume_rejects_changed_measurement_script(measurement, monkeypatch, module):
    region, args, _, _, isolated = measurement
    saved = region.RECORD.read_bytes()
    structure = importlib.import_module("evaluate_structure")
    reference = structure.reference
    changed_path = Path(importlib.import_module(module).__file__)

    def changed(path):
        result = reference(path)
        if path == changed_path:
            result["sha256"] = "changed"
        return result

    monkeypatch.setattr(structure, "reference", changed)
    with pytest.raises(ValueError, match="provenance"):
        region.run(args)
    assert isolated.call_count == 3
    assert region.RECORD.read_bytes() == saved
