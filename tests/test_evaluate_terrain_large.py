from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_runner():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import evaluate_terrain_large
    finally:
        sys.path.remove(str(scripts))
    return evaluate_terrain_large


runner = load_runner()


def matrix(case: str = "castle-medieval") -> dict:
    return {"case": case, "scale": "medium", "seed": 42,
            "version": "1.21.11", "format": "schem"}


def contract() -> dict:
    return {"runnerSha256": "runner", "provenance": {"engine": "engine"},
            "runtime": {"python": "3.12", "dependencies": {"numpy": "2"}},
            "implementation": {"engine": "engine"},
            "evaluationScriptsSha256": {"benchmark.py": "script"},
            "composedConfigSha256": "config"}


def valid_result(selected: dict | None = None) -> dict:
    selected = selected or matrix()
    stages = {stage: {"code": 0, "matches": dict.fromkeys(runner.SIGNATURE_FIELDS, True)}
              for stage in runner.pipeline.STAGES}
    signature = {field: "a" * 64 for field in runner.SIGNATURE_FIELDS}
    for stage in stages.values():
        stage["signature"] = signature
    return {
        **selected, "frozenInputSha256": "frozen", "actualInputSha256": "actual",
        **contract(), "makeCode": 0, "runStatus": "passed", "buildCode": 0,
        "buildMatches": True, "signature": signature, "resume": stages,
        "configMatches": True,
        "edits": {"status": "passed", "code": 0, "signature": signature,
                  "matches": dict.fromkeys(runner.SIGNATURE_FIELDS, True),
                  "generationConflict": {"status": "rejected",
                                         "reasonCode": "input-generation-mismatch"}},
    }


def frozen_hash(spec: dict) -> str:
    return hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()


def test_matrix_spec_preserves_and_hashes_fixed_terrain_config() -> None:
    terrain = {"roughness": 0.5, "amplitude": 0.65, "erosion": 0.35,
               "caves": 0.15, "vegetation": 0.5}
    build_spec = {"formatVersion": 1, "minecraftVersion": "1.21.11",
                  "archetype": "terrain", "config": {"terrain": terrain}}
    case = {"buildSpec": build_spec}
    plan = {"scales": {"medium": [64, 48, 64]}}
    expected = {**build_spec, "scale": "medium", "size": [64, 48, 64], "seed": 42}
    frozen = {"sha256": frozen_hash(expected)}

    spec, input_hash, actual_hash = runner.matrix_spec(case, matrix("terrain"), plan, frozen)
    assert input_hash == frozen["sha256"]
    assert spec["config"]["terrain"] == terrain
    assert spec["config"]["preview"]["enabled"] is False
    assert spec["config"]["output"]["format"] == "schem"
    assert "preview" not in build_spec["config"]
    assert actual_hash == hashlib.sha256(runner.encoded_spec(spec)).hexdigest()


@pytest.mark.parametrize(("field", "value"), [
    ("makeCode", 3),
    ("buildCode", 2),
    ("runStatus", "failed"),
])
def test_successful_rejects_make_or_build_failure(field: str, value: object) -> None:
    result = valid_result()
    result[field] = value
    assert not runner.successful(result)


def test_successful_rejects_resume_failure_codes_even_with_matching_hashes() -> None:
    result = valid_result()
    result["resume"][runner.pipeline.STAGES[0]]["code"] = 3
    assert not runner.successful(result)


def test_successful_rejects_edit_hash_mismatch_even_if_marked_passed() -> None:
    result = valid_result()
    result["edits"]["matches"][runner.SIGNATURE_FIELDS[-1]] = False
    assert not runner.successful(result)


@pytest.mark.parametrize(("field", "value"), [
    ("runnerSha256", "older-runner"),
    ("provenance", {"engine": "older-engine"}),
    ("runtime", {"python": "3.11"}),
    ("implementation", {"engine": "older-engine"}),
    ("actualInputSha256", "different-input"),
    ("evaluationScriptsSha256", {"benchmark.py": "old-script"}),
    ("composedConfigSha256", "different-config"),
])
def test_reusable_requires_exact_execution_contract(field: str, value: object) -> None:
    selected = matrix()
    result = valid_result(selected)
    result.update({"frozenInputSha256": "frozen", "actualInputSha256": "actual"})
    expected = contract()
    assert runner.reusable([result], selected, "frozen", "actual", expected) is result
    result[field] = value
    assert runner.reusable([result], selected, "frozen", "actual", expected) is None


def test_deduplicate_keeps_reused_success_instead_of_older_placeholder() -> None:
    old = {**matrix(), "inputSha256": "frozen", "edits": {"status": "not-run"}}
    current = valid_result()
    current["inputSha256"] = "frozen"
    kept = runner.deduplicate([old, current], contract())
    assert kept == [current]


