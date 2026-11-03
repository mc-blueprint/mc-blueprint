from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

from mcblueprint import blockdata, preview, schema
from mcblueprint.cli import main
from mcblueprint.loader import read_blueprint_json
from mcblueprint.model.block import BlockState
from mcblueprint.validator import validate

spec = importlib.util.spec_from_file_location(
    "mcblueprint_benchmark", Path(__file__).resolve().parents[1] / "scripts" / "benchmark.py"
)
assert spec is not None and spec.loader is not None
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
STAGES, _cases, _worker = benchmark.STAGES, benchmark._cases, benchmark._worker


def test_benchmark_fixtures_cover_fixed_workloads_and_validate() -> None:
    cases = _cases()
    assert len(cases) == 12
    assert {case["kind"] for case in cases} == {
        "sparse", "dense_terrain", "air_region", "repeated_operation"
    }
    for case in cases:
        path = Path(__file__).parents[1] / "benchmarks" / "fixtures" / f"{case['name']}.json"
        assert not validate(read_blueprint_json(path))


def test_generation_profiles_are_fixed_valid_and_deterministic(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    record_path = root / "benchmarks" / "generation-profiles.json"
    record_before = record_path.read_bytes()
    record = json.loads(record_before)
    profiles = record["profiles"]
    assert {profile["kind"] for profile in profiles} == {
        "castle",
        "town",
        "terrain",
        "underground",
        "vegetation",
    }
    assert all(profile["dimensions"] == [64, 48, 64] for profile in profiles[:-1])
    assert profiles[-1]["kind"] == "castle"
    assert profiles[-1]["dimensions"] == [256, 128, 256]
    before = {
        profile["name"]: (root / "benchmarks" / "fixtures" / f"{profile['name']}.json").read_bytes()
        for profile in profiles
    }
    for profile in profiles:
        path = root / "benchmarks" / "fixtures" / f"{profile['name']}.json"
        raw = read_blueprint_json(path)
        blueprint = benchmark.load_blueprint_dict(raw)
        assert not validate(raw)
        assert benchmark._bounds(blueprint).size.to_list() == profile["dimensions"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == profile["input_sha256"]

    generator_spec = importlib.util.spec_from_file_location(
        "benchmark_profiles",
        root / "scripts" / "generate_benchmark_profiles.py",
    )
    assert generator_spec is not None and generator_spec.loader is not None
    generator = importlib.util.module_from_spec(generator_spec)
    generator_spec.loader.exec_module(generator)
    generated_dir = tmp_path / "generated"
    generator.generate_profiles(generated_dir)

    assert (generated_dir / "generation-profiles.json").read_bytes() == record_before.replace(
        b"\r\n", b"\n"
    )
    assert {
        profile["name"]: (generated_dir / "fixtures" / f"{profile['name']}.json").read_bytes()
        for profile in profiles
    } == before


def test_benchmark_stage_reports_elapsed_time_and_python_peak() -> None:
    result = _worker("repeat-8", "bounds_expansion", repetitions=1)
    assert result["seconds_median"] >= 0
    assert result["python_peak_bytes"] > 0
    assert len(result["samples_seconds"]) == 1


def test_process_peak_rss_includes_python_external_allocation() -> None:
    baseline = benchmark._process_peak_rss()
    assert baseline is not None
    allocation = bytearray(128 * 1024 * 1024)
    allocation[::4096] = b"x" * (len(allocation) // 4096)

    peak = benchmark._process_peak_rss()

    assert peak is not None
    assert peak >= baseline + 24 * 1024 * 1024
    assert allocation[0] == ord("x")


def test_benchmark_median_uses_midpoint_for_even_sample_count(monkeypatch) -> None:
    samples = iter(((1.0, None), (3.0, None), (0.0, 42)))
    monkeypatch.setattr(benchmark, "_measure", lambda *_args, **_kwargs: next(samples))

    result = _worker("repeat-8", "bounds_expansion", repetitions=2)

    assert result["seconds_median"] == 2.0
    assert result["samples_seconds"] == [1.0, 3.0]


def test_benchmark_reset_clears_all_runtime_caches() -> None:
    blockdata.supported_versions()
    blockdata.load_block_data(blockdata.supported_versions()[0])
    preview.block_color(BlockState.parse("water"))
    preview.block_color(BlockState.parse("chest"))
    schema.load_schema()
    schema.load_component_schema()

    benchmark._reset_caches()

    assert blockdata._versions.cache_info().currsize == 0
    assert blockdata.load_block_data.cache_info().currsize == 0
    assert blockdata._read_block_data.cache_info().currsize == 0
    assert preview._block_color.cache_info().currsize == 0
    assert preview._material_colors.cache_info().currsize == 0
    assert schema.load_schema.cache_info().currsize == 0
    assert schema.load_component_schema.cache_info().currsize == 0


def test_benchmark_baseline_matches_fixtures_and_stage_contract() -> None:
    root = Path(__file__).parents[1]
    baseline = json.loads((root / "benchmarks" / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["environment"]["repetitions"] == 3
    results = {result["name"]: result for result in baseline["results"]}
    assert set(results) == {case["name"] for case in _cases()}
    for case in _cases():
        result = results[case["name"]]
        fixture = root / "benchmarks" / "fixtures" / f"{case['name']}.json"
        assert hashlib.sha256(fixture.read_bytes()).hexdigest() == result["input_sha256"]
        assert result["cells"] > 0
        assert result["bounds_volume"] >= result["cells"]
        assert result["output_bytes"] > 0
        assert set(result["stages"]) == set(STAGES)
        for stage in result["stages"].values():
            assert len(stage["samples_seconds"]) == 3
            assert stage["python_peak_bytes"] > 0


def test_issue_153_generation_records_cover_fixed_workloads_and_integrations() -> None:
    root = Path(__file__).parents[1]
    old_record = json.loads((root / "benchmarks" / "generation-baseline.json").read_text(
        encoding="utf-8"
    ))
    core = json.loads((root / "benchmarks" / "generation-core-baseline.json").read_text(
        encoding="utf-8"
    ))
    make = json.loads((root / "benchmarks" / "make-baseline.json").read_text(encoding="utf-8"))
    evaluation = json.loads((root / "evaluation/records/generation-core.json").read_text(
        encoding="utf-8"
    ))

    assert len(old_record["results"]) == 6
    assert len(core["results"]) == 6
    assert all(
        core["provenance"].get(key)
        for key in ("commit", "engine_sha256", "lock_sha256", "default_config_sha256")
    )
    for result in core["results"]:
        assert result["repetitions"] == 3
        assert len(result["samples"]) == 3
        assert all(sample["process_peak_rss_bytes"] for sample in result["samples"])
    assert len(make["results"]) == 6
    for result in make["results"]:
        assert result["repetitions"] == 3
        assert len(result["samples"]) == 3
        assert result["process_peak_rss_bytes_median"] > 0
        assert all(
            len(sample["input_sha256"]) == 64
            and len(sample["config_sha256"]) == 64
            and sample["seconds"] >= sample["make_runtime_seconds"]
            for sample in result["samples"]
        )
        assert all(
            stage["rss_at_stage_start_bytes"] > 0
            for run in result["samples"]
            for stage in run["stage_hook_samples"]
        )
    assert len(evaluation["results"]) == 16
    assert all(result["placement_identical"] for result in evaluation["results"])
    assert all(
        requirement["result"] == "success"
        for result in evaluation["results"]
        for route in ("buildspec_requirements", "blueprint_requirements")
        for requirement in result[route]
    )
    regressions = evaluation["regression_checks"]
    assert regressions["seed_reproducible"]
    assert regressions["seed_variation_changes_placement"]
    assert regressions["core_sizes_16_32_64_identical"]
    assert all(regressions["resume_each_stage_identical"].values())
    assert all(item["validation"] == "success" for item in regressions["legacy_examples"])
    assert len(regressions["legacy_benchmark_fixtures"]) == 12
    assert all(
        item["validation"] == "success" and item["generated_cells"] > 0
        for item in regressions["legacy_benchmark_fixtures"]
    )


def test_benchmark_records_cli_regeneration_and_single_pass() -> None:
    for stage in ("cli_validate", "cli_preview", "cli_build", "single_pass"):
        result = _worker("terrain-8", stage, repetitions=1)
        assert result["seconds_median"] >= 0
        assert result["python_peak_bytes"] > 0


def test_single_pass_preview_matches_cli_origin_mark(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    fixture = root / "benchmarks" / "fixtures" / "sparse-16.json"
    cli_dir = tmp_path / "cli"
    single_dir = tmp_path / "single"

    assert main(
        ["preview", str(fixture), "--views", "top", "--scale", "1", "-o", str(cli_dir)]
    ) == 0
    benchmark._single_pass(fixture, single_dir)

    assert (cli_dir / "sparse-16-top.png").read_bytes() == (
        single_dir / "sparse-16-top.png"
    ).read_bytes()
