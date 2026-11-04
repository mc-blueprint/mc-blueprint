"""配色パックv1の構造・意味検証。ファイル探索や通信は行わない。"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from mcblueprint.errors import BlueprintError


@cache
def _validator() -> Draft202012Validator:
    schema = resources.files("mcblueprint.schema").joinpath("colorpack.schema.json")
    return Draft202012Validator(json.loads(schema.read_text(encoding="utf-8")))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ColorPack:
    """検証済みの版・分布・由来。validate_colorpackまたはparse_colorpackで取得する。

    配列はtuple、objectは読み取り専用Mappingで保持し、入力の変更から切り離す。
    taxonomyの元commitと対応表は同梱colorpack-vocabulary.jsonで追跡する。
    """

    format_version: int
    pack_version: str
    taxonomy_version: str
    vocabulary_version: str
    palette_prior: tuple[Mapping[str, Any], ...]
    material_prior: tuple[Mapping[str, Any], ...]
    relation_rule: tuple[Mapping[str, Any], ...]
    accent_rule: tuple[Mapping[str, Any], ...]
    provenance: Mapping[str, Any]


def _fail(path: str, message: str) -> None:
    raise BlueprintError(f"{path}: {message}")


def _json_value(value: Any, path: str = "$") -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                _fail(path, "objectのキーは文字列で指定してください")
            result[key] = _json_value(item, f"{path}.{key}")
        return result
    if isinstance(value, list):
        return [_json_value(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, float) and not math.isfinite(value):
        _fail(path, "数値は有限である必要があります")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        _fail(path, "JSONで表せない値です")
    return value


def _ordered(values: list[float], path: str) -> None:
    if any(left > right for left, right in zip(values, values[1:], strict=False)):
        _fail(path, "5分位点は非減少順で指定してください")


def _condition(when: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(when.items()))


def validate_colorpack(raw: Mapping[str, Any]) -> ColorPack:
    """配色パックv1を入力を変えずに検証し、独立した読み取り専用値を返す。

    同梱schemaの構造検証後に分位点・支持・確率和・重複・親参照を検査する。
    未知のtaxonomy/vocabularyの組と旧形式は受け付けない。不正な値には
    JSONパス付きBlueprintErrorを送出する。入力ファイルの読取・通信は行わない。
    JSONテキストの重複キーも検出する場合はparse_colorpackを使う。
    """
    data = _json_value(raw)
    error = next(_validator().iter_errors(data), None)
    if error is not None:
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        _fail(path, error.message)

    provenance = data["provenance"]
    for kind in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        entries = {}
        for i, row in enumerate(data[kind]):
            path = f"$.{kind}[{i}]"
            when = _condition(row["when"])
            if kind == "relationRule":
                values = [(tuple(row["pair"]), row, path)]
            elif kind == "accentRule":
                values = [(row["role"], row, path)]
            else:
                values = [(role, value, f"{path}.roles.{role}")
                          for role, value in row["roles"].items()]
            for role, value, value_path in values:
                # 逆向きのpairも同じ二役割の競合として扱う。
                identity = tuple(sorted(role)) if kind == "relationRule" else role
                key = (when, identity)
                if key in entries:
                    _fail(value_path, "同条件・同役割またはpairが重複しています")
                entries[key] = (value, value_path)
                if kind == "materialPrior":
                    try:
                        total = math.fsum(value.values())
                    except OverflowError as exc:
                        raise BlueprintError(
                            f"{value_path}: 材質確率の和は有限である必要があります"
                        ) from exc
                    if not 1 - 1e-6 <= total <= 1 + 1e-6:
                        _fail(value_path, "材質確率の和は1±1e-6である必要があります")
                elif kind == "palettePrior":
                    for component, quantiles in value["oklab"].items():
                        _ordered(quantiles, f"{value_path}.oklab.{component}")
                elif kind == "relationRule":
                    for component in ("dL", "dC"):
                        _ordered(value[component], f"{value_path}.{component}")
                    _ordered(value["dh"]["quantiles"], f"{value_path}.dh.quantiles")
        if kind not in ("palettePrior", "relationRule"):
            continue
        for (when, identity), (value, path) in entries.items():
            if not 0 <= value["nEff"] <= value["subjects"] <= value["n"]:
                _fail(path, "支持は0 <= nEff <= subjects <= nを満たす必要があります")
            if value["n"] > provenance["inputs"]:
                _fail(f"{path}.n", "nはprovenance.inputs以下にしてください")
            if value["subjects"] > provenance["subjects"]:
                _fail(f"{path}.subjects", "subjectsはprovenance.subjects以下にしてください")
            support = value.get("support", {"status": "direct"})
            if support["status"] == "direct":
                if value["n"] == 0:
                    _fail(path, "0支持の分布にはsmoothedと親参照が必要です")
                continue
            if any(value[key] != 0 for key in ("n", "subjects", "nEff")):
                _fail(f"{path}.support", "smoothedの直接支持はすべて0にしてください")
            parent = _condition(support["sourceWhen"])
            if len(parent) >= len(when) or not set(parent) < set(when):
                _fail(f"{path}.support.sourceWhen", "厳密な上位条件を指定してください")
            target = entries.get((parent, identity))
            if target is None:
                _fail(f"{path}.support.sourceWhen", "同じ役割/pairの親分布がありません")
            if kind == "relationRule" and target[0]["pair"] != value["pair"]:
                _fail(f"{path}.support.sourceWhen", "親pairの向きが一致しません")

    if provenance["subjects"] > provenance["inputs"]:
        _fail("$.provenance.subjects", "subjectsはinputs以下にしてください")
    if provenance["inputs"] > 0:
        for key in ("licenses", "attributions"):
            if not provenance[key]:
                _fail(f"$.provenance.{key}", "入力があるパックの由来は空にできません")
    for i, attribution in enumerate(provenance["attributions"]):
        if attribution["license"] not in provenance["licenses"]:
            _fail(f"$.provenance.attributions[{i}].license", "licensesにない識別子です")

    frozen = _freeze(data)
    return ColorPack(
        format_version=frozen["formatVersion"], pack_version=frozen["packVersion"],
        taxonomy_version=frozen["taxonomyVersion"],
        vocabulary_version=frozen["vocabularyVersion"],
        palette_prior=frozen["palettePrior"], material_prior=frozen["materialPrior"],
        relation_rule=frozen["relationRule"], accent_rule=frozen["accentRule"],
        provenance=frozen["provenance"],
    )


class _Pairs(list):
    pass


def parse_colorpack(text: str) -> ColorPack:
    """JSONテキストを重複キー・非有限数も含め検証する。I/Oは行わない。

    不正なJSON構文・値にはJSONパス付きBlueprintErrorを送出する。
    ファイルの読み取りエラーは呼出し元で扱う。
    """
    def convert(value: Any, path: str) -> Any:
        if isinstance(value, _Pairs):
            result = {}
            for key, item in value:
                child = f"{path}.{key}"
                if key in result:
                    _fail(child, "JSONのキーが重複しています")
                result[key] = convert(item, child)
            return result
        if isinstance(value, list):
            return [convert(item, f"{path}[{i}]") for i, item in enumerate(value)]
        return value

    try:
        raw = json.loads(text, object_pairs_hook=_Pairs)
    except ValueError as exc:
        raise BlueprintError(f"$: JSONの構文または数値が不正です: {exc}") from exc
    return validate_colorpack(convert(raw, "$"))