def test_completion_requires_each_full_matrix_row_with_current_contract() -> None:
    first = matrix("first")
    second = matrix("second")
    jobs = [{"matrix": first, "inputSha256": "frozen", "actualInputSha256": "actual",
             "composedConfigSha256": "config"},
            {"matrix": second, "inputSha256": "frozen", "actualInputSha256": "actual",
             "composedConfigSha256": "config"}]
    rows = []
    for selected in (first, second):
        row = valid_result(selected)
        row.update({"inputSha256": "frozen", "frozenInputSha256": "frozen",
                        "actualInputSha256": "actual", "composedConfigSha256": "config"})
        rows.append(row)
    assert runner.completion_passed(rows, jobs, contract())
    assert not runner.completion_passed(rows[:1], jobs, contract())
    rows[1]["runnerSha256"] = "stale"
    assert not runner.completion_passed(rows, jobs, contract())


def test_run_job_keeps_failure_artifacts_after_a_later_success(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "FAILURES", tmp_path / "failures")
    job = {"matrix": matrix(), "spec": {}, "colorpack": None,
           "inputSha256": "frozen", "actualInputSha256": "actual",
           "contract": contract(), "runnerSha256": "runner",
           "provenance": contract()["provenance"], "runtime": contract()["runtime"]}
    calls = 0

    def run_case(entry: dict, spec: dict, selected: dict, work: Path) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            (work / "partial.bin").write_bytes(b"preserved")
            (work / "broken.json").write_text('{"source":"/tmp/private/file',
                                                encoding="utf-8")
            raise RuntimeError("forced failure")
        result = valid_result(selected)
        result.update({"frozenInputSha256": "frozen", "actualInputSha256": "actual",
                       **contract()})
        return result

    monkeypatch.setattr(runner, "run_case", run_case)
    failed = runner.run_job(job)
    artifact = tmp_path / failed["failureArtifacts"] / "partial.bin"
    assert artifact.read_bytes() == b"preserved"
    assert failed["failureReason"] == "RuntimeError: forced failure"
    assert failed["artifactWarnings"] == [
        "broken.json: 匿名化できないためartifactから除外"
    ]
    assert not (artifact.parent / "broken.json").exists()
    assert runner.successful(runner.run_job(job))
    assert artifact.read_bytes() == b"preserved"


def test_record_lock_rejects_a_concurrent_writer(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    source = ("import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); "
              "from evaluate_terrain_large import record_lock; "
              "exec('with record_lock(Path(sys.argv[1]), Path(sys.argv[2])): pass')")
    with runner.record_lock(path, tmp_path / "locks"):
        result = subprocess.run([sys.executable, "-c", source, str(path),
                                 str(tmp_path / "locks")], cwd=runner.ROOT,
                                capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert "FileExistsError" in result.stderr


def test_run_returns_failure_exit_code_and_leaves_partial_completion_false(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    case = {"id": "only-case", "buildSpec": {"formatVersion": 1,
            "minecraftVersion": "1.21.11", "archetype": "terrain"}}
    scales = {"medium": [1, 1, 1], "huge": [2, 2, 2]}
    inputs = []
    for scale, size in scales.items():
        for seed in runner.SEEDS:
            spec = {**case["buildSpec"], "scale": scale, "size": size, "seed": seed}
            inputs.append({"caseId": case["id"], "scale": scale, "seed": seed,
                           "sha256": frozen_hash(spec)})
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"scales": scales, "cases": [case], "inputs": inputs}),
                         encoding="utf-8")
    record_path = tmp_path / "record.json"
    monkeypatch.setattr(runner, "PLAN", plan_path)
    monkeypatch.setattr(runner, "REUSE_RECORDS", ())
    monkeypatch.setattr(runner, "LOCKS", tmp_path / "locks")

    class ImmediatePool:
        def __init__(self, max_workers: int) -> None:
            pass

        def __enter__(self) -> ImmediatePool:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def submit(self, fn, job):
            future = concurrent.futures.Future()
            future.set_result({**job["matrix"], "makeCode": 1,
                               "failureReason": "expected plan failure"})
            return future

    monkeypatch.setattr(runner.concurrent.futures, "ProcessPoolExecutor", ImmediatePool)
    code = runner.run(["--case", "only-case", "--limit", "1", "--record", str(record_path)])
    record = json.loads(record_path.read_bytes())
    assert code == 1
    assert record["completionPassed"] is False
    assert record["results"][0]["makeCode"] == 1


def test_make_record_supports_pack_specific_external_destination(tmp_path: Path) -> None:
    palette = tmp_path / "colorpack-v1.json"
    palette.write_bytes(b"synthetic pack")
    destination = tmp_path / "terrain-large-pack.json"
    record = runner.make_record({}, palette, destination)
    assert record["colorpack"] == {"kind": "override", "path": palette.name,
                                    "sha256": runner.digest(palette)}
    assert record["recordPath"] == runner.performance.benchmark._record_path(destination.resolve())


