"""BuildSpecの生成、検査、段階保存と検証付き再開。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, fields, is_dataclass
from hashlib import sha256
from pathlib import Path

import numpy as np
import yaml

from mcblueprint import __version__, blockdata
from mcblueprint.buildspec import SUPPORTED_TERRAINS, ResolveResult, resolve_buildspec
from mcblueprint.colorpack import load_colorpack
from mcblueprint.critic import CriticAccumulator, plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.generator import GeneratedRegion, GenerationLimits, generate_regions
from mcblueprint.importers import terrain_file_hash
from mcblueprint.model import AABB, Vec3
from mcblueprint.plan_contract import PLAN_VERSION, plan_buildings
from mcblueprint.planning import plan_buildspec
from mcblueprint.region_export import _export_spool
from mcblueprint.scene import capture_sources, write_scene
from mcblueprint.support import check_support
from mcblueprint.traversal import check_routes
from mcblueprint.validator import validate_and_load
from mcblueprint.weather import apply_weather, required_halo

STAGES = ("resolve", "site", "massing", "detail", "blueprint")
GENERATION = "buildspec-v1"


class PipelineError(BlueprintError):
    """段階とCLI終了コードを持つ失敗。完成出力は公開しない。"""

    def __init__(self, stage: str, message: str, code: int = 1):
        self.stage, self.code = stage, code
        super().__init__(f"{stage}: {' '.join(message.splitlines())}")


@dataclass(frozen=True)
class MakeResult:
    """出力先・実行記録と合否。code=3でも成果物は保存済み。"""

    output: Path
    record: Path
    code: int
    diagnostics: tuple[str, ...]


def preview_make(result: MakeResult, source: Path, *, overrides: list[str], out=None) -> None:
    """CLIのmake成果を表示する。再生成せず読み、表示結果をrunへ記録する。

    3DではCtrl+Cまでサーバーを保持する。表示失敗は生成済み成果を残して
    PipelineError(code=2)を送出し、評価のstatusとは別にpreviewへ記録する。
    """
    from mcblueprint.assets import ensure_assets
    from mcblueprint.output_lock import lock_paths
    from mcblueprint.preview import Options, write_previews
    from mcblueprint.viewer_input import ViewerInput, read_scene
    from mcblueprint.viewer_server import serve

    record = json.loads(result.record.read_bytes())
    config = record["config"]
    view = config["preview"]
    if not view["enabled"]:
        record["preview"] = "disabled"
        _atomic_json(result.record, record)
        return
    try:
        source, output = source.resolve(), result.output.resolve()
        scene_path = output.with_name(output.stem + ".scene.json.gz")
        with lock_paths(source, output):
            raw, scene = read_scene(scene_path)
            meta = json.loads(scene_path.with_suffix(scene_path.suffix + ".meta.json").read_bytes())
            prepared = ViewerInput(
                source, output, scene_path, output.with_name(output.stem + ".edits.json"),
                output.with_name(output.stem + ".edited.json"), config, overrides, True,
                source.read_bytes(), raw, scene, meta,
            )
            prepared.check_generation()
        if view["mode"] == "2d":
            from mcblueprint.importers import read_schematic

            imported = read_schematic(output)
            for path in write_previews(imported.volume, output.parent / "preview", output.stem,
                                       options=Options(version=scene["minecraftVersion"])):
                print(f"Wrote {path.as_posix()}", file=out)
            record["preview"] = "2d"
        else:
            assets = ensure_assets(scene["minecraftVersion"], config["assets"])
            record["preview"] = "3d"
            _atomic_json(result.record, record)
            serve(prepared, assets, open_browser=view["open_browser"], out=out)
            return
    except (BlueprintError, OSError, ValueError, ImportError) as exc:
        record["preview"] = "failed"
        record["previewError"] = str(exc)
        _atomic_json(result.record, record)
        raise PipelineError("Preview", str(exc), 2) from exc
    _atomic_json(result.record, record)


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _hash(value) -> str:
    return sha256(_json(value)).hexdigest()


def _working_budget(max_memory_mb, *retained):
    pending = list(retained)
    seen = set()
    used = 64 * 1024 * 1024  # Python・同梱データ・圧縮処理の余裕。
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        used += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list | tuple | set | frozenset):
            pending.extend(value)
        elif isinstance(value, np.ndarray):
            if value.base is not None:
                pending.append(value.base)
        elif is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, item.name) for item in fields(value))
        elif hasattr(value, "__dict__"):
            pending.append(vars(value))
    available = int(max_memory_mb * 1024 * 1024) - used
    if available <= 0:
        raise BlueprintError("計画・入力と実行環境が limits.max_memory_mb に収まりません")
    return available


def _cli_overrides(resolved: ResolveResult) -> dict:
    result = {}
    pending = list(resolved.config.items())
    while pending:
        path, value = pending.pop()
        if resolved.origins.get(path) == "cli":
            result[path] = value
        elif isinstance(value, dict):
            pending.extend((f"{path}.{key}", item) for key, item in value.items())
    return result


def _check_cli_overrides(meta: dict, resolved: ResolveResult) -> None:
    if "cliOverrides" not in meta:
        raise BlueprintError("sidecarにCLI指定の記録がありません。sceneを再生成してください")
    expected = meta["cliOverrides"]
    actual = _cli_overrides(resolved)
    differences = [path for path in sorted(expected.keys() | actual.keys())
                   if path not in expected or path not in actual or expected[path] != actual[path]]
    if differences:
        raise BlueprintError(f"scene生成時のCLI指定と一致しません: {', '.join(differences)}")


def _atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as f:
        temporary = Path(f.name)
    try:
        temporary.write_bytes(_json(value))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _environment(*, colorpack=...) -> dict:
    if colorpack is ...:
        colorpack = load_colorpack()
    root = Path(__file__).parent
    result = {
        "engine": __version__,
        "planVersion": PLAN_VERSION,
        "engineHash": _hash({p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                             for p in sorted(root.rglob("*.py"))}),
        "lockHash": sha256((root / "data/lock.json").read_bytes()).hexdigest(),
        "dataHash": _hash({p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                           for folder in ("blockdata", "patterns")
                           for p in sorted((root / "data" / folder).rglob("*")) if p.is_file()}),
    }
    if colorpack is not None:
        identity = {
            "packVersion": colorpack.pack_version,
            "taxonomyVersion": colorpack.taxonomy_version,
            "vocabularyVersion": colorpack.vocabulary_version,
            "packSha256": colorpack.pack_sha256,
            "licenseSha256": colorpack.license_sha256,
            "vocabularySha256": colorpack.vocabulary_sha256,
        }
        result["colorpack"] = identity
        result["dataHash"] = _hash({"data": result["dataHash"], "colorpack": identity})
    return result


def _read_checkpoint(
    path: Path, expected: dict, seen: set[Path] | None = None,
) -> tuple[dict, dict]:
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        raise PipelineError("Resume", "前段の参照が循環しています")
    seen.add(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sidecar_path = path.with_suffix(path.suffix + ".meta.json")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if (sidecar["formatVersion"] != 1 or sidecar["stage"] not in STAGES
                or sidecar["environment"] != expected["environment"]
                or sidecar["input"] != expected["input"]
                or sidecar["sha256"] != _hash(data)
                or sidecar["contextHash"] != _hash(sidecar["context"])):
            raise PipelineError("Resume", "入力・engine・データ・中間出力のhashが一致しません。"
                                "BuildSpecから再生成してください")
        if sidecar.get("planVersion") != PLAN_VERSION:
            raise PipelineError("Resume", "内部計画の版が一致しません。"
                                "BuildSpecから再生成してください")
        previous = sidecar["previous"]
        index = STAGES.index(sidecar["stage"])
        context = sidecar["context"]
        resolved = context["resolved"]
        if (_hash(resolved["buildspec"]) != expected["input"]["resolvedHash"]
                or resolved["config"] != resolved["buildspec"]["config"]):
            raise PipelineError("Resume", "再開用contextと合成入力が一致しません")
        if index:
            for key in ("site", "massing", "detail", "plannedBlueprint", "palette"):
                if not isinstance(context[key], dict):
                    raise PipelineError("Resume", "保存済み計画が不正です")
            for key in ("site", "massing", "detail"):
                plan_buildings(context[key])
            plan_buildings(context["plannedBlueprint"]["metadata"]["planning"])
        if index == 4:
            if not isinstance(context["materialDiagnostics"], list):
                raise PipelineError("Resume", "保存済み材質診断が不正です")
        else:
            value = resolved if index == 0 else context[sidecar["stage"]]
            if data != {"formatVersion": 1, "stage": sidecar["stage"], **value}:
                raise PipelineError("Resume", "段階JSONと再開用contextが一致しません")
        if index:
            previous_path = path.parent / previous["file"]
            _, parent = _read_checkpoint(previous_path, expected, seen)
            if (parent["stage"] != STAGES[index - 1]
                    or previous["sha256"] != parent["sha256"]
                    or previous["contextHash"] != parent["contextHash"]):
                raise PipelineError("Resume", "前段のhashまたは段が一致しません")
        elif previous is not None:
            raise PipelineError("Resume", "Resolveに前段はありません")
        return data, sidecar
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise PipelineError("Resume", f"中間出力またはsidecarが不正です: {path}") from exc


def generated_blueprint_regions(
    blueprint, *, seed=None, on_diagnostic: Callable[[dict], None] | None = None,
    max_memory_mb: float | None = None,
) -> Iterator[GeneratedRegion]:
    """make由来Blueprintの混色とWeatherを再現する。旧Blueprintには使用しない。

    metadata.generationはmakeが保存した合成設定・基準面・実装方式を持つ。
    編集後も呼出し元でvalidate_and_loadを行う。各volumeは次の反復前に解放する。
    max_memory_mbは呼出し元が配賦した有限の正の生成・Weather予算。保存設定の上限も超えない。
    """
    metadata = (blueprint.metadata or {}).get("generation", {})
    if not isinstance(metadata, dict) or metadata.get("mode") != GENERATION:
        raise BlueprintError("未対応の生成metadataです")
    from mcblueprint.config import load_schema, validate_schema

    config = metadata.get("config", {})
    validate_schema(config, load_schema("config.schema.json"), "metadata.generation.config")
    ground = metadata.get("groundHeight")
    constant_ground = (isinstance(ground, dict) and ground.get("type") == "constant"
                       and type(ground.get("value")) is int)
    terrain_ground = (isinstance(ground, dict) and ground.get("type") == "terrain"
                      and blueprint.terrain is not None
                      and ground.get("inputHash") == blueprint.terrain.snapshot.input_sha256)
    height_field = None
    if isinstance(ground, dict) and ground.get("type") == "heightfield":
        from mcblueprint.terrain_field import TerrainField

        height_field = TerrainField.from_dict(ground["field"])
    if (not {"limits", "weathering", "randomness"} <= config.keys()
            or not (type(ground) is int or constant_ground or terrain_ground
                    or height_field is not None)):
        raise BlueprintError("生成metadataに合成設定・基準面が必要です")
    if not {"region", "halo", "max_memory_mb"} <= config["limits"].keys():
        raise BlueprintError("metadata.generation.config.limitsに合成済み上限が必要です")
    limits = GenerationLimits(**{**config["limits"], "region": tuple(config["limits"]["region"])})
    if max_memory_mb is not None:
        allocated = GenerationLimits(limits.region, limits.halo, max_memory_mb)
        if allocated.max_memory_mb < limits.max_memory_mb:
            limits = allocated
    blocks = blockdata.load_block_data(blueprint.minecraft_version)
    for region in generate_regions(blueprint, limits=limits, seed=seed,
                                   reference_radius=required_halo(config["weathering"])):
        heights = (blueprint.terrain.snapshot.ground_heights(region.volume.region)
                   if terrain_ground else ground["value"] if constant_ground else ground)
        if height_field is not None:
            area = region.volume.region
            field_bounds = height_field.bounds
            heights = {(x, z): int(height_field.heights[
                min(max(z - field_bounds.min.z, 0), field_bounds.size.z - 1),
                min(max(x - field_bounds.min.x, 0), field_bounds.size.x - 1)])
                for z in range(area.min.z, area.max.z + 1)
                for x in range(area.min.x, area.max.x + 1)}
        diagnostics = apply_weather(
            region, blocks, config=config["weathering"],
            seed=blueprint.seed if seed is None else seed,
            randomness=config["randomness"], ground_height=heights,
            max_memory_mb=limits.max_memory_mb,
        )
        if on_diagnostic:
            for diagnostic in diagnostics:
                on_diagnostic(diagnostic)
        yield region


class _SpoolWindow:
    """完成spoolの行キャッシュ。全体volumeを復元せず隣接セルを参照する。"""

    def __init__(self, spool: RegionSpool, stream, max_rows: int):
        self.spool, self.stream = spool, stream
        self.rows: OrderedDict[tuple[int, int], np.ndarray] = OrderedDict()
        self.max_rows = max_rows
        self.core = spool.bounds

    def bounds(self):
        return self.spool.bounds

    def get(self, pos):
        if not self.spool.bounds.contains(pos):
            return None
        row = self._row(pos.y, pos.z)
        return self.spool.palette[row[pos.x - self.spool.bounds.min.x]]

    def _row(self, y, z):
        key = y, z
        row = self.rows.get(key)
        if row is None:
            self.spool._seek(self.stream, y, z, self.spool.bounds.min.x)
            raw = self.stream.read(self.spool.bounds.size.x * 4)
            if len(raw) != self.spool.bounds.size.x * 4:
                raise BlueprintError("評価spoolの行が欠落しています")
            row = np.frombuffer(raw, dtype="<u4")
            self.rows[key] = row
            if len(self.rows) > self.max_rows:
                self.rows.popitem(last=False)
        else:
            self.rows.move_to_end(key)
        return row

    def __iter__(self):
        bounds, core = self.spool.bounds, self.core
        start = max(0, core.min.x - bounds.min.x)
        stop = min(bounds.size.x, core.max.x - bounds.min.x + 1)
        if start >= stop:
            return
        for y in range(max(core.min.y, bounds.min.y), min(core.max.y, bounds.max.y) + 1):
            for z in range(max(core.min.z, bounds.min.z), min(core.max.z, bounds.max.z) + 1):
                row = self._row(y, z)
                for relative_x in np.flatnonzero(row[start:stop]):
                    x = start + int(relative_x)
                    yield Vec3(bounds.min.x + x, y, z), self.spool.palette[row[x]]


def _cores(bounds: AABB, limits: dict) -> Iterator[AABB]:
    width = min(32, limits["region"][0])
    depth = min(32, limits["region"][2])
    height = min(16, limits["region"][1] or 16)
    for y in range(bounds.min.y, bounds.max.y + 1, height):
        for z in range(bounds.min.z, bounds.max.z + 1, depth):
            for x in range(bounds.min.x, bounds.max.x + 1, width):
                low = Vec3(x, y, z)
                yield AABB(low, Vec3.min(bounds.max, low + Vec3(width - 1, height - 1, depth - 1)))


def _support(window, limits):
    support_count = 0
    first = None
    for core in _cores(window.bounds(), limits):
        window.core = core
        warnings = check_support(window)
        support_count += len(warnings)
        if warnings and first is None:
            first = warnings[0].format().replace("\n", " ")
    return support_count, first


def export_generated_blueprint(
    blueprint, path: Path, *, format: str, seed: int | None = None, strict: bool = False,
) -> tuple[int, str | None]:
    """emitしたBlueprintを生成・支持検査・出力する。strictの支持違反では保存しない。

    戻り値は支持違反数と最初の診断。全体volumeを復元せずmakeと同じ配置を出す。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{path.name}-build-", dir=path.parent) as tmp:
            spool = RegionSpool(Path(tmp) / "cells", blueprint.bounds())
            limits = blueprint.metadata["generation"]["config"]["limits"]
            available = _working_budget(limits["max_memory_mb"], blueprint)
            spool.write(generated_blueprint_regions(
                blueprint, seed=seed, max_memory_mb=available * 3 / 4 / (1024 * 1024),
            ))
            with spool.path.open("rb") as stream:
                window = _SpoolWindow(spool, stream, 32)
                count, first = _support(window, limits)
                if not strict or not count:
                    _export_spool(
                        spool, blueprint, path, format=format,
                        max_memory_bytes=(available // 4 - len(spool.palette) * 2048
                                          - 32 * (spool.bounds.size.x * 4 + 256)),
                    )
                return count, first
    except OSError as exc:
        raise BlueprintError(f"Blueprintの出力に失敗しました: {exc}") from exc


