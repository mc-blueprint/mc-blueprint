"""viewとrenderで共有する入力準備と、固定した入力への編集適用。"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from mcblueprint import blockdata, components
from mcblueprint.buildspec import SUPPORTED_TERRAINS, _read_buildspec, resolve_buildspec
from mcblueprint.config import load_schema, validate_schema
from mcblueprint.edits import apply_edits
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.generator import GeneratedRegion, generate
from mcblueprint.importers import ImportedSchematic, read_schematic, to_blueprint
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.output_lock import lock_paths
from mcblueprint.pipeline import (
    _cli_overrides,
    _environment,
    _hash,
    generated_blueprint_regions,
    make,
)
from mcblueprint.planning import plan_buildspec
from mcblueprint.scene import capture_sources, write_scene
from mcblueprint.validator import validate_and_load
from mcblueprint.volume import BlockVolume, RegionVolume


class EditConflict(BlueprintError):
    """保存済み編集と現在の世代が一致しない。"""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_write(path: Path, raw: bytes) -> None:
    """同じディレクトリに候補を書き、既存ファイルを原子的に置換する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_scene(path: Path) -> tuple[bytes, dict]:
    """gzip sceneを展開し、v1 schemaと配列の整合を検証する。由来パスは開かない。"""
    raw = gzip.decompress(path.read_bytes())
    scene = json.loads(raw)
    validate_schema(scene, load_schema("scene.schema.json"), "scene")
    size = scene["size"]
    occupied = set()
    for chunk in scene["chunks"]:
        at = chunk["at"]
        key = tuple(at)
        if key in occupied or any(at[i] < 0 or at[i] % 16 or at[i] >= size[i] for i in range(3)):
            raise BlueprintError("sceneのchunk位置が不正です")
        occupied.add(key)
        count = int(np.prod([min(16, size[i] - at[i]) for i in range(3)]))
        indexes = np.frombuffer(base64.b64decode(chunk["blocks"], validate=True),
                                dtype="<u2" if scene["indexWidth"] == 16 else "<u4")
        sources = np.frombuffer(base64.b64decode(chunk["sources"], validate=True), dtype="<u4")
        ids = {0, *(entry["id"] for entry in scene["sources"])}
        if (len(indexes) != count or len(sources) != count
                or np.any(indexes >= len(scene["palette"]))
                or not set(map(int, sources)) <= ids):
            raise BlueprintError("sceneのchunk配列が不正です")
    return raw, scene