def test_record_and_failure_artifacts_anonymize_user_paths(tmp_path: Path) -> None:
    absolute = str(Path.home() / "AppData/Local/Temp/issue-263/spec.yaml")
    payload = {"diagnostics": [{"source": absolute, "detail": f"failed: {absolute}"}]}
    record_path = tmp_path / "record.json"
    runner.write_json(record_path, payload)

    artifacts = tmp_path / "failure"
    artifacts.mkdir()
    (artifacts / "run.json").write_text(json.dumps(payload), encoding="utf-8")
    (artifacts / "scene.json.gz").write_bytes(gzip.compress(
        json.dumps(payload).encode("utf-8")
    ))
    (artifacts / "spec.yaml").write_text(f"source: {absolute}\n", encoding="utf-8")
    (artifacts / "broken.json").write_text(f'{{"source":"{absolute}', encoding="utf-8")
    (artifacts / "broken.gz").write_bytes(b"not gzip")
    errors = runner.sanitize_artifacts(artifacts)

    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["diagnostics"][0]["source"] == "<tmp>/issue-263/spec.yaml"
    assert {"broken.json", "broken.gz"} <= set(errors)
    assert not (artifacts / "broken.json").exists()
    assert not (artifacts / "broken.gz").exists()
    contents = [
        json.dumps(json.loads((artifacts / "run.json").read_text(encoding="utf-8"))),
        gzip.decompress((artifacts / "scene.json.gz").read_bytes()).decode("utf-8"),
        (artifacts / "spec.yaml").read_text(encoding="utf-8"),
    ]
    assert all(Path.home().name.casefold() not in content.casefold() for content in contents)
    assert all(str(Path.home()) not in content for content in contents)


def test_record_text_anonymizes_unix_temporary_paths() -> None:
    text = runner.performance.benchmark._record_text(
        "failed /tmp/private/spec.yaml and /var/folders/ab/cd/T/session.log")
    assert "/tmp/private" not in text
    assert "/var/folders" not in text
    assert text.count("<tmp>") == 2


def test_reuse_requires_composed_config_hash() -> None:
    selected = matrix()
    result = valid_result(selected)
    result["inputSha256"] = "frozen"
    expected = contract()
    assert runner.reusable([result], selected, "frozen", "actual", expected) is result
    result["composedConfigSha256"] = "changed-config"
    assert runner.reusable([result], selected, "frozen", "actual", expected) is None


def test_deduplicate_prefers_current_implementation_fingerprint() -> None:
    old = valid_result()
    old["implementation"] = {"engine": "old"}
    current = valid_result()
    assert runner.deduplicate([old, current], contract()) == [current]


def test_deduplicate_prefers_current_composed_config_hash() -> None:
    old = valid_result()
    old["composedConfigSha256"] = "old-config"
    current = valid_result()
    key = runner.identity({**matrix(), "inputSha256": "frozen"})
    hashes = {key: "config"}
    assert runner.deduplicate([old, current], contract(), hashes) == [current]


def test_run_case_records_config_hash_from_generated_scene_metadata(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    generated_hash = "generated-config"

    def make(source: Path, *, output: Path, emit: list[str]):
        scene = output.with_name(output.stem + ".scene.json.gz")
        scene.with_suffix(scene.suffix + ".meta.json").write_text(
            json.dumps({"configHash": generated_hash}), encoding="utf-8")
        return SimpleNamespace(code=2, record=None)

    monkeypatch.setattr(runner.pipeline, "make", make)
    entry = {"frozenInputSha256": "frozen", "composedConfigSha256": "prepared-config",
             "provenance": contract()["provenance"], "runtime": contract()["runtime"],
             "runnerSha256": "runner", "contract": contract()}
    result = runner.run_case(entry, {}, matrix(), tmp_path)
    assert result["composedConfigSha256"] == generated_hash
    assert result["configMatches"] is False
    assert not runner.successful(result)


def test_unrelated_error_is_not_recorded_as_generation_rejection(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "input.yaml"
    source.write_text("input", encoding="utf-8")

    def fail(*args, **kwargs):
        raise OSError("temporary storage error")

    monkeypatch.setattr(runner, "apply_edits_file", fail)
    conflict = runner.check_generation_conflict(tmp_path / "scene.gz",
                                                tmp_path / "edits.json", source)
    assert conflict["status"] == "error"
    assert "reasonCode" not in conflict
    assert source.read_text(encoding="utf-8") == "input"


def test_input_generation_mismatch_is_the_only_rejected_conflict(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "input.yaml"
    source.write_text("input", encoding="utf-8")

    def fail(*args, **kwargs):
        raise runner.BlueprintError("edits: scene生成後に入力が変更されています")

    monkeypatch.setattr(runner, "apply_edits_file", fail)
    conflict = runner.check_generation_conflict(tmp_path / "scene.gz",
                                                tmp_path / "edits.json", source)
    assert conflict["status"] == "rejected"
    assert conflict["reasonCode"] == "input-generation-mismatch"