def _evaluate(window, blueprint, context, source, max_nodes, critic):
    buildings = plan_buildings(context["massing"])
    root = buildings[0]["element"] if len(buildings) == 1 else context["massing"]["element"]
    metrics = dict(critic.metrics)
    diagnostics = []

    def diagnostic(code, metric, severity, actual, expected, *, element=root,
                   repair_paths=(), **details):
        diagnostics.append({"code": code, "metric": metric, "severity": severity,
                            "element": element, "source": str(source), "actual": actual,
                            "expected": expected, "repairPaths": list(repair_paths), **details})

    for item in critic.diagnostics:
        details = dict(item)
        code, metric, severity = (details.pop(k) for k in ("code", "metric", "severity"))
        actual, expected = details.pop("actual"), details.pop("expected")
        paths = details.pop("repairPaths", ())
        diagnostic(code, metric, severity, actual, expected,
                   repair_paths=paths, **details)
    structural = [d for d in diagnostics if d["severity"] == "error"]
    if structural:
        raise PipelineError("Critic", " ".join(_diagnostic_line(d) for d in structural), 1)

    support_count, first = _support(window, context["resolved"]["config"]["limits"])
    metrics["support"] = {"value": support_count}
    diagnostic("support", "support", "fail" if support_count else "ok", support_count, 0,
               detail=first)
    plan = context["massing"]
    plan_buildings(plan)
    nodes = {node["id"]: node for node in plan["connections"]["nodes"]}
    routes = []
    edges = list(plan["connections"]["edges"])
    pairs = {frozenset((edge["from"], edge["to"])) for edge in edges}
    for junction in plan["junctions"]:
        for passage in junction["passages"]:
            pair = frozenset((passage["from"], passage["to"]))
            if pair not in pairs:
                edges.append(passage)
                pairs.add(pair)
    pairs = ((Vec3.from_seq(nodes[edge["from"]]["position"]),
              Vec3.from_seq(nodes[edge["to"]]["position"])) for edge in edges)
    for edge, route in zip(edges, check_routes(
        window, pairs, blueprint.minecraft_version, max_nodes=max_nodes,
    ) if edges else (), strict=True):
        start, end = route.start, route.end
        reason = route.issues[0].reason if route.issues else ""
        element = nodes[edge["from"]]["element"]
        routes.append({**({"element": element} if len(buildings) > 1 else {}),
                       "from": start.to_list(), "to": end.to_list(),
                       "status": route.status})
        diagnostic("walkability", "walkability", "ok" if route.status == "reachable" else "fail",
                   route.status, "reachable", element=element,
                   position=end.to_list(), detail=reason)
    metrics["walkability"] = {"routes": routes}
    if not buildings and not routes:
        metrics["walkability"].update(value=None, status="n/a", reason="棟なしの敷地計画")
    for item in context.get("materialDiagnostics", []):
        diagnostics.append({**item, "metric": "palette_relation", "severity": "fail",
                            "relationMetric": item["metric"],
                            "element": item.get("element", root), "source": str(source),
                            "actual": item.get("actual"),
                            "expected": {key: item[key] for key in ("minimum", "maximum")
                                         if key in item},
                            "repairPaths": ["config.palette.contrast",
                                            "config.palette.saturation"]})
    metrics["palette_relation"] = {"value": not bool(context.get("materialDiagnostics"))}
    if not buildings and not context.get("materialDiagnostics"):
        metrics["palette_relation"].update(value=None, status="n/a", reason="対象建物なし")
    if "colorpack" in context:
        checks = context["colorpack"]["relationChecks"]
        relation = {}
        for origin in ("pack", "d28"):
            records = [check for check in checks if check["source"] == origin]
            applied = [check for check in records if check["applied"]]
            passed = sum(check["minimum"] <= check["actual"] <= check["maximum"]
                         for check in applied)
            relation[origin] = {
                "value": passed / len(applied) if applied else None,
                "numerator": passed, "denominator": len(applied),
                "excluded": sum(check.get("reason") == "achromatic" for check in records),
                "missing": sum(check.get("reason") == "color_unavailable" for check in records),
            }
            if relation[origin]["missing"]:
                relation[origin]["status"] = "failed"
            elif not applied:
                relation[origin]["status"] = "n/a"
        numerator = sum(value["numerator"] for value in relation.values())
        denominator = sum(value["denominator"] for value in relation.values())
        metrics["palette_relation"] = {
            "value": numerator / denominator if denominator else None,
            "numerator": numerator, "denominator": denominator, **relation,
        }
    for metric in ("decoration_density", "facade_mix"):
        if not metrics[metric]:
            reason = ("装飾指定Operationなし" if metric == "decoration_density" else
                      "固定ID役割は対象外、または外壁役割の測定対象なし")
            metrics[metric] = {"value": None, "status": "n/a", "reason": reason}
    if (metrics["beam_continuity"]["value"] is None
            and metrics["beam_continuity"].get("status") != "n/a"):
        metrics["beam_continuity"].update(status="n/a", reason="計画梁なし")
    if metrics["flat_ratio"]["value"] is None and metrics["flat_ratio"].get("status") != "n/a":
        metrics["flat_ratio"].update(status="failed", reason="有効な外壁深度を測定できません")
    lines = [_diagnostic_line(d) for d in diagnostics]
    for metric, value in metrics.items():
        if any(d["metric"] == metric for d in diagnostics):
            continue
        actual = value.get("value", value)
        if value.get("status") == "n/a":
            actual = "n/a"
        state = {"n/a": "INFO", "failed": "FAIL"}.get(value.get("status"), "OK")
        lines.append(f"{state} {metric}={actual} {source} {root}"
                     + (f" {value['reason']}" if "reason" in value else ""))
    return any(d["severity"] == "fail" for d in diagnostics), lines, metrics, diagnostics


