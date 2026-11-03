"""BuildSpecの生成、検査、段階保存と検証付き再開。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import numpy as np
import yaml

from mcblueprint import __version__, blockdata
from mcblueprint.buildspec import ResolveResult, resolve_buildspec
from mcblueprint.critic import CriticAccumulator, plan_from_metadata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.generator import GeneratedRegion, GenerationLimits, generate_regions
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.box_like import iter_box
from mcblueprint.planning import plan_buildspec
from mcblueprint.region_export import export_generated_regions
from mcblueprint.scene import capture_sources, write_scene
from mcblueprint.support import check_support
from mcblueprint.traversal import check_route
from mcblueprint.validator import validate_and_load
from mcblueprint.volume import RegionVolume
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


def _environment() -> dict:
    root = Path(__file__).parent
    return {
        "engine": __version__,
        "engineHash": _hash({p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                             for p in sorted(root.rglob("*.py"))}),
        "lockHash": sha256((root / "data/lock.json").read_bytes()).hexdigest(),
        "dataHash": _hash({p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
                           for folder in ("blockdata", "patterns")
                           for p in sorted((root / "data" / folder).rglob("*")) if p.is_file()}),
    }


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
            raise PipelineError("Resume", "入力・engine・データ・中間出力のhashが一致しません")
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
) -> Iterator[GeneratedRegion]:
    """make由来Blueprintの混色とWeatherを再現する。旧Blueprintには使用しない。

    metadata.generationはmakeが保存した合成設定・基準面・実装方式を持つ。
    編集後も呼出し元でvalidate_and_loadを行う。各volumeは次の反復前に解放する。
    """
    metadata = (blueprint.metadata or {}).get("generation", {})
    if not isinstance(metadata, dict) or metadata.get("mode") != GENERATION:
        raise BlueprintError("未対応の生成metadataです")
    from mcblueprint.config import load_schema, validate_schema

    config = metadata.get("config", {})
    validate_schema(config, load_schema("config.schema.json"), "metadata.generation.config")
    if (not {"limits", "weathering", "randomness"} <= config.keys()
            or type(metadata.get("groundHeight")) is not int):
        raise BlueprintError("生成metadataに合成設定・基準面が必要です")
    if not {"region", "halo", "max_memory_mb"} <= config["limits"].keys():
        raise BlueprintError("metadata.generation.config.limitsに合成済み上限が必要です")
    limits = GenerationLimits(**{**config["limits"], "region": tuple(config["limits"]["region"])})
    blocks = blockdata.load_block_data(blueprint.minecraft_version)
    for region in generate_regions(blueprint, limits=limits, seed=seed,
                                   reference_radius=required_halo(config["weathering"])):
        diagnostics = apply_weather(
            region, blocks, config=config["weathering"],
            seed=blueprint.seed if seed is None else seed,
            randomness=config["randomness"], ground_height=metadata["groundHeight"],
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
        key = pos.y, pos.z
        row = self.rows.get(key)
        if row is None:
            self.spool._seek(self.stream, pos.y, pos.z, self.spool.bounds.min.x)
            raw = self.stream.read(self.spool.bounds.size.x * 4)
            if len(raw) != self.spool.bounds.size.x * 4:
                raise BlueprintError("評価spoolの行が欠落しています")
            row = np.frombuffer(raw, dtype="<u4")
            self.rows[key] = row
            if len(self.rows) > self.max_rows:
                self.rows.popitem(last=False)
        else:
            self.rows.move_to_end(key)
        return self.spool.palette[row[pos.x - self.spool.bounds.min.x]]

    def __iter__(self):
        for pos in iter_box(self.core):
            state = self.get(pos)
            if state is not None:
                yield pos, state


def _cores(bounds: AABB, limits: dict) -> Iterator[AABB]:
    width = min(32, limits["region"][0])
    depth = min(32, limits["region"][2])
    height = min(16, limits["region"][1] or 16)
    for y in range(bounds.min.y, bounds.max.y + 1, height):
        for z in range(bounds.min.z, bounds.max.z + 1, depth):
            for x in range(bounds.min.x, bounds.max.x + 1, width):
                low = Vec3(x, y, z)
                yield AABB(low, Vec3.min(bounds.max, low + Vec3(width - 1, height - 1, depth - 1)))


def _spool_regions(window: _SpoolWindow, limits: dict) -> Iterator[GeneratedRegion]:
    # 出力再読込にはhaloも属性も不要。生成時と独立した小さい窓を使う。
    for core in _cores(window.bounds(), limits):
        volume = RegionVolume(core)
        for pos in iter_box(core):
            state = window.get(pos)
            if state is not None:
                volume.set(pos, state)
        yield GeneratedRegion(core, volume)


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
            spool.write(generated_blueprint_regions(blueprint, seed=seed))
            limits = blueprint.metadata["generation"]["config"]["limits"]
            with spool.path.open("rb") as stream:
                window = _SpoolWindow(spool, stream, 32)
                count, first = _support(window, limits)
                if not strict or not count:
                    export_generated_regions(_spool_regions(window, limits), blueprint, path,
                                             format=format)
                return count, first
    except OSError as exc:
        raise BlueprintError(f"Blueprintの出力に失敗しました: {exc}") from exc


def _evaluate(window, blueprint, context, source, max_nodes, critic):
    root = context["massing"]["element"]
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
    points = context["massing"]
    start = Vec3.from_seq(points["entrance"])
    routes = []
    for point in points["arrivals"]:
        end = Vec3.from_seq(point)
        route = check_route(window, start, end, blueprint.minecraft_version, max_nodes=max_nodes)
        reason = route.issues[0].reason if route.issues else ""
        routes.append({"from": start.to_list(), "to": end.to_list(), "status": route.status})
        diagnostic("walkability", "walkability", "ok" if route.status == "reachable" else "fail",
                   route.status, "reachable", position=end.to_list(), detail=reason)
        start = end
    metrics["walkability"] = {"routes": routes}
    for item in context.get("materialDiagnostics", []):
        diagnostics.append({**item, "metric": "palette_relation", "severity": "fail",
                            "relationMetric": item["metric"],
                            "element": root, "source": str(source),
                            "actual": item.get("actual"),
                            "expected": {key: item[key] for key in ("minimum", "maximum")
                                         if key in item},
                            "repairPaths": ["config.palette.contrast",
                                            "config.palette.saturation"]})
    metrics["palette_relation"] = {"value": not bool(context.get("materialDiagnostics"))}
    for metric in ("decoration_density", "facade_mix"):
        if not metrics[metric]:
            reason = ("装飾指定Operationなし" if metric == "decoration_density" else
                      "固定ID役割は対象外、または外壁役割の測定対象なし")
            metrics[metric] = {"value": None, "status": "n/a", "reason": reason}
    if metrics["beam_continuity"]["value"] is None:
        metrics["beam_continuity"].update(status="n/a", reason="計画梁なし")
    if metrics["flat_ratio"]["value"] is None:
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
        # 出力先は解決後に分かるため、入力と合わせて一定順で取り直す。
        locks.enter_context(lock_paths(source, out))
        if source.read_bytes() != original:
            raise PipelineError(stage, "ロック取得中に入力が変更されています", 2)
        expected = {"environment": _environment(), "input": {
            "path": str(source), "basePath": str(source.parent),
            "sha256": sha256(original).hexdigest(),
            "resolvedHash": _hash(resolved.buildspec),
        }}
        context = {"resolved": {"buildspec": resolved.buildspec,
                                "config": config, "origins": resolved.origins}}
        resumed = -1
        resumed_data = None
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
            meta = {"formatVersion": 1, "stage": name, **expected,
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
            context.update(site=plan.site, massing=plan.massing, detail=plan.detail,
                           plannedBlueprint=plan.blueprint, palette=plan.palette)
        for name in ("site", "massing", "detail"):
            checkpoint(name, context[name])
        if resumed < 4:
            from mcblueprint.materialize import materialize

            stage = "Materialize"
            if stage_hook:
                stage_hook(stage)
            material = materialize(context["plannedBlueprint"], style=spec["style"],
                                   biome=spec["site"]["biome"], palette=context["palette"])
            concrete = material.blueprint
            context["materialDiagnostics"] = list(material.diagnostics)
            context["maxAreaRatios"] = material.max_area_ratios
            context["fixedRoles"] = sorted(material.fixed_roles)
            concrete.setdefault("metadata", {})["generation"] = {
                "mode": GENERATION, "config": config,
                "groundHeight": context["site"]["floorY"],
            }
        else:
            concrete = resumed_data
        checkpoint("blueprint", concrete)
        stage = "Generate"
        blueprint, errors = validate_and_load(concrete, base_dir=source.parent)
        if errors:
            raise PipelineError("Validate", " ".join(e.format() for e in errors))
        bounds = blueprint.bounds()
        budget = config["limits"]["max_memory_mb"] * 1024 * 1024
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

            regions = generated_blueprint_regions(blueprint, on_diagnostic=weather_diagnostic)
            style_path = Path(__file__).parent / "data/patterns/styles" / f"{spec['style']}.yaml"
            style = yaml.safe_load(style_path.read_text(encoding="utf-8"))
            critic_plan = plan_from_metadata(
                blueprint.metadata["planning"], massing=context["massing"],
                offset=context["site"]["offset"],
                decoration_range=style["decoration"]["density"], fixed_roles=context["fixedRoles"],
            )
            accumulator = CriticAccumulator(
                critic_plan, max_memory_mb=config["limits"]["max_memory_mb"],
            )

            def consume_regions():
                for region in regions:
                    accumulator.consume(region)
                    yield region

            spool.write(capture_sources(consume_regions(), bounds, source_spool, context,
                                        scene_sources))
            stage = "Critic"
            if stage_hook:
                stage_hook(stage)
            with spool.path.open("rb") as stream:
                max_rows = max(1, min(128, int(budget // (bounds.size.x * 4 + 256) // 8)))
                window = _SpoolWindow(spool, stream, max_rows)
                failed, lines, metrics, structured = _evaluate(
                    window, blueprint, context, source, max_nodes=max(1, int(budget // 2048)),
                    critic=accumulator.finalize(),
                )
                for diagnostic in context["detail"]["diagnostics"]:
                    structured.append(diagnostic)
                    lines.append(_diagnostic_line(diagnostic))
                for code, count in sorted(weather_counts.items()):
                    diagnostic = {"code": code, "metric": "weather_transition", "severity": "warn",
                                  "source": str(source), "element": context["massing"]["element"],
                                  "actual": count, "expected": 0, "repairPaths": []}
                    structured.append(diagnostic)
                    lines.append(_diagnostic_line(diagnostic))
                stage = "Export"
                if stage_hook:
                    stage_hook(stage)
                export_generated_regions(_spool_regions(window, config["limits"]), blueprint,
                                         staged_out, format=fmt)
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
        resource = stage in ("Generate", "Critic") and any(
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
