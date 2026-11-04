"""敷地規則の構文・範囲・参照と同梱データのlockを検証する。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from mcblueprint.buildspec import _UniqueLoader
from mcblueprint.config import load_schema, validate_schema
from mcblueprint.errors import BlueprintError

PATTERNS = Path(__file__).parent / "data" / "patterns"


def validate_layout(data: dict, *, styles: set[str], modules: set[str]) -> dict:
    """layout v1を検証し入力を返す。未知キー・逆転範囲・未知参照はBlueprintError。"""
    validate_schema(data, load_schema("pattern-layout.schema.json"), "layout")
    if data["style"] not in styles or not data["name"].startswith(data["style"] + "."):
        raise BlueprintError("layout.style/name: 未知の様式または名前の不整合です")
    for key, limits in data["rules"].items():
        if limits[0] > limits[1]:
            raise BlueprintError(f"layout.rules.{key}: 最小値が最大値を超えています")
    for index, name in enumerate(data["modules"]):
        if name not in modules:
            raise BlueprintError(f"layout.modules[{index}]: 未知のモジュール {name!r}")
    return data


def load_layout(name: str, *, root: Path | None = None) -> dict:
    """style.nameの敷地規則を読む。同梱時はpatterns全体のcommit/hash lockも確認する。

    rootは検証用ライブラリの注入先。I/OエラーはOSError、不正なデータはBlueprintError。
    外部探索・通信・欠損時の既定値への切替は行わない。
    """
    if (len(name.split(".")) != 2
            or any(not part or not all(c.isalnum() or c in "_-" for c in part)
                   for part in name.split("."))):
        raise BlueprintError("layout.name: style.nameで指定してください")
    if root is None:
        root = PATTERNS
        try:
            lock = json.loads((root.parent / "lock.json").read_text(encoding="utf-8"))["patterns"]
            commit, expected = lock["commit"], lock["sha256"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BlueprintError("layout: patternsのlockが不正です") from exc
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(root).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(path.read_bytes())
                digest.update(b"\0")
        if (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
                or digest.hexdigest() != expected):
            raise BlueprintError("layout: patternsのcommit/hash lockが一致しません")
    style, stem = name.split(".")
    path = root / "layouts" / style / f"{stem}.yaml"
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise BlueprintError(f"layout: YAMLが不正です: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("name") != name:
        raise BlueprintError("layout.name: ファイル名と一致しません")
    return validate_layout(data, styles={p.stem for p in (root / "styles").glob("*.yaml")},
                           modules={f"{p.parent.name}.{p.stem}"
                                    for p in (root / "modules").glob("*/*.json")})
