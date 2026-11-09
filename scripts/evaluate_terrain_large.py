"""Hの固定ケースを実行し、機能評価を再開可能な記録へ保存する。"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import tempfile
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import evaluate_make_performance as performance
import evaluate_structure as structure
import yaml

from mcblueprint import colorpack, pipeline
from mcblueprint.buildspec import load_buildspec, resolve_buildspec
from mcblueprint.cli import main
from mcblueprint.edits import apply_edits_file
from mcblueprint.errors import BlueprintError

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "evaluation/cases/large-scale.json"
RECORD = ROOT / "evaluation/records/terrain-large.json"
LOCKS = ROOT / "output/issue-263/record-locks"
FAILURES = ROOT / "evaluation/artifacts/issue-263/failures"
REUSE_RECORDS = (
    ROOT / "evaluation/records/castle-240.json",
    ROOT / "evaluation/records/town-241.json",
    ROOT / "evaluation/records/underground-242.json",
)
VERSIONS = ("1.21.11", "26.1.2", "26.2", "26.3")
FORMATS = ("schem", "litematic")
SEEDS = (42, 73, 101)
DEPENDENCIES = ("numpy", "pyfastnoiselite", "nbtlib", "jsonschema", "PyYAML")
SIGNATURE_FIELDS = (
    "placement_sha256", "metrics_sha256", "structured_diagnostics_sha256",
    "scene_json_sha256",
)
PATH_FIELDS = {"path", "recordpath", "source", "input_path", "output_path"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded_spec(spec: dict) -> bytes:
    return yaml.safe_dump(spec, allow_unicode=True, sort_keys=False).encode("utf-8")


def runtime_provenance() -> dict:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "dependencies": {name: importlib.metadata.version(name) for name in DEPENDENCIES},
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(record_value(value), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def record_value(value, key: str | None = None):
    if isinstance(value, dict):
        return {name: record_value(item, name.lower()) for name, item in value.items()}
    if isinstance(value, list):
        return [record_value(item) for item in value]
    if isinstance(value, str):
        if key in PATH_FIELDS and Path(value).is_absolute():
            return performance.benchmark._record_path(value)
        return performance.benchmark._record_text(value)
    return value


def sanitize_artifacts(root: Path) -> list[str]:
    errors = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            try:
                original = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                errors.append(path.relative_to(root).as_posix())
                continue
            if (sanitized := record_value(original)) != original:
                write_json(path, sanitized)
        elif path.suffix.lower() == ".gz":
            try:
                original = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, gzip.BadGzipFile,
                    EOFError, zlib.error):
                path.unlink(missing_ok=True)
                errors.append(path.relative_to(root).as_posix())
                continue
            if (sanitized := record_value(original)) != original:
                encoded = json.dumps(sanitized, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")
                path.write_bytes(gzip.compress(encoded, mtime=0))
        elif path.suffix.lower() in {".yaml", ".yml", ".txt", ".log"}:
            original = path.read_text(encoding="utf-8")
            if (sanitized := performance.benchmark._record_text(original)) != original:
                path.write_text(sanitized, encoding="utf-8", newline="\n")
    return errors


@contextmanager
def record_lock(path: Path, lock_dir: Path | None = None):
    lock_dir = lock_dir or LOCKS
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    with (lock_dir / f"{key}.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FileExistsError(f"評価記録は別プロセスが更新中です: {path}") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def make_record(plan: dict, palette: Path | None, record_path: Path) -> dict:
    resolved_path = record_path.resolve()
    reference = performance.benchmark._record_path(resolved_path)
    return {
        "schemaVersion": 1,
        "recordType": "terrain-large-functional-matrix",
        "issue": 263,
        "planSha256": digest(PLAN),
        "recordPath": reference,
        "scriptSha256": digest(Path(__file__).resolve()),
        "benchmarkScriptSha256": digest(ROOT / "scripts/evaluate_make_performance.py"),
        "evaluationScriptsSha256": evaluation_scripts_hashes(),
        "provenance": performance.benchmark._benchmark_provenance(),
        "runtime": runtime_provenance(),
        "colorpack": ({"kind": "override", "path": palette.name,
                       "sha256": digest(palette)} if palette else {"kind": "default"}),
        "measuredAt": datetime.now(UTC).isoformat(),
        "results": [],
    }


def evaluation_scripts_hashes() -> dict[str, str]:
    return {name: digest(ROOT / "scripts" / name) for name in
            ("evaluate_terrain_large.py", "evaluate_make_performance.py",
             "evaluate_structure.py", "benchmark.py")}


def install_colorpack(path: Path, root: Path) -> None:
    data = root / "data"
    pack_dir = data / "colorpack"
    pack_dir.mkdir(parents=True)
    shutil.copyfile(path, pack_dir / "colorpack.json")
    (pack_dir / "DATA_LICENSE").write_bytes(b"Synthetic evaluation fixture\n")
    vocabulary = Path(colorpack.__file__).parent / "data/colorpack-vocabulary.json"
    shutil.copyfile(vocabulary, data / "colorpack-vocabulary.json")
    raw = json.loads(path.read_bytes())
    files = {"pack": pack_dir / "colorpack.json", "license": pack_dir / "DATA_LICENSE",
             "vocabulary": data / "colorpack-vocabulary.json"}
    identity = {key: raw[key] for key in
                ("packVersion", "taxonomyVersion", "vocabularyVersion")}
    identity["sha256"] = {key: digest(file) for key, file in files.items()}
    write_json(data / "lock.json", {"colorpack": identity})
    colorpack._data_root = lambda: data


def run_case(entry: dict, spec: dict, matrix: dict, work: Path) -> dict:
    source = work / f"{matrix['case']}-{matrix['version']}-{matrix['format']}.yaml"
    source.write_bytes(encoded_spec(spec))
    output = work / f"{source.stem}.{matrix['format']}"
    result = pipeline.make(source, output=output, emit=["blueprint"])
    run = json.loads(result.record.read_bytes()) if result.record and result.record.exists() else {}
    scene_path = output.with_name(output.stem + ".scene.json.gz")
    scene_meta = scene_path.with_suffix(scene_path.suffix + ".meta.json")
    generated_config_hash = (json.loads(scene_meta.read_bytes()).get("configHash")
                             if scene_meta.is_file() else None)
    item = {
        **matrix, "frozenInputSha256": entry["frozenInputSha256"],
        "actualInputSha256": digest(source), "implementation": run.get("environment"),
        "composedConfigSha256": generated_config_hash,
        "configMatches": generated_config_hash == entry["composedConfigSha256"],
        "provenance": entry["provenance"], "runtime": entry["runtime"],
        "runnerSha256": entry["runnerSha256"],
        "evaluationScriptsSha256": entry["contract"]["evaluationScriptsSha256"],
        "colorpack": run.get("colorpack"),
        "makeCode": result.code, "runStatus": run.get("status"),
        "metrics": run.get("metrics"), "diagnostics": run.get("structuredDiagnostics"),
        "signature": None, "buildCode": None, "buildMatches": False,
        "resume": {}, "edits": {"status": "not-run"},
    }
    if result.code != 0 or not output.is_file():
        item["failureReason"] = "makeが正常終了しなかったため、後続検証を実行していません"
        return item

    item["signature"] = performance.signature(output)
    blueprint = output.with_name(output.stem + ".blueprint.json")
    built = work / f"{source.stem}.rebuilt.{matrix['format']}"
    item["buildCode"] = main(["build", str(blueprint), "--format", matrix["format"], "-o",
                              str(built)])
    item["buildMatches"] = (item["buildCode"] == 0 and built.is_file()
                            and structure.cells(output) == structure.cells(built))

    for stage in pipeline.STAGES:
        checkpoint = output.with_name(output.stem + f".{stage}.json")
        if not checkpoint.exists():
            item["resume"][stage] = {"code": None, "status": "missing-checkpoint"}
            continue
        resumed = pipeline.make(
            source, output=work / f"{source.stem}.{stage}-resume.{matrix['format']}",
            resume=checkpoint)
        if resumed.code == 0 and resumed.output.is_file():
            resumed_signature = performance.signature(resumed.output)
            matches = {field: resumed_signature[field] == item["signature"][field]
                       for field in SIGNATURE_FIELDS}
        else:
            resumed_signature = None
            matches = dict.fromkeys(SIGNATURE_FIELDS, False)
        item["resume"][stage] = {"code": resumed.code, "signature": resumed_signature,
                                 "matches": matches}

    scene_bytes = gzip.decompress(scene_path.read_bytes())
    scene = json.loads(scene_bytes)
    element = next((item["element"] for item in scene.get("sources", [])
                    if item.get("element")), None)
    if element is None:
        item["edits"] = {"status": "no-editable-element"}
        return item
    edits_path = work / f"{source.stem}.edits.json"
    edits_path.write_text(json.dumps({
        "formatVersion": 1,
        "scene": hashlib.sha256(scene_bytes).hexdigest(),
        "edits": [{"id": "issue-263-note", "kind": "note", "element": element,
                   "text": "機能行列の編集経路を確認"}],
    }, ensure_ascii=False), encoding="utf-8", newline="\n")
    notes: list[dict] = []
    apply_edits_file(scene_path, edits_path, source, source, notes=notes)
    conflict = check_generation_conflict(scene_path, edits_path, source)
    edited_output = work / f"{source.stem}.edited.{matrix['format']}"
    edited_run = pipeline.make(source, output=edited_output, emit=["blueprint"])
    edited_signature = (performance.signature(edited_output)
                        if edited_run.code == 0 and edited_output.is_file() else None)
    matches = ({field: edited_signature[field] == item["signature"][field]
                for field in SIGNATURE_FIELDS} if edited_signature else
               dict.fromkeys(SIGNATURE_FIELDS, False))
    item["edits"] = {"status": "passed" if edited_run.code == 0 and notes
                     and all(matches.values()) and conflict["status"] == "rejected"
                     else "failed", "code": edited_run.code,
                     "notes": notes, "signature": edited_signature, "matches": matches,
                     "generationConflict": conflict}
    return item


def check_generation_conflict(scene_path: Path, edits_path: Path, source: Path) -> dict:
    original = source.read_bytes()
    source.write_bytes(original + b"\n")
    try:
        apply_edits_file(scene_path, edits_path, source, source)
    except BlueprintError as exc:
        reason = performance.benchmark._record_text(str(exc))
        if str(exc) == "edits: scene生成後に入力が変更されています":
            return {"status": "rejected", "case": "changed-input",
                    "reasonCode": "input-generation-mismatch",
                    "errorType": type(exc).__name__, "reason": reason}
        return {"status": "failed", "case": "changed-input",
                "errorType": type(exc).__name__, "reason": reason}
    except Exception as exc:
        return {"status": "error", "case": "changed-input",
                "errorType": type(exc).__name__,
                "reason": performance.benchmark._record_text(str(exc))}
    else:
        return {"status": "unexpectedly-accepted", "case": "changed-input"}
    finally:
        source.write_bytes(original)


def matrix_spec(case: dict, matrix: dict, plan: dict, frozen: dict) -> tuple[dict, str, str]:
    spec = copy.deepcopy(case["buildSpec"])
    spec.update({"scale": matrix["scale"], "size": plan["scales"][matrix["scale"]],
                 "seed": matrix["seed"]})
    input_hash = hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")).hexdigest()
    if input_hash != frozen["sha256"]:
        raise ValueError(f"固定BuildSpecのhashが一致しません: {matrix}")
    spec.update({"minecraftVersion": matrix["version"]})
    config = spec.setdefault("config", {})
    config["preview"] = {**config.get("preview", {}), "enabled": False}
    config["output"] = {**config.get("output", {}), "format": matrix["format"]}
    spec.setdefault("overrides", [])
    spec.setdefault("elements", {})
    with tempfile.TemporaryDirectory(prefix="terrain-large-spec-") as temporary:
        source = Path(temporary) / "spec.yaml"
        source.write_bytes(encoded_spec(spec))
        spec = load_buildspec(source)
    actual_hash = hashlib.sha256(encoded_spec(spec)).hexdigest()
    return spec, input_hash, actual_hash


def composed_config_hash(spec: dict) -> str:
    with tempfile.TemporaryDirectory(prefix="terrain-large-config-") as temporary:
        source = Path(temporary) / "spec.yaml"
        source.write_bytes(encoded_spec(spec))
        resolved = resolve_buildspec(source)
    return pipeline._hash(resolved.config)


def successful(result: dict) -> bool:
    edits = result.get("edits", {})
    return (result.get("makeCode") == 0 and result.get("runStatus") == "passed"
            and result.get("buildCode") == 0 and result.get("buildMatches") is True
            and result.get("configMatches") is True
            and signature_complete(result.get("signature"))
            and edits.get("status") == "passed" and edits.get("code") == 0
            and signature_complete(edits.get("signature"))
            and edits.get("matches") == dict.fromkeys(SIGNATURE_FIELDS, True)
            and edits.get("generationConflict", {}).get("status") == "rejected"
            and edits.get("generationConflict", {}).get("reasonCode") ==
            "input-generation-mismatch"
            and len(result.get("resume", {})) == len(pipeline.STAGES)
            and all(value.get("code") == 0 and value.get("status") is None
                    and signature_complete(value.get("signature"))
                    and value.get("matches") == dict.fromkeys(SIGNATURE_FIELDS, True)
                    for value in result.get("resume", {}).values()))


def signature_complete(signature: dict | None) -> bool:
    return (isinstance(signature, dict)
            and all(isinstance(signature.get(field), str)
                    and len(signature[field]) == 64 for field in SIGNATURE_FIELDS))


def identity(result: dict) -> tuple:
    return (result.get("case"), result.get("scale"), result.get("seed"),
            result.get("version", result.get("minecraftVersion")), result.get("format"),
            result.get("inputSha256", result.get("frozenInputSha256")))


def reusable(rows: list[dict], matrix: dict, input_hash: str, actual_hash: str,
             contract: dict) -> dict | None:
    wanted = (*identity({**matrix, "inputSha256": input_hash}),)
    return next((row for row in rows if identity(row) == wanted
                 and row.get("frozenInputSha256") == input_hash
                 and row.get("actualInputSha256") == actual_hash
                 and row.get("composedConfigSha256") == contract["composedConfigSha256"]
                 and row.get("runnerSha256") == contract["runnerSha256"]
                 and row.get("provenance") == contract["provenance"]
                 and row.get("runtime") == contract["runtime"]
                 and row.get("evaluationScriptsSha256") == contract["evaluationScriptsSha256"]
                 and row.get("implementation") == contract["implementation"]
                 and successful(row)), None)


def deduplicate(rows: list[dict], contract: dict,
                config_hashes: dict[tuple, str] | None = None) -> list[dict]:
    unique = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = identity(row)
        current = (row.get("runnerSha256") == contract["runnerSha256"]
                   and row.get("provenance") == contract["provenance"]
                   and row.get("runtime") == contract["runtime"]
                   and row.get("implementation") == contract["implementation"]
                   and row.get("evaluationScriptsSha256") == contract["evaluationScriptsSha256"])
        config_current = (config_hashes is not None
                          and row.get("composedConfigSha256") == config_hashes.get(key))
        rank = (current, config_current, successful(row), bool(row.get("actualInputSha256")))
        prior = unique.get(key)
        if prior is None or rank > prior[0]:
            unique[key] = (rank, row)
    return [row for _, row in unique.values()]


def completion_passed(rows: list[dict], matrices: list[dict], contract: dict) -> bool:
    if len(rows) != len(matrices):
        return False
    by_identity = {identity(row): row for row in rows}
    for job in matrices:
        matrix = job["matrix"]
        row = by_identity.get(identity({**matrix, "inputSha256": job["inputSha256"]}))
        job_contract = {**contract,
                        "composedConfigSha256": job.get("composedConfigSha256")}
        if row is None or reusable([row], matrix, job["inputSha256"],
                                   job["actualInputSha256"], job_contract) is None:
            return False
    return True


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--colorpack", type=Path, help="実行時に使う配色パック")
    parser.add_argument("--record", type=Path, default=RECORD,
                        help="結果記録の保存先（配色別の記録にも指定）")
    parser.add_argument("--case", help="固定case idで絞り込む")
    parser.add_argument("--limit", type=int, help="先頭から指定件数だけ実行する")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit は1以上を指定してください")

    plan = json.loads(PLAN.read_bytes())
    cases = {case["id"]: case for case in plan["cases"]}
    if args.case and args.case not in cases:
        parser.error(f"未知のcase idです: {args.case}")
    record_path = args.record if args.record.is_absolute() else ROOT / args.record
    record_path = record_path.resolve()
    palette = args.colorpack.resolve() if args.colorpack else None
    runner_hash = digest(Path(__file__).resolve())
    provenance = performance.benchmark._benchmark_provenance()
    runtime = runtime_provenance()

    frozen = {(item["caseId"], item["scale"], item["seed"]): item
              for item in plan["inputs"]}
    all_jobs = []
    for case_id, case in cases.items():
        for scale in ("medium", "huge"):
            for seed in SEEDS:
                entry = frozen[(case_id, scale, seed)]
                for version in VERSIONS:
                    for fmt in FORMATS:
                        matrix = {"case": case_id, "scale": scale, "seed": seed,
                                  "version": version, "format": fmt}
                        spec, input_hash, actual_hash = matrix_spec(case, matrix, plan, entry)
                        config_hash = composed_config_hash(spec)
                        all_jobs.append({
                            "matrix": matrix, "spec": spec, "inputSha256": input_hash,
                            "actualInputSha256": actual_hash,
                            "frozenInputSha256": input_hash,
                            "runnerSha256": runner_hash, "provenance": provenance,
                            "runtime": runtime,
                            "composedConfigSha256": config_hash,
                        })

    selected = [job for job in all_jobs if not args.case or job["matrix"]["case"] == args.case]
    if args.limit is not None:
        selected = selected[:args.limit]
    script_hashes = evaluation_scripts_hashes()
    contract = {"runnerSha256": runner_hash, "provenance": provenance, "runtime": runtime,
                "evaluationScriptsSha256": script_hashes}
    config_hashes = {
        identity({**job["matrix"], "inputSha256": job["inputSha256"]}):
        job["composedConfigSha256"] for job in all_jobs
    }

    with record_lock(record_path):
        if record_path.exists():
            record = json.loads(record_path.read_bytes())
        else:
            record = make_record(plan, palette, record_path)
        if record.get("planSha256") != digest(PLAN):
            raise ValueError("固定ケースが記録と異なります。別の記録先を指定してください")
        expected_pack = ({"kind": "override", "path": palette.name,
                          "sha256": digest(palette)} if palette else {"kind": "default"})
        if record.get("colorpack") != expected_pack:
            raise ValueError("配色設定と記録が異なります。配色別の--recordを指定してください")
        record_reference = performance.benchmark._record_path(record_path)
        if record.get("evaluationScriptsSha256") not in (None, script_hashes):
            record["results"] = []
        record.update({
            "recordPath": record_reference,
            "scriptSha256": runner_hash,
            "benchmarkScriptSha256": digest(ROOT / "scripts/evaluate_make_performance.py"),
            "evaluationScriptsSha256": script_hashes,
            "provenance": provenance,
            "runtime": runtime,
        })

        with tempfile.TemporaryDirectory(prefix="terrain-large-pack-") as temporary:
            if palette:
                install_colorpack(palette, Path(temporary) / "pack")
            loaded = colorpack.load_colorpack()
            implementation = pipeline._environment(colorpack=loaded)
        contract["implementation"] = implementation
        record["completionPassed"] = completion_passed(
            deduplicate(record.get("results", []), contract), all_jobs, contract)

        reuse_sources = [record_path, *REUSE_RECORDS]
        rows = []
        audit = []
        for path in reuse_sources:
            if not path.is_file():
                audit.append({"path": performance.benchmark._record_path(path),
                              "status": "missing"})
                continue
            source_record = json.loads(path.read_bytes())
            source_rows = [row for row in source_record.get("results", []) if isinstance(row, dict)]
            rows.extend(source_rows)
            audit.append({"path": performance.benchmark._record_path(path),
                          "provenance": source_record.get("provenance"),
                          "runnerSha256": source_record.get("scriptSha256"),
                          "runtime": source_record.get("runtime"), "rows": len(source_rows)})

        jobs = []
        reused = []
        for job in selected:
            job_contract = {**contract, "composedConfigSha256": job["composedConfigSha256"]}
            match = reusable(rows, job["matrix"], job["inputSha256"],
                             job["actualInputSha256"], job_contract)
            if match:
                reused.append(job["matrix"])
                if match not in record.get("results", []):
                    record.setdefault("results", []).append(match)
            else:
                jobs.append(job)
        record["reuseAudit"] = {
            "implementation": implementation, "sources": audit,
            "reusedCount": len(reused), "reused": reused,
            "matrixCount": len(all_jobs), "selectedCount": len(selected),
            "remainingCount": len(jobs),
        }
        record["results"] = deduplicate(record.get("results", []), contract, config_hashes)
        record["completionPassed"] = completion_passed(record["results"], all_jobs, contract)
        write_json(record_path, record)

        run_success = True
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {}
            for job in jobs:
                work = {**job, "colorpack": str(palette) if palette else None,
                        "contract": {**contract,
                                     "composedConfigSha256": job["composedConfigSha256"]}}
                futures[pool.submit(run_job, work)] = job
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {**job["matrix"], "frozenInputSha256": job["inputSha256"],
                              "actualInputSha256": job["actualInputSha256"],
                              **contract, "status": "error",
                              "failureReason": f"{type(exc).__name__}: {exc}"}
                result["inputSha256"] = job["inputSha256"]
                record["results"] = [row for row in record["results"]
                                      if identity(row) != identity(result)]
                record["results"].append(result)
                record["results"] = deduplicate(record["results"], contract, config_hashes)
                record["measuredAt"] = datetime.now(UTC).isoformat()
                write_json(record_path, record)
                passed = successful(result)
                run_success &= passed
                message = ("passed" if passed else
                           performance.benchmark._record_text(result.get("failureReason", "")))
                print(job["matrix"], message, flush=True)

        record["results"] = deduplicate(record["results"], contract, config_hashes)
        record["completionPassed"] = completion_passed(record["results"], all_jobs, contract)
        write_json(record_path, record)
        return 0 if run_success else 1


def run_job(job: dict) -> dict:
    matrix = job["matrix"]
    with tempfile.TemporaryDirectory(prefix="terrain-large-") as temporary:
        work = Path(temporary)
        if job["colorpack"]:
            install_colorpack(Path(job["colorpack"]), work / "pack")
        try:
            result = run_case(job, job["spec"], matrix, work)
            if successful(result):
                return result
            reason = result.get("failureReason", "make/build/resume/edits検証が不合格です")
        except Exception as exc:
            result = {**matrix, "frozenInputSha256": job["inputSha256"],
                      "actualInputSha256": job["actualInputSha256"], **job["contract"],
                      "status": "error", "failureReason": f"{type(exc).__name__}: {exc}"}
            reason = result["failureReason"]
        target = FAILURES / (
            f"{matrix['case']}-{matrix['scale']}-s{matrix['seed']}-"
            f"{matrix['version']}-{matrix['format']}-{job['inputSha256'][:10]}-"
            f"{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, target)
        artifact_errors = sanitize_artifacts(target)
        result["failureReason"] = reason
        result["failureArtifacts"] = target.relative_to(ROOT).as_posix()
        if artifact_errors:
            result["artifactWarnings"] = [
                f"{name}: 匿名化できないためartifactから除外" for name in artifact_errors
            ]
        return result


def main_cli() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main_cli()
