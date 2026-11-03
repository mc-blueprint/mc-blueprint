"""viewer edits v1をBuildSpecまたは派生Blueprintへ適用する。"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mcblueprint.config import load_schema, validate_schema
from mcblueprint.errors import BlueprintError
from mcblueprint.output_lock import lock_paths


def _fail(message: str) -> BlueprintError:
    return BlueprintError(f"edits: {message}")


def _reserved_bounds(data: dict[str, Any], parents: list[dict] | None = None):
    from mcblueprint.operations.registry import build_operation

    parents = [] if parents is None else parents
    if data.get("reserved") in ("structure", "circulation"):
        wrapped = copy.deepcopy(data)
        for parent in reversed(parents):
            wrapper = copy.deepcopy(parent)
            wrapper["operations"] = [wrapped]
            wrapped = wrapper
        yield data["element"], data["reserved"], build_operation(wrapped, "edits").bounds()
    for child in data.get("operations", []):
        yield from _reserved_bounds(child, [*parents, data])


def apply_edits(scene: dict[str, Any], edits: dict[str, Any], source: dict[str, Any], *,
                scene_bytes: bytes | None = None,
                notes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """検証済みsceneとeditsをBuildSpecまたはBlueprintのコピーへ適用する。

    scene_bytesを渡す場合、展開済みscene JSONのSHA-256も一致確認する。
    入力dictは変更せず、BuildSpec候補はplanningで検証する。
    BuildSpecのoverride IDはscene hashの先頭8桁で名前空間付けし、同内容は再追加しない。
    noteは候補に書かず、成功時にnotesへ対象付きメモを追加する。
    """
    validate_schema(edits, load_schema("edits.schema.json"), "edits")
    if (scene_bytes is not None
            and hashlib.sha256(scene_bytes).hexdigest() != edits["scene"].lower()):
        raise _fail("scene hashが一致しません")
    if scene.get("formatVersion") != 1:
        raise _fail("scene v1が必要です")
    result = copy.deepcopy(source)
    spec = result.get("formatVersion") == 1 and "archetype" in result
    sources = {item["id"]: item for item in scene.get("sources", [])}
    size = scene.get("size", [])
    origin = scene.get("origin", [0, 0, 0])
    seen: dict[str, dict] = {}
    overrides = result.setdefault("overrides", [])
    elements = result.setdefault("elements", {}) if spec else None
    collected_notes = []
    for edit in edits["edits"]:
        ident = edit["id"]
        if ident in seen:
            if seen[ident] == edit:
                continue
            raise _fail(f"ID {ident!r} の内容が競合しています")
        seen[ident] = edit
        kind = edit["kind"]
        if kind in ("replace", "delete"):
            if kind == "replace":
                low, high = edit["region"]
                block = edit["block"]
            elif "at" in edit:
                low = high = edit["at"]
                block = "minecraft:air"
            else:
                low, high = edit["region"]
                block = "minecraft:air"
            if any(low[i] < 0 or high[i] < low[i] or high[i] >= size[i] for i in range(3)):
                raise _fail(f"ID {ident!r} の座標がscene範囲外です")
            local_low, local_high = list(low), list(high)
            world_low = [low[i] + origin[i] for i in range(3)]
            world_high = [high[i] + origin[i] for i in range(3)]
            entry = {"id": f"edit-{edits['scene'][:8].lower()}-{ident}" if spec else ident,
                     "region": [local_low, local_high] if spec
                     else [world_low, world_high], "block": block}
            if spec:
                previous = next((item for item in overrides if item["id"] == entry["id"]), None)
                if previous is not None:
                    if previous == entry:
                        continue
                    raise _fail(f"ID {entry['id']!r} の内容が競合しています")
            if not spec:
                if kind not in ("replace", "delete"):
                    raise _fail("非BuildSpec入力ではreplace/deleteのみ適用できます")
            overrides.append(entry)
        elif kind == "param":
            if not spec:
                raise _fail("非BuildSpec入力ではparamを適用できません")
            descriptor = next((item for item in sources.values()
                               if item.get("element") == edit["element"]), None)
            if descriptor is None or edit["param"] not in descriptor.get("params", []):
                raise _fail(f"ID {ident!r} の要素またはparamが許可されていません")
            elements.setdefault(edit["element"], {})[edit["param"]] = edit["value"]
        else:
            descriptor = next((item for item in sources.values()
                               if item.get("element") == edit["element"]), None)
            if descriptor is None:
                raise _fail(f"ID {ident!r} の要素がsceneにありません")
            collected_notes.append({"id": ident, "element": edit["element"], "text": edit["text"]})
    if spec:
        from mcblueprint.buildspec import resolve_buildspec
        from mcblueprint.planning import plan_buildspec

        planned = plan_buildspec(resolve_buildspec(result))
        for edit in edits["edits"]:
            if edit["kind"] not in ("replace", "delete"):
                continue
            low, high = edit.get("region", [edit.get("at"), edit.get("at")])
            target_low = [low[i] + origin[i] for i in range(3)]
            target_high = [high[i] + origin[i] for i in range(3)]
            reserved = []
            for operation_data in planned.blueprint["operations"]:
                reserved.extend(_reserved_bounds(operation_data))
            reserved.sort(key=lambda item: item[1] != "circulation")
            for element, reservation, bounds in reserved:
                bounds_min, bounds_max = bounds.min.to_list(), bounds.max.to_list()
                if all(target_low[i] <= bounds_max[i]
                       and bounds_min[i] <= target_high[i] for i in range(3)):
                    raise _fail(f"要素 {element} の予約 {reservation} と重なる編集です")
    if notes is not None:
        notes.extend(collected_notes)
    return result


def apply_edits_file(scene_path: Path, edits_path: Path, source_path: Path,
                     output_path: Path, *, notes: list[dict[str, Any]] | None = None,
                     overrides: list[str] | None = None) -> Path:
    """世代を検証して候補を保存する。成功時にnotesへ対象付きメモを追加する。

    overridesはmakeと同じkey=valueの指定。BuildSpecでは生成時のCLI指定と
    合成設定を照合し、省略・変更時はBlueprintErrorを送出して保存しない。
    """
    scene_path, edits_path, source_path, output_path = map(
        Path, (scene_path, edits_path, source_path, output_path),
    )
    with lock_paths(source_path, output_path):
        original = source_path.read_bytes()
        raw_scene = gzip.decompress(scene_path.read_bytes())
        scene = json.loads(raw_scene)
        edits = json.loads(edits_path.read_text(encoding="utf-8"))
        if source_path.suffix.lower() in (".yaml", ".yml"):
            from mcblueprint.buildspec import load_buildspec, resolve_buildspec

            source = load_buildspec(source_path)
            resolved = resolve_buildspec(source_path, overrides=overrides)
            config = resolved.config
        else:
            source = json.loads(original)
            config = None
        meta_path = scene_path.with_suffix(scene_path.suffix + ".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if hashlib.sha256(raw_scene).hexdigest() != meta.get("sceneSha256"):
            raise _fail("sidecarのscene hashが一致しません")
        if hashlib.sha256(original).hexdigest() != meta.get("inputSha256"):
            raise _fail("scene生成後に入力が変更されています")
        if scene.get("source", {}).get("sha256") != meta.get("inputSha256"):
            raise _fail("sceneとsidecarの入力世代が一致しません")
        from mcblueprint.pipeline import _cli_overrides, _environment, _hash

        if meta.get("environment") != _environment():
            raise _fail("scene生成後にengineまたはdataが変更されています")
        if config is not None:
            if "cliOverrides" not in meta:
                raise _fail("sidecarにCLI指定の記録がありません。sceneを再生成してください")
            expected = meta["cliOverrides"]
            actual = _cli_overrides(resolved)
            differences = [path for path in sorted(expected.keys() | actual.keys())
                           if path not in expected or path not in actual
                           or expected[path] != actual[path]]
            if differences:
                raise _fail(f"scene生成時のCLI指定と一致しません: {', '.join(differences)}")
        if config is not None and (
                _hash(resolved.buildspec) != meta.get("resolvedHash")
                or _hash(config) != meta.get("configHash")):
            raise _fail("scene生成後にBuildSpecまたは設定が変更されています")
        collected_notes = []
        candidate = apply_edits(scene, edits, source, scene_bytes=raw_scene, notes=collected_notes)
        if config is not None:
            import yaml

            encoded = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode("utf-8")
        else:
            encoded = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{output_path.name}.", dir=output_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
            if source_path.read_bytes() != original:
                raise _fail("保存直前に入力が変更されています")
            os.replace(name, output_path)
        finally:
            Path(name).unlink(missing_ok=True)
    if notes is not None:
        notes.extend(collected_notes)
    return output_path
