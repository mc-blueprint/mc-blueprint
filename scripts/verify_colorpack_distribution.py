"""実sdistの一時コピーへ指定パックを注入し、配布物とオフラインmakeを検証する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("data/colorpack/colorpack.json", "data/colorpack/DATA_LICENSE",
            "data/colorpack-vocabulary.json", "data/lock.json")

# 別プロセス・非editable環境で、socket通信そのものも拒否する。
SMOKE = '''
import json, socket, sys
from pathlib import Path
from importlib.resources import files
from mcblueprint.colorpack import load_colorpack
from mcblueprint.pipeline import make
import yaml
def offline(*args, **kwargs):
    raise AssertionError("オフライン検証中に通信が要求されました")
socket.socket.connect = offline
socket.create_connection = offline
pack = load_colorpack()
assert pack is not None and pack.pack_sha256 == sys.argv[2]
assert Path(str(files("mcblueprint"))).resolve().is_relative_to(Path(sys.prefix).resolve())
results = []
plan = json.loads(Path(sys.argv[3]).read_bytes())
from mcblueprint.blockdata import supported_versions
for version in supported_versions():
    for index, case in enumerate(plan["conditions"]):
        for fmt in ("schem", "litematic"):
            name = f"{index}-{case['buildingType']}-{version}-{fmt}"
            source = Path(sys.argv[1]) / f"{name}.yaml"
            source.write_text(yaml.safe_dump({
                "minecraftVersion": version, "archetype": case["buildingType"],
                "style": case["style"], "site": {"biome": case["biome"]},
                "scale": "tiny", "seed": 0,
                "config": {"preview": {"enabled": False}, "weathering": {"age": 0}},
            }), encoding="utf-8", newline="\\n")
            result = make(source, output=source.with_suffix(f".{fmt}"),
                          overrides=[f"output.format={fmt}", "assets.download=false",
                                     "palette.accent=1"])
            assert result.code == 0, result
            record = json.loads(result.record.read_bytes())
            assert record["colorpack"]["packSha256"] == pack.pack_sha256
            selected = record["colorpack"]["selectionSources"]
            assert case["roles"], "pack使用を確認する役割が必要です"
            for role in case["roles"]:
                for axis in case.get("axes", ["color"]):
                    actual = selected.get(role, {}).get(axis, {}).get("source")
                    assert actual == "pack", (role, axis)
            assert result.output.is_file()
            results.append({"case": name, "condition": case, "code": result.code,
                            "selectionSources": {r: selected[r] for r in case["roles"]}})
Path(sys.argv[1], "smoke.json").write_text(json.dumps(results) + "\\n", encoding="utf-8",
                                        newline="\\n")
'''


def run(args, cwd, env=None):
    subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=True)


def snapshot_source(root, output, env):
    """実リポジトリのbackendが収録したsdistを、そのまま一時ソースにする。"""
    baseline = output / "baseline"
    run([sys.executable, "-m", "build", "--sdist", "--outdir", baseline], root, env)
    archive = next(baseline.glob("*.tar.gz"))
    with tarfile.open(archive) as opened:
        members = [member for member in opened.getmembers() if member.isfile()]
        prefix = members[0].name.split("/", 1)[0]
        included = {member.name.removeprefix(prefix + "/") for member in members}
        opened.extractall(output / "source", filter="data")
    return output / "source" / prefix, included


def check_assets(names):
    """jarとMinecraft名前空間の生資産を拒否し、リポジトリの評価画像は許す。"""
    for name in names:
        path = PurePosixPath(name.lower())
        parts = path.parts
        mojang_asset = any(
            parts[i:i + 2] == ("assets", "minecraft")
            and parts[i + 2] in ("textures", "models", "blockstates")
            for i in range(len(parts) - 2)
        )
        if path.suffix == ".jar" or mojang_asset:
            raise ValueError(f"配布物へjarまたはMojang資産が入りました: {name}")


def verify(output, pack, license_path, wheelhouse, venv_root, synthetic, plan_path):
    output.mkdir(parents=True, exist_ok=True)
    if (output / "source").exists() or (output / "baseline").exists():
        raise ValueError("未使用の配布検証先を指定してください")
    plan = json.loads(plan_path.read_bytes())
    if not plan["conditions"] or any(not case["roles"] for case in plan["conditions"]):
        raise ValueError("支持条件とpack使用を確認する役割を計画へ指定してください")
    for case in plan["conditions"]:
        axes = case.get("axes", ["color"])
        if not axes or any(axis not in ("color", "material") for axis in axes):
            raise ValueError("確認する配色軸はcolor/materialから1つ以上指定してください")
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(wheelhouse.resolve()), PIP_NO_CACHE_DIR="1")
    stage, baseline_members = snapshot_source(ROOT, output, env)
    smoke_plan = output / "smoke-plan.json"
    smoke_plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8", newline="\n")
    data = stage / "src/mcblueprint/data"
    (data / "colorpack").mkdir(exist_ok=True)
    shutil.copy2(pack, data / "colorpack/colorpack.json")
    shutil.copy2(license_path, data / "colorpack/DATA_LICENSE")
    raw = json.loads(pack.read_bytes())
    lock = json.loads((data / "lock.json").read_bytes())
    entry = {key: raw[key] for key in ("packVersion", "taxonomyVersion", "vocabularyVersion")}
    entry.update(repository=raw["provenance"]["generator"]["repository"],
                 commit=raw["provenance"]["generator"]["commit"],
                 sha256={key: hashlib.sha256((data.parent / name).read_bytes()).hexdigest()
                         for key, name in zip(("pack", "license", "vocabulary"), REQUIRED,
                                              strict=False)})
    lock["colorpack"] = entry
    (data / "lock.json").write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8", newline="\n")
    run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", output / "dist"],
        stage, env)
    artifacts = []
    for archive in sorted((output / "dist").iterdir()):
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as opened:
                contents = {name: opened.read(name) for name in opened.namelist()}
        else:
            with tarfile.open(archive) as opened:
                contents = {member.name: opened.extractfile(member).read()
                            for member in opened.getmembers() if member.isfile()}
            prefix = next(iter(contents)).split("/", 1)[0]
            included = {name.removeprefix(prefix + "/") for name in contents}
            expected = baseline_members | {"src/mcblueprint/" + name for name in REQUIRED[:2]}
            if included != expected:
                raise ValueError(f"実sdistの収録範囲が変わりました: {included ^ expected}")
        for name in REQUIRED:
            matches = [value for key, value in contents.items()
                       if key.endswith("mcblueprint/" + name)]
            if matches != [(data.parent / name).read_bytes()]:
                raise ValueError(f"配布物の内容が不一致: {archive.name}: {name}")
        check_assets(contents)
        artifacts.append({"path": archive.name,
                          "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
    for fmt in ("wheel", "sdist"):
        target = venv_root / fmt
        if target.exists():
            raise ValueError(f"クリーンvenvの保存先が既に存在します: {target}")
        venv.EnvBuilder(with_pip=True).create(target)
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        artifact = next((output / "dist").glob("*.whl" if fmt == "wheel" else "*.tar.gz"))
        run([python, "-m", "pip", "install", "--no-index", "--find-links", wheelhouse, artifact],
            output, env)
        smoke = output / f"{fmt}-smoke"
        smoke.mkdir()
        run([python, "-I", "-c", SMOKE, smoke, entry["sha256"]["pack"], smoke_plan], smoke, env)
    report = {"synthetic": synthetic, "gAcceptanceEvidence": False, "artifacts": artifacts,
              "required": REQUIRED, "offline": ["wheel", "sdist"], "plan": plan,
              "baselineSdistMembers": sorted(baseline_members)}
    (output / "distribution.json").write_text(json.dumps(report, indent=2) + "\n",
                                             encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="新しい検証用ディレクトリ")
    parser.add_argument("--pack", type=Path, required=True)
    parser.add_argument("--license", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True, help="確認する支持条件・役割の計画")
    parser.add_argument("--wheelhouse", type=Path, required=True,
                        help="事前取得した依存・build用wheel")
    parser.add_argument("--venv-root", type=Path, required=True,
                        help="リポジトリ外のクリーンvenv作成先")
    parser.add_argument("--synthetic", action="store_true", help="人工パックの検証と明記する")
    args = parser.parse_args()
    verify(args.output.resolve(), args.pack, args.license, args.wheelhouse.resolve(),
           args.venv_root.resolve(), args.synthetic, args.plan.resolve())
