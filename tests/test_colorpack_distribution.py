"""配布検証の支持条件指定と、実sdistの収録範囲の引継ぎを確認する。"""

import hashlib
import importlib.util
import json
import socket
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mcblueprint import blockdata, colorpack, pipeline

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests/data/colorpack"
spec = importlib.util.spec_from_file_location(
    "verify_colorpack_distribution", ROOT / "scripts/verify_colorpack_distribution.py")
distribution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distribution)


@pytest.mark.parametrize("axes", [["color"], ["material"]])
def test_smoke_uses_planned_modern_frame_without_wall_prior(tmp_path, monkeypatch, axes):
    raw = json.loads((DATA / "modern-only.json").read_bytes())
    pack = colorpack.validate_colorpack(raw)
    assert pack.palette_prior[0]["when"] == {"style": "modern"}
    assert set(pack.palette_prior[0]["roles"]) == {"frame"}
    digest = hashlib.sha256((DATA / "modern-only.json").read_bytes()).hexdigest()
    plan = tmp_path / "plan.json"
    planned = json.loads((DATA / "distribution-plan.json").read_bytes())
    for case in planned["conditions"]:
        case["axes"] = axes
    plan.write_text(json.dumps(planned), encoding="utf-8", newline="\n")
    monkeypatch.setattr(colorpack, "load_colorpack", lambda: SimpleNamespace(pack_sha256=digest))
    monkeypatch.setattr(blockdata, "supported_versions", lambda: ["1.21.11"])
    monkeypatch.setattr(sys, "argv", ["-c", str(tmp_path), digest, str(plan)])
    monkeypatch.setattr(sys, "prefix", str(ROOT))
    monkeypatch.setattr(socket.socket, "connect", socket.socket.connect)
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    observed = []

    def make(source, *, output, overrides):
        document = yaml.safe_load(source.read_bytes())
        assert document["style"] == "modern"
        assert document["site"]["biome"] == "plains"
        assert "palette.accent=1" in overrides
        observed.append(document["archetype"])
        output.write_bytes(b"generated")
        record = output.with_suffix(".run.json")
        record.write_text(json.dumps({"colorpack": {
            "packSha256": digest, "selectionSources": {"frame": {
                "color": {"source": "pack"}, "material": {"source": "explicit"}}},
        }}), encoding="utf-8", newline="\n")
        return SimpleNamespace(code=0, record=record, output=output)

    monkeypatch.setattr(pipeline, "make", make)
    if axes == ["material"]:
        with pytest.raises(AssertionError, match="material"):
            exec(compile(distribution.SMOKE, "distribution-smoke", "exec"), {})
        return
    exec(compile(distribution.SMOKE, "distribution-smoke", "exec"), {})
    assert observed == ["house", "house", "tower", "tower"]
    result = json.loads((tmp_path / "smoke.json").read_bytes())
    assert all(row["condition"]["roles"] == ["frame"] for row in result)


def test_snapshot_uses_complete_backend_selection(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    output = tmp_path / "build"
    selected = ["pyproject.toml", ".gitignore", "docs/guide.md", "tests/test_case.py",
                "src/mcblueprint/__init__.py"]
    for name in [*selected, "ignored.txt"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + "\n", encoding="utf-8", newline="\n")

    def build(args, cwd, env):
        assert cwd == root
        assert args[1:5] == ["-m", "build", "--sdist", "--outdir"]
        directory = Path(args[-1])
        directory.mkdir(parents=True)
        with tarfile.open(directory / "mc_blueprint-0.1.0.tar.gz", "w:gz") as archive:
            for name in selected:
                archive.add(root / name, arcname="mc_blueprint-0.1.0/" + name)

    monkeypatch.setattr(distribution, "run", build)
    stage, members = distribution.snapshot_source(root, output, {})
    assert members == set(selected)
    assert not (stage / "ignored.txt").exists()
    assert all((stage / name).read_bytes() == (root / name).read_bytes() for name in selected)


def test_distribution_keeps_repository_evaluation_images():
    distribution.check_assets([
        "mc_blueprint-0.1.0/evaluation/artifacts/baseline/single-house/house-east.png",
        "mc_blueprint-0.1.0/evaluation/artifacts/baseline/standalone-tower/tower-top.png",
        "mc_blueprint-0.1.0/docs/example.jpg",
        "mcblueprint/data/colorpack/colorpack.json",
    ])


@pytest.mark.parametrize("name", [
    "mcblueprint/data/client.jar", "mc_blueprint-0.1.0/cache/CLIENT.JAR",
    "assets/minecraft/textures/block/stone.png",
    "mcblueprint/data/assets/1.21.11/assets/minecraft/textures/entity/chest/normal.png",
    "mc_blueprint-0.1.0/assets/minecraft/models/block/stone.json",
    "assets/minecraft/models/item/diamond_sword.json",
    "assets/minecraft/blockstates/stone.json",
])
def test_distribution_rejects_jars_and_mojang_assets(name):
    with pytest.raises(ValueError, match="jarまたはMojang資産"):
        distribution.check_assets([name])