def _diagnostic_line(diagnostic):
    severity = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "error": "FAIL"}
    actual = diagnostic["actual"]
    paths = ",".join(diagnostic["repairPaths"])
    return (f"{severity[diagnostic['severity']]} {diagnostic['code']} "
            f"{diagnostic['metric']}={actual} expected={diagnostic['expected']} "
            f"{diagnostic['source']} {diagnostic['element']}"
            + (f" face={diagnostic['face']}" if "face" in diagnostic else "")
            + (f" role={diagnostic['role']}" if "role" in diagnostic else "")
            + (f" position={diagnostic['position']}" if "position" in diagnostic else "")
            + (f" pair={diagnostic['pair']}" if "pair" in diagnostic else "")
            + (f" → {paths}" if paths else "")
            + (f" {diagnostic['detail']}" if diagnostic.get("detail") else ""))


def make(
    source: str | Path, *, output: str | Path | None = None,
    emit: list[str] | None = None, resume: str | Path | None = None,
    overrides: list[str] | None = None, project_dir: Path | None = None,
    user_config: Path | None = None, stage_hook: Callable[[str], None] | None = None,
) -> MakeResult:
    """BuildSpecを一度生成し、完成coreから評価・単一出力する。

    emitはSTAGESの段名。要求した段までの前段とsidecarも出力隣へ保存する。
    resumeは同じ入力・合成設定・engine・データに限る。sourceは常に必要。
    stage_hookは段開始の計測用。成功0、評価不合格3を返し、それ以外は
    PipelineError（検証1、I/O・資源不足2）を送出する。3D表示は実行しない。
    入力と出力stemを非待機で排他し、競合はPipelineError(code=2)とする。
    解決後の再ロック時と公開直前に入力の変更を検出した場合も競合として拒否する。
    """
    from mcblueprint.output_lock import lock_paths

    source = Path(source).resolve()
    locks = ExitStack()
    stage = "Resolve"
    staged_out = None
    staged_scene = None
    staged_scene_meta = None
    try:
        if stage_hook:
            stage_hook(stage)
        with lock_paths(source):
            original = source.read_bytes()
            resolved = resolve_buildspec(source, project_dir=project_dir, user_config=user_config,
                                         overrides=overrides)
        config = resolved.config
        fmt = config["output"]["format"]
        out = Path(output) if output is not None else Path(config["output"]["directory"])
        if out.suffix != EXPORTERS[fmt].extension:
            out = out / (source.stem + EXPORTERS[fmt].extension)
        if out.resolve() == source:
            raise PipelineError(stage, "入力と出力は別のパスにしてください", 2)
        terrain_source = resolved.buildspec["site"]["terrain"]
        if (terrain_source not in SUPPORTED_TERRAINS
                and out.resolve() == Path(terrain_source).resolve()):
            raise PipelineError(stage, "元地形の入力と出力は別のパスにしてください", 2)
        # 出力先は解決後に分かるため、入力と合わせて一定順で取り直す。
        locks.enter_context(lock_paths(source, out))
        if source.read_bytes() != original:
            raise PipelineError(stage, "ロック取得中に入力が変更されています", 2)
        if (resolved.buildspec["archetype"] not in ("house", "tower")
                or terrain_source in SUPPORTED_TERRAINS[2:]):
            plan_buildspec(resolved)
        colorpack = load_colorpack()
        expected = {"environment": _environment(colorpack=colorpack), "input": {
            "path": str(source), "basePath": str(source.parent),
            "sha256": sha256(original).hexdigest(),
            "resolvedHash": _hash(resolved.buildspec),
        }}
        terrain_hash = None
        if terrain_source not in SUPPORTED_TERRAINS:
            terrain_hash = terrain_file_hash(terrain_source)
            expected["input"]["terrainHash"] = terrain_hash
        context = {"resolved": {"buildspec": resolved.buildspec,
                                "config": config, "origins": resolved.origins}}
        resumed = -1
        resumed_data = None
        terrain_snapshot = None
        if resume is not None:
            stage = "Resume"
            resumed_data, sidecar = _read_checkpoint(Path(resume), expected)
            resumed = STAGES.index(sidecar["stage"])
            context = sidecar["context"]
        requested = config["output"]["emit"] if emit is None else emit
        if set(requested) - set(STAGES):
            raise PipelineError("Emit", "未知の段です", 2)
        emit_last = max((STAGES.index(s) for s in requested), default=-1)
        previous = None

        def checkpoint(name, value):
            nonlocal previous
            if STAGES.index(name) > emit_last:
                return
            if name != "blueprint":
                value = {"formatVersion": 1, "stage": name, **value}
            path = out.with_name(f"{out.stem}.{name}.json")
            if path.resolve() == source:
                raise PipelineError("Emit", "中間出力が入力と同じパスです", 2)
            saved = context.copy()
            if name == "resolve":
                saved = {"resolved": context["resolved"]}
            elif name != "blueprint":
                saved.pop("materialDiagnostics", None)
                saved.pop("maxAreaRatios", None)
            meta = {"formatVersion": 1, "planVersion": PLAN_VERSION, "stage": name, **expected,
                    "sha256": _hash(value), "previous": previous,
                    "context": saved, "contextHash": _hash(saved)}
            _atomic_json(path, value)
            _atomic_json(path.with_suffix(".json.meta.json"), meta)
            previous = {"file": path.name, "sha256": meta["sha256"],
                        "contextHash": meta["contextHash"]}

        checkpoint("resolve", context["resolved"])
        spec = resolved.buildspec
        if resumed < 1:
            stage = "Site/Massing/Detail"
            if stage_hook:
                stage_hook(stage)
            plan = plan_buildspec(ResolveResult(**context["resolved"]))
            terrain_snapshot = plan.terrain_snapshot
            context.update(site=plan.site, massing=plan.massing, detail=plan.detail,
                           plannedBlueprint=plan.blueprint, palette=plan.palette)
        for name in ("site", "massing", "detail"):
            checkpoint(name, context[name])
        if resumed < 4:
            from mcblueprint.materialize import materialize_plan

            stage = "Materialize"
            if stage_hook:
                stage_hook(stage)
            material = materialize_plan(context["plannedBlueprint"], style=spec["style"],
                                   biome=spec["site"]["biome"], palette=context["palette"],
                                   colorpack=colorpack.pack if colorpack is not None else None,
                                   building_type=spec["archetype"])
            concrete = material.blueprint
            context["materialDiagnostics"] = list(material.diagnostics)
            context["maxAreaRatios"] = material.max_area_ratios
            context["buildingMaxAreaRatios"] = {
                root: result.max_area_ratios for root, result in material.building_results.items()
            }
            context["fixedRoles"] = sorted(material.fixed_roles)
            context["buildingFixedRoles"] = {
                root: sorted(result.fixed_roles)
                for root, result in material.building_results.items()
            }
            if colorpack is not None:
                context["colorpack"] = {
                    **expected["environment"]["colorpack"],
                    "selectionSources": material.selection_sources,
                    "relationChecks": list(material.relation_checks),
                    "maxAreaRatios": material.max_area_ratios,
                    **({"buildingMaxAreaRatios": context["buildingMaxAreaRatios"]}
                       if material.building_results else {}),
                    "attempts": material.attempts,
                }
            concrete.setdefault("metadata", {})["generation"] = {
                "mode": GENERATION, "config": config,
                "groundHeight": context["site"].get("groundHeight", {
                    "type": "constant", "value": context["site"]["floorY"],
                }),
            }
            if "terrainHash" in context["site"]:
                concrete["metadata"]["generation"]["terrainHash"] = context["site"]["terrainHash"]
        else:
            concrete = resumed_data
        checkpoint("blueprint", concrete)
        stage = "Generate"
        blueprint, errors = validate_and_load(
            concrete, base_dir=source.parent, terrain_snapshot=terrain_snapshot,
        )
        if errors:
            message = " ".join(e.format() for e in errors)
            raise PipelineError("Validate", message, 2 if "max_memory_mb" in message else 1)
        if (terrain_hash is not None
                and blueprint.terrain.snapshot.input_sha256 != terrain_hash):
            raise PipelineError(stage, "計画中に地形入力が変更されています", 2)
        bounds = blueprint.bounds()
        budget = _working_budget(config["limits"]["max_memory_mb"],
                                 context, blueprint, concrete, resolved)
        if stage_hook:
            stage_hook(stage)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=out.parent, prefix=f".{out.name}-", delete=False) as f:
            staged_out = Path(f.name)
        staged_out.unlink()
        with tempfile.NamedTemporaryFile(
            dir=out.parent, prefix=f".{out.name}-scene-", delete=False,
        ) as f:
            staged_scene = Path(f.name)
        staged_scene.unlink()
        with tempfile.NamedTemporaryFile(
            dir=out.parent, prefix=f".{out.name}-scene-meta-", delete=False,
        ) as f:
            staged_scene_meta = Path(f.name)
        staged_scene_meta.unlink()
        with tempfile.TemporaryDirectory(prefix=f".{out.name}-pipeline-", dir=out.parent) as tmp:
            temporary_dir = Path(tmp)
            spool = RegionSpool(temporary_dir / "cells", bounds)
            source_spool = temporary_dir / "sources"
            scene_sources: dict[int, dict] = {}
            weather_counts: dict[str, int] = {}

            def weather_diagnostic(diagnostic):
                code = diagnostic["code"]
                weather_counts[code] = weather_counts.get(code, 0) + 1

            regions = generated_blueprint_regions(
                blueprint, on_diagnostic=weather_diagnostic,
                max_memory_mb=budget / 2 / (1024 * 1024),
            )
            style_path = Path(__file__).parent / "data/patterns/styles" / f"{spec['style']}.yaml"
            style = yaml.safe_load(style_path.read_text(encoding="utf-8"))
            critic_plan = plan_from_metadata(
                blueprint.metadata["planning"], massing=context["massing"],
                offset=context["site"]["offset"],
                decoration_range=style["decoration"]["density"], fixed_roles=context["fixedRoles"],
                building_fixed_roles=context.get("buildingFixedRoles", {}),
            )
            accumulator = CriticAccumulator(
                critic_plan, max_memory_mb=budget / 4 / (1024 * 1024),
                max_area_ratios=context["maxAreaRatios"] if colorpack is not None else None,
                building_area_ratios=context.get("buildingMaxAreaRatios", {}),
            )

            def consume_regions():
                for region in regions:
                    accumulator.consume(region)
                    check_tables()
                    yield region

            def check_tables():
                if len(scene_sources) * 4096 + len(spool.palette) * 2048 > budget / 4:
                    raise BlueprintError(
                        "出典表・出力パレットが limits.max_memory_mb に収まりません",
                    )

            spool.write(capture_sources(consume_regions(), bounds, source_spool, context,
                                        scene_sources))
            check_tables()
            stage = "Critic"
            if stage_hook:
                stage_hook(stage)
            critic_result = accumulator.finalize()
            accumulator = None
            del critic_plan
            with spool.path.open("rb") as stream:
                max_rows = max(1, min(128, int(budget // (bounds.size.x * 4 + 256) // 8)))
                window = _SpoolWindow(spool, stream, max_rows)
                failed, lines, metrics, structured = _evaluate(
                    window, blueprint, context, source, max_nodes=max(1, int(budget // 2 // 2048)),
                    critic=critic_result,
                )
                for diagnostic in context["detail"]["diagnostics"]:
                    structured.append(diagnostic)
                    lines.append(_diagnostic_line(diagnostic))
                for code, count in sorted(weather_counts.items()):
                    buildings = plan_buildings(context["massing"])
                    root = (buildings[0]["element"] if len(buildings) == 1
                            else context["massing"]["element"])
                    diagnostic = {"code": code, "metric": "weather_transition", "severity": "warn",
                                  "source": str(source), "element": root,
                                  "actual": count, "expected": 0, "repairPaths": []}
                    structured.append(diagnostic)
                    lines.append(_diagnostic_line(diagnostic))
                stage = "Export"
                if stage_hook:
                    stage_hook(stage)
                _export_spool(
                    spool, blueprint, staged_out, format=fmt,
                    max_memory_bytes=(budget // 4 - len(scene_sources) * 4096
                                      - len(spool.palette) * 2048
                                      - max_rows * (bounds.size.x * 4 + 256)),
                )
            input_kind = "spec" if source.suffix.lower() in (".yaml", ".yml") else "blueprint"
            scene_input = {"kind": input_kind, "path": str(source),
                           "sha256": expected["input"]["sha256"]}
            view = config["preview"]
            scene_preview = {"enabled": view["enabled"], "shader": view["shader"],
                             "shadows": view["shadows"], "brightness": view["brightness"],
                             "timeOfDay": view["time_of_day"], "blockLight": view["block_light"],
                             "quality": view["quality"]}
            from mcblueprint.viewer_input import _spec_sources

            source_table = {"sources": list(scene_sources.values())}
            _spec_sources(source_table, resolved, massing=context["massing"])
            scene_sources = {entry["id"]: entry for entry in source_table["sources"]}
            scene_hash = write_scene(
                staged_scene, source_spool, spool, scene_sources, blueprint, context,
                scene_input, scene_preview,
                view["biome"] or context["resolved"]["buildspec"]["site"]["biome"],
            )
            _atomic_json(staged_scene_meta, {
                "formatVersion": 1,
                "inputSha256": expected["input"]["sha256"],
                "resolvedHash": expected["input"]["resolvedHash"],
                "configHash": _hash(config),
                "cliOverrides": _cli_overrides(resolved),
                "environment": expected["environment"],
                "sceneSha256": scene_hash,
            })
        lines.append("INFO preview=not-run (表示はCLIが実行)")
        status = "failed" if failed else "passed"
        lines.append(f"STATUS {status}")
        record = out.with_suffix(out.suffix + ".run.json")
        with tempfile.TemporaryDirectory(prefix=f".{out.name}-publish-", dir=out.parent) as tmp:
            temporary_dir = Path(tmp)
            staged_record = temporary_dir / record.name
            _atomic_json(staged_record, {"formatVersion": 1, **expected, "status": status,
                                         "preview": "not-run", "seed": blueprint.seed,
                                         "config": config, "sources": resolved.origins,
                                         "output": str(out), "diagnostics": lines,
                                         **({"colorpack": context["colorpack"]}
                                            if colorpack is not None else {}),
                                         "metrics": metrics, "structuredDiagnostics": structured})
            backups = {}
            scene_out = out.with_name(f"{out.stem}.scene.json.gz")
            scene_meta_out = scene_out.with_suffix(scene_out.suffix + ".meta.json")
            for target in (out, record, scene_out, scene_meta_out):
                backup = temporary_dir / f"{target.name}.backup"
                if target.exists():
                    shutil.copy2(target, backup)
                    backups[target] = backup
            try:
                if source.read_bytes() != original:
                    raise PipelineError(stage, "公開直前に入力が変更されています", 2)
                if terrain_hash is not None and terrain_file_hash(terrain_source) != terrain_hash:
                    raise PipelineError(stage, "公開直前に地形入力が変更されています", 2)
                os.replace(staged_out, out)
                os.replace(staged_record, record)
                os.replace(staged_scene, scene_out)
                os.replace(staged_scene_meta, scene_meta_out)
            except OSError:
                for target in (out, record, scene_out, scene_meta_out):
                    if target in backups:
                        os.replace(backups[target], target)
                    else:
                        target.unlink(missing_ok=True)
                raise
        return MakeResult(out, record, 3 if failed else 0, tuple(lines))
    except PipelineError:
        raise
    except (BlueprintError, OSError, UnicodeError, ValueError, MemoryError) as exc:
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        resource = stage in ("Site/Massing/Detail", "Generate", "Critic") and any(
            word in str(exc) for word in ("メモリ", "max_memory_mb")
        )
        code = 2 if isinstance(cause, OSError | MemoryError) or resource or stage == "Export" else 1
        raise PipelineError(stage, str(exc).replace("\n", " "), code) from exc
    finally:
        locks.close()
        if staged_out is not None:
            staged_out.unlink(missing_ok=True)
        if staged_scene is not None:
            staged_scene.unlink(missing_ok=True)
        if staged_scene_meta is not None:
            staged_scene_meta.unlink(missing_ok=True)
