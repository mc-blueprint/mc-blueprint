import hashlib
import json
import re
import runpy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
CASES_PATH = Path("evaluation/cases/fixed-cases.json")
BASELINE_PATH = Path("evaluation/records/baseline.json")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def read_json(path: Path) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def file_hash(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def test_case_definitions_and_references_are_valid() -> None:
    cases_data = read_json(CASES_PATH)
    cases = cases_data["cases"]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert {"single-house", "standalone-tower", "connected-pair", "slope-settlement"} == set(ids)

    for case in cases:
        assert case["task"]
        assert case["requirements"]
        assert case["views"]
        for reference in case["references"]:
            assert (ROOT / reference["path"]).is_file()
            assert SHA256.fullmatch(reference["sha256"])
            assert file_hash(reference["path"]) == reference["sha256"]
            assert reference["license"]

    pair = next(case for case in cases if case["id"] == "connected-pair")
    assert "共有壁" in pair["task"]
    assert "渡り廊下は設けない" in pair["task"]
    assert any("開口" in requirement for requirement in pair["requirements"])
    assert any("連続した切妻屋根" in requirement for requirement in pair["requirements"])


def test_baseline_records_have_consistent_statuses_and_hashes() -> None:
    cases = read_json(CASES_PATH)["cases"]
    cases_by_id = {case["id"]: case for case in cases}
    baseline = read_json(BASELINE_PATH)
    assert baseline["schemaVersion"] == 1
    assert baseline["recordType"] == "baseline"
    assert {run["caseId"] for run in baseline["runs"]} == set(cases_by_id)

    for run in baseline["runs"]:
        case = cases_by_id[run["caseId"]]
        definition = run["caseDefinition"]
        assert definition["path"] == CASES_PATH.as_posix()
        assert definition["sha256"] == file_hash(definition["path"])
        assert run["status"] in {"success", "failure", "unhandled"}
        assert len(run["requirementResults"]) == len(case["requirements"])
        recorded_requirements = {item["requirement"] for item in run["requirementResults"]}
        assert recorded_requirements == set(case["requirements"])
        for item in run["requirementResults"]:
            assert item["status"] in {"pass", "fail", "unknown"}
            if item["status"] == "unknown":
                assert item["reason"]
                assert item["general"] is None
                assert item["experienced"] is None
            else:
                assert item["reason"]

        for category in ("appearance", "interior"):
            score = run["scores"][category]
            assert score["status"] in {"not-evaluated", "evaluated"}
            if score["status"] == "not-evaluated":
                assert score["reason"]
                assert score["general"] is None
                assert score["experienced"] is None
            else:
                assert all(1 <= score[group] <= 5 for group in ("general", "experienced"))

        execution = run["execution"]
        expected_views = set(case["views"])
        artifacts = execution["viewArtifacts"]
        artifact_views = {artifact["view"] for artifact in artifacts}
        assert set(execution["fixedViews"]) == expected_views
        assert artifact_views <= expected_views
        assert set(run["missingViews"]) == expected_views - artifact_views
        for artifact in artifacts:
            assert SHA256.fullmatch(artifact["sha256"])
            assert file_hash(artifact["path"]) == artifact["sha256"]

        if execution["model"] is None:
            assert execution["modelStatus"] in {"not-applicable", "not-run", "unknown"}
            assert execution["modelReason"]
        if execution["seed"] is None:
            assert execution["seedStatus"] == "not-run"
            assert execution["seedReason"]
        if run["input"] is None or run["output"] is None:
            assert run["status"] == "unhandled"
            assert run["notes"]
        for artifact in (run["input"], run["output"]):
            if artifact is not None:
                assert SHA256.fullmatch(artifact["sha256"])
                assert file_hash(artifact["path"]) == artifact["sha256"]
        if run["status"] == "success":
            assert execution["validate"] == "success"
            assert execution["build"] == "success"
            assert run["output"] is not None

    for control in baseline["controls"]:
        assert control["status"] in {"success", "failure", "unhandled"}
        assert SHA256.fullmatch(control["input"]["sha256"])
        assert file_hash("evaluation/controls/unknown-operation.json") == control["input"]["sha256"]
        assert control["execution"]["error"]


def test_evaluation_document_links_resolve() -> None:
    text = (ROOT / "docs/EVALUATION.md").read_text(encoding="utf-8")
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
    assert links
    for link in links:
        assert (ROOT / "docs" / link).is_file()


def test_hashed_text_fixtures_use_lf() -> None:
    cases = read_json(CASES_PATH)["cases"]
    paths = {CASES_PATH.as_posix(), "evaluation/controls/unknown-operation.json"}
    paths.update(reference["path"] for case in cases for reference in case["references"])
    for path in paths:
        assert b"\r" not in (ROOT / path).read_bytes(), path


def test_structure_detail_inputs_were_frozen_before_measurement() -> None:
    plan = read_json(Path("evaluation/cases/structure-detail.json"))
    record = read_json(Path("evaluation/records/structure-detail.json"))
    assert plan["frozen_at"] < record["measured_at"]
    assert plan["thresholds"]["column_continuity"] == 1
    assert plan["thresholds"]["flat_ratio_max"] == 0.35
    expected = {(case, version, fmt)
                for case in ("single-house", "standalone-tower")
                for version in ("1.21.11", "26.1.2", "26.2", "26.3")
                for fmt in ("schem", "litematic")}
    def key(r):
        return r["case"], r["version"], r["format"]

    assert {key(r) for r in plan["inputs"]} == expected
    assert {key(r) for r in record["results"]} == expected
    assert len(record["results"]) == len(plan["inputs"]) == 16
    for item in plan["inputs"]:
        assert file_hash(item["input"]["path"]) == item["input"]["sha256"]
        assert b"\r" not in (ROOT / item["input"]["path"]).read_bytes()
        assert item["seed"] == 42
    assert record["plan"]["sha256"] == file_hash(record["plan"]["path"])


def test_structure_detail_records_keep_local_counts_and_failures() -> None:
    record = read_json(Path("evaluation/records/structure-detail.json"))
    for run in record["results"]:
        assert run["input"]["sha256"] == run["resolved_input"]["sha256"]
        assert run["placement_identical"]
        assert run["placement_sha256"] == run["build_placement_sha256"]
        assert run["build_code"] == 0
        assert run["requirements"] == run["build_requirements"]
        assert run["seconds"] >= run["make_runtime_seconds"] > 0
        assert run["process_peak_rss_bytes"] > 0
        assert all(SHA256.fullmatch(run["engine_environment"][field])
                   for field in ("engineHash", "lockHash", "dataHash"))
        metrics = run["metrics"]
        audit = run["face_audit"]
        assert audit["faces"]
        assert all(metrics[name] == value for name, value in audit["critic_metrics"].items())
        faces = [face["flat_ratio"] for face in audit["faces"].values()]
        for field in ("numerator", "denominator"):
            assert sum(face[field] for face in faces) == metrics["flat_ratio"][field]
        for face in faces:
            assert face["denominator"] > 0
            assert face["value"] == face["numerator"] / face["denominator"]
        flat_passed = metrics["flat_ratio"]["value"] <= 0.35 and all(
            face["value"] <= 0.35 for face in faces)
        assert run["completion"]["flat_ratio"] == flat_passed
        assert run["completion_passed"] == all(run["completion"].values())
        assert run["quality_passed"] == (run["code"] == 0)
        assert run["run_status"] == ("passed" if run["code"] == 0 else "failed")
        assert run["code"] in (0, 3)
    assert record["completion_passed"] == all(r["completion_passed"] for r in record["results"])
    assert record["quality_passed"] == all(r["quality_passed"] for r in record["results"])


def test_structure_detail_regressions_cover_connections() -> None:
    checks = read_json(Path("evaluation/records/structure-detail.json"))["regression_checks"]
    for case in ("single-house", "standalone-tower"):
        item = checks[case]
        assert item["repeat"]["placement_identical"]
        assert item["repeat"]["metrics_identical"]
        assert not item["changed_seed"]["placement_identical"]
        assert set(item["cores"]) == {"16", "32", "64", "16-reverse", "32-reverse", "64-reverse"}
        assert set(item["resume"]) == {"resolve", "site", "massing", "detail", "blueprint"}
        for result in (*item["cores"].values(), *item["resume"].values()):
            assert result["placement_identical"]
            assert result["metrics_identical"]
            assert result["diagnostics_identical"]
            assert result["code"] == item["base_code"]
        negative = item["negative_column"]
        assert negative["detected"]
        assert negative["metrics"]["column_continuity"]["value"] < 1
        assert any(d["code"] == "E101" for d in negative["diagnostics"])
        rotated = item["rotation90"]
        assert rotated["metrics"]["column_continuity"]["value"] == 1
        assert rotated["metrics"]["flat_ratio"]["value"] <= 0.35
        assert not any(d["severity"] in ("error", "fail") for d in rotated["diagnostics"])
    functions = checks["styles_and_shapes"]
    assert len(functions) == 9
    assert {r["style"] for r in functions} == {"medieval", "japanese", "modern"}
    assert {r["shape"] for r in functions if r["archetype"] == "tower"} == {"round", "square"}
    for result in functions:
        assert result["code"] in (0, 3)
        assert result["metrics"]["column_continuity"]["value"] == 1
    assert checks["legacy_examples"]
    assert all(not r["validation_errors"] and r["cells"] > 0 for r in checks["legacy_examples"])
    previous = read_json(Path("evaluation/records/generation-core.json"))
    legacy = {r["path"]: r for r in previous["regression_checks"]["legacy_examples"]}
    for result in checks["legacy_examples"]:
        assert result["sha256"] == legacy[result["path"]]["sha256"]
        assert result["cells"] == legacy[result["path"]]["generated_cells"]


def test_structure_detail_performance_preserves_c_inputs() -> None:
    record = read_json(Path("evaluation/records/structure-detail-performance.json"))
    baseline = read_json(Path(record["reference"]["path"]))
    assert record["reference"]["sha256"] == file_hash(record["reference"]["path"])
    assert len(record["results"]) == 6
    for run in record["results"]:
        previous = next(r for r in baseline["results"]
                        if r["archetype"] == run["archetype"] and r["scale"] == run["scale"])
        source = yaml.safe_dump(run["input"], allow_unicode=True)
        # CのWindows計測はCRLF、今回の固定入力はLF。内容の一致もbyteで照合する。
        c_hash = hashlib.sha256(source.replace("\n", "\r\n").encode()).hexdigest()
        assert all(s["input_sha256"] == c_hash for s in previous["samples"])
        assert hashlib.sha256(source.encode()).hexdigest() == run["input_sha256"]
        assert len(run["samples"]) == 3
        assert run["seconds_delta"] == run["seconds_median"] - run["baseline_seconds_median"]
        assert run["rss_delta_bytes"] == (run["process_peak_rss_bytes_median"]
                                          - run["baseline_rss_bytes_median"])
        assert all(s["process_peak_rss_bytes"] > 0 and s["seconds"] > 0 for s in run["samples"])


def test_structure_detail_actually_crosses_core_boundaries() -> None:
    record = read_json(Path("evaluation/records/structure-detail-partition.json"))
    assert record["core"] == 7
    assert len(record["results"]) == 4
    for run in record["results"]:
        assert run["core_count"] > 1
        assert run["placement_identical"]
        assert run["metrics_identical"]
        assert run["diagnostics_identical"]
    for case in ("single-house", "standalone-tower"):
        forward, reverse = [r for r in record["results"] if r["case"] == case]
        assert forward["cores"] == reverse["cores"][::-1]


@pytest.mark.parametrize("failure", ("local-flat", "missing-column", "no-target", "c-floor"))
def test_structure_completion_does_not_hide_local_or_unmeasurable_failures(monkeypatch, failure):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    runner = runpy.run_path(str(ROOT / "scripts/evaluate_structure.py"))
    metrics = {"column_continuity": {"denominator": 2, "value": 1.0},
               "flat_ratio": {"denominator": 10, "value": 0.1}}
    diagnostics = []
    requirements = [{"result": "success"}]
    if failure == "local-flat":
        diagnostics = [{"code": "E201", "severity": "fail"}]
    elif failure == "missing-column":
        metrics["column_continuity"]["value"] = 0.5
    elif failure == "no-target":
        metrics["column_continuity"] = {"denominator": 0, "value": None}
    else:
        requirements = [{"result": "failure"}]
    thresholds = {"column_continuity": 1, "flat_ratio_max": 0.35}
    assert not all(runner["accepted"](metrics, diagnostics, requirements, thresholds).values())