def _scene_blueprint(scene: dict, name: str) -> dict:
    volume = BlockVolume()
    palette = [BlockState.parse(entry["state"]) for entry in scene["palette"]]
    for chunk in scene["chunks"]:
        cx, cy, cz = chunk["at"]
        sx, sy, sz = [min(16, scene["size"][i] - chunk["at"][i]) for i in range(3)]
        indexes = np.frombuffer(base64.b64decode(chunk["blocks"]),
                                dtype="<u2" if scene["indexWidth"] == 16 else "<u4")
        for index, value in enumerate(indexes):
            if value:
                volume.set(Vec3(cx + index % sx, cy + index // (sx * sz),
                                cz + index // sx % sz), palette[int(value)])
    origin = Vec3(*scene["origin"])
    if len(volume):
        imported = ImportedSchematic(volume, origin, None)
        data = to_blueprint(imported, name=name, minecraft_version=scene["minecraftVersion"],
                            description=None)
    else:
        data = {"formatVersion": 1, "minecraftVersion": scene["minecraftVersion"],
                "name": name, "operations": []}
    high = [scene["origin"][i] + scene["size"][i] - 1 for i in range(3)]
    data["operations"][:0] = [
        {"type": "set", "position": pos, "block": "minecraft:air"}
        for pos in (origin.to_list(), high)
    ]
    return data


def _validated(data: dict, base: Path):
    with components.component_search_paths(components.default_search_paths(base / "input.json")):
        blueprint, errors = validate_and_load(data, base_dir=base)
    if errors:
        raise BlueprintError("\n".join(map(str, errors)))
    return blueprint


def _regions(blueprint):
    if (blueprint.metadata or {}).get("generation", {}).get("mode") == "buildspec-v1":
        yield from generated_blueprint_regions(blueprint)
    else:
        volume = generate(blueprint)
        if volume.bounds() is None:
            raise BlueprintError("表示するブロックがありません")
        bounds = blueprint.bounds()
        region = RegionVolume(bounds)
        for pos, state in volume:
            region.set(pos, state, volume.attributes(pos))
        yield GeneratedRegion(bounds, region)


def _write_blueprint_scene(data: dict, base: Path, target: Path, source: dict,
                           config: dict) -> None:
    blueprint = _validated(data, base)
    view = config["preview"]
    preview = {"enabled": view["enabled"], "shader": view["shader"],
               "shadows": view["shadows"], "brightness": view["brightness"],
               "timeOfDay": view["time_of_day"], "blockLight": view["block_light"],
               "quality": view["quality"]}
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        root = Path(temporary)
        bounds = blueprint.bounds()
        spool = RegionSpool(root / "blocks", bounds)
        table = {}
        paths = components.default_search_paths(base / "input.json")
        with components.component_search_paths(paths):
            spool.write(capture_sources(_regions(blueprint), bounds, root / "sources", {}, table))
        write_scene(target, root / "sources", spool, table, blueprint, {}, source,
                    preview, view["biome"] or "plains")


def _spec_sources(scene: dict, resolved) -> None:
    # 子要素しかセルを持たない場合も、編集対象の親要素をsource表へ残す。
    massing = plan_buildspec(resolved).massing
    root = massing["element"]
    if any(entry.get("element") == root for entry in scene["sources"]):
        return
    entry = {"element": root, "label": root, "params": sorted(massing["params"])}
    ident = int.from_bytes(hashlib.sha256(_json(entry)).digest()[:4], "little") or 1
    if any(entry["id"] == ident for entry in scene["sources"]):
        raise BlueprintError("生成元IDが衝突しました")
    scene["sources"].append({"id": ident, **entry})


@dataclass
class ViewerInput:
    """起動時に固定したパスと世代。spec以外は現在のsceneから派生Blueprintを作る。

    regenerateは入力・成果のロックを保持して候補を生成し、公開失敗時は元へ戻す。
    複数ファイルの置換に電源断までのトランザクション保証はない。
    """

    input: Path
    output: Path
    scene_path: Path
    edits_path: Path
    derived_path: Path
    config: dict
    overrides: list[str]
    spec: bool
    original: bytes
    raw_scene: bytes
    scene: dict
    meta: dict

    @property
    def scene_hash(self) -> str:
        return _sha(self.raw_scene)

    def check_generation(self) -> None:
        if self.input.read_bytes() != self.original:
            raise EditConflict("scene生成後に入力が変更されています")
        if _sha(gzip.decompress(self.scene_path.read_bytes())) != self.scene_hash:
            raise EditConflict("sceneが外部で変更されています")
        if self.meta.get("environment") != _environment():
            raise EditConflict("engineまたはdataの世代が一致しません")
        if self.spec:
            meta_path = self.scene_path.with_suffix(self.scene_path.suffix + ".meta.json")
            resolved = resolve_buildspec(self.input, overrides=self.overrides)
            if (json.loads(meta_path.read_bytes()) != self.meta
                    or self.meta.get("inputSha256") != _sha(self.original)
                    or self.meta.get("sceneSha256") != self.scene_hash
                    or self.meta.get("resolvedHash") != _hash(resolved.buildspec)
                    or self.meta.get("configHash") != _hash(resolved.config)):
                raise EditConflict("入力・設定・engine・dataの世代が一致しません")

    def candidate(self, edits: dict, *, notes: list[dict] | None = None) -> dict:
        validate_schema(edits, load_schema("edits.schema.json"), "edits")
        if edits["scene"].lower() != self.scene_hash:
            raise EditConflict("scene hashが一致しません")
        if not self.spec and any(e["kind"] not in ("replace", "delete") for e in edits["edits"]):
            raise BlueprintError("非spec入力ではreplace/deleteだけを使用できます")
        if self.spec:
            source = _read_buildspec(self.input)
            source.setdefault("formatVersion", 1)
            source.setdefault("archetype", "house")
        else:
            source = _scene_blueprint(self.scene, self.derived_path.stem)
        candidate = apply_edits(self.scene, edits, source, scene_bytes=self.raw_scene, notes=notes)
        if not self.spec:
            for override in candidate.pop("overrides", []):
                low, high = override["region"]
                candidate["operations"].append({"type": "fill", "from": low, "to": high,
                                                 "block": override["block"]})
            _validated(candidate, self.input.parent)
        return candidate

    def save_edits(self, edits: dict) -> dict:
        notes = []
        with lock_paths(self.input, self.output, self.edits_path):
            self.check_generation()
            self.candidate(edits, notes=notes)
            atomic_write(self.edits_path, _json(edits))
        return {"ok": True, "scene": self.scene_hash, "notes": notes}

    def regenerate(self) -> dict:
        with lock_paths(self.input, self.output, self.derived_path):
            self.check_generation()
            if not self.edits_path.is_file():
                raise EditConflict("editsが保存されていません")
            notes = []
            candidate = self.candidate(json.loads(self.edits_path.read_bytes()), notes=notes)
            self.output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.output.parent, prefix=".regenerate-") as tmp:
                root = Path(tmp)
                staged = root / self.output.name
                scene_path = root / self.scene_path.name
                changes = {}
                code, diagnostics = 0, []
                if self.spec:
                    encoded = yaml.safe_dump(candidate, allow_unicode=True,
                                             sort_keys=False).encode("utf-8")
                    generation_source = json.loads(json.dumps(candidate))
                    site = generation_source.get("site", {})
                    terrain = site.get("terrain", "none")
                    if terrain not in SUPPORTED_TERRAINS:
                        site["terrain"] = str((self.input.parent / terrain).resolve())
                    resolved = resolve_buildspec(generation_source, overrides=self.overrides)
                    source = root / self.input.name
                    source.write_text(yaml.safe_dump(resolved.buildspec, allow_unicode=True),
                                      encoding="utf-8", newline="\n")
                    result = make(source, output=staged)
                    code = result.code
                    diagnostics = [line.replace(str(source), str(self.input))
                                   for line in result.diagnostics]
                    generated_scene = staged.with_name(staged.stem + ".scene.json.gz")
                    _, scene = read_scene(generated_scene)
                    _spec_sources(scene, resolved)
                    scene["source"] = {"kind": "spec", "path": str(self.input),
                                       "sha256": _sha(encoded)}
                    raw = _json(scene)
                    scene_path.write_bytes(gzip.compress(raw, mtime=0))
                    meta = {"formatVersion": 1, "inputSha256": _sha(encoded),
                            "resolvedHash": _hash(resolved.buildspec),
                            "configHash": _hash(resolved.config), "environment": _environment(),
                            "cliOverrides": _cli_overrides(resolved),
                            "sceneSha256": _sha(raw)}
                    changes[self.input] = encoded
                    record = json.loads(result.record.read_bytes())
                    record["input"].update(
                        path=str(self.input), basePath=str(self.input.parent),
                        sha256=_sha(encoded), resolvedHash=_hash(resolved.buildspec),
                    )
                    record["output"] = str(self.output)
                    record["diagnostics"] = diagnostics
                    record["sources"] = {key: value.replace(str(source), str(self.input))
                                         for key, value in record["sources"].items()}
                    record_path = self.output.with_suffix(self.output.suffix + ".run.json")
                    changes[record_path] = _json(record)
                else:
                    encoded = _json(candidate)
                    changes[self.derived_path] = encoded
                    _write_blueprint_scene(candidate, self.input.parent, scene_path,
                                           {"kind": "blueprint", "path": str(self.derived_path),
                                            "sha256": _sha(encoded)}, self.config)
                    blueprint = _validated(candidate, self.input.parent)
                    EXPORTERS[self.config["output"]["format"]]().export(
                        generate(blueprint), blueprint, staged,
                    )
                    meta = {"formatVersion": 1, "inputSha256": _sha(self.original),
                            "environment": _environment()}
                raw, scene = read_scene(scene_path)
                meta["sceneSha256"] = _sha(raw)
                changes[self.output] = staged.read_bytes()
                changes[self.scene_path] = scene_path.read_bytes()
                meta_path = self.scene_path.with_suffix(self.scene_path.suffix + ".meta.json")
                changes[meta_path] = _json(meta)
                self.check_generation()
                backups = {p: p.read_bytes() if p.exists() else None for p in changes}
                published = []
                try:
                    for path, content in changes.items():
                        atomic_write(path, content)
                        published.append(path)
                except BaseException:
                    for path in reversed(published):
                        if backups[path] is None:
                            path.unlink(missing_ok=True)
                        else:
                            atomic_write(path, backups[path])
                    raise
                self.original = self.input.read_bytes()
                self.raw_scene, self.scene, self.meta = raw, scene, meta
                return {"ok": True, "code": code, "diagnostics": diagnostics,
                        "scene": self.scene_hash, "notes": notes}


def prepare_input(path: str | Path, *, output: str | Path | None = None,
                  overrides: list[str] | None = None,
                  minecraft_version: str | None = None) -> ViewerInput:
    """4種類の入力をsceneへ接続する。出力先に成果を保存し、ブラウザは起動しない。

    YAMLだけをspecとして扱い、scene内のsource.pathは信用して開かない。
    schematicの版は完全一致するDataVersionか明示値が必要。
    """
    path = Path(path).resolve()
    spec = path.suffix.lower() in (".yaml", ".yml")
    overrides = list(overrides or [])
    resolved = resolve_buildspec(path if spec else None, overrides=overrides)
    config = resolved.config
    original = path.read_bytes()
    fmt = config["output"]["format"]
    scene_input = path.name.endswith(".scene.json.gz")
    stem = path.name.removesuffix(".scene.json.gz") if scene_input else path.stem
    out = Path(output or config["output"]["directory"])
    if out.suffix != EXPORTERS[fmt].extension:
        out /= stem + EXPORTERS[fmt].extension
    out = out.resolve()
    scene_path = out.with_name(out.stem + ".scene.json.gz")
    derived_path = out.with_name(out.stem + ".edited.json")
    edits_path = out.with_name(out.stem + ".edits.json")
    if (path in (out, derived_path, edits_path)
            or (not scene_input and scene_path == path)):
        raise BlueprintError("入力と出力は別のパスにしてください")
    if spec:
        if minecraft_version and minecraft_version != resolved.buildspec["minecraftVersion"]:
            raise BlueprintError("既知のMinecraft版と指定値が矛盾しています")
        result = make(path, output=out, overrides=overrides)
        out = result.output.resolve()
        scene_path = out.with_name(out.stem + ".scene.json.gz")
    elif path.name.endswith(".scene.json.gz"):
        raw, scene = read_scene(path)
        if minecraft_version and minecraft_version != scene["minecraftVersion"]:
            raise BlueprintError("既知のMinecraft版と指定値が矛盾しています")
        if scene_path == path:
            scene_path = out.with_name(out.stem + ".view.scene.json.gz")
        with lock_paths(path, out, scene_path):
            for entry in scene["sources"]:
                entry.pop("params", None)
            atomic_write(scene_path, gzip.compress(_json(scene), mtime=0))
    else:
        if path.suffix.lower() == ".json":
            data = json.loads(original)
            if not isinstance(data, dict):
                raise BlueprintError("BlueprintにはJSON objectが必要です")
            version = data.get("minecraftVersion")
        elif path.suffix.lower() == ".schem":
            imported = read_schematic(path)
            version = blockdata.version_for_data_version(imported.data_version)
            if imported.data_version is not None and version is None:
                raise BlueprintError("未知のDataVersionです。近傍版への置換は行いません")
            version = version or minecraft_version
            if version is None:
                raise BlueprintError("版不明のschematicには--minecraft-versionが必要です")
            data = to_blueprint(imported, name=stem, minecraft_version=version, description=None)
        else:
            raise BlueprintError("入力はBuildSpec・Blueprint・.schem・.scene.json.gzが必要です")
        if minecraft_version and version != minecraft_version:
            raise BlueprintError("既知のMinecraft版と指定値が矛盾しています")
        with lock_paths(path, out):
            _write_blueprint_scene(data, path.parent, scene_path,
                                   {"kind": "blueprint" if path.suffix == ".json" else "schematic",
                                    "path": str(path), "sha256": _sha(original)}, config)
    raw, scene = read_scene(scene_path)
    meta_path = scene_path.with_suffix(scene_path.suffix + ".meta.json")
    meta = json.loads(meta_path.read_bytes()) if spec else {"environment": _environment()}
    return ViewerInput(path, out, scene_path, edits_path, derived_path, config, overrides,
                       spec, original, raw, scene, meta)


def apply_input_edits(edits_path: Path, source: Path, scene_path: Path, *,
                       output: Path | None = None, spec: bool = False,
                       notes: list[dict] | None = None,
                       overrides: list[str] | None = None) -> Path:
    """再生成せず編集候補だけを保存する。specは既定で原本、非specは派生JSON。

    overridesはspec生成時のkey=value指定を再現する。非specへの指定は拒否する。
    """
    from mcblueprint.edits import apply_edits_file

    if spec:
        if source.suffix.lower() not in (".yaml", ".yml"):
            raise BlueprintError("--specにはBuildSpec YAMLが必要です")
        return apply_edits_file(scene_path, edits_path, source, output or source, notes=notes,
                                overrides=overrides)
    if overrides:
        raise BlueprintError("--set・--seedは--specと組み合わせて指定してください")
    if output is not None and output.resolve() in (source.resolve(), scene_path.resolve()):
        raise BlueprintError("非spec入力とsceneは上書きできません")
    raw, scene = read_scene(scene_path)
    stem = source.name.removesuffix(".scene.json.gz") if source.name.endswith(
        ".scene.json.gz") else source.stem
    target = output or source.with_name(stem + ".edited.json")
    original = source.read_bytes()
    if not source.name.endswith(".scene.json.gz") and scene["source"]["sha256"] != _sha(original):
        raise EditConflict("sceneと指定入力の世代が一致しません")
    session = ViewerInput(source, target, scene_path, edits_path, target, {}, [], False,
                          original, raw, scene, {"environment": _environment()})
    with lock_paths(source, target):
        session.check_generation()
        candidate = session.candidate(json.loads(edits_path.read_bytes()))
        session.check_generation()
        atomic_write(target, _json(candidate))
    return target
