"""完成済み volume の非重複領域出力。"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path

from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model.block import BlockState
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.box_like import iter_box
from mcblueprint.volume import BlockVolume


def export_generated_regions(
    regions: Iterable[GeneratedRegion], blueprint: Blueprint, out_path: Path,
    *, bounds: AABB | None = None, format: str = "schem",
) -> Path:
    """完成core列を消費・退避し、全volumeを復元せず単一ファイルへ出力する。

    regionsはhaloを含んでもよいが、coreは重複なくboundsを被覆すること。
    boundsの既定はblueprint.bounds()。未配置セルはairとして保持し、属性は
    出力しない。呼出側も消費済みvolumeを保持しないこと。形式・coreの不正は
    BlueprintError、生成側の例外はそのまま伝播する。I/O失敗はBlueprintError。
    成功時だけ既存ファイルを置換し、通常の例外・中断では一時物を除去する。
    強制終了時に残る .出力名-spool-* は未完成品であり、再開には使用しない。
    """
    if format not in EXPORTERS:
        raise BlueprintError(f"未対応の出力形式: {format}")
    bounds = blueprint.bounds() if bounds is None else bounds
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{out_path.name}-spool-", dir=out_path.parent,
        ) as tmp:
            spool = RegionSpool(Path(tmp) / "cells", bounds)
            spool.write(regions)
            _export_spool(spool, blueprint, out_path, format=format)
    except OSError as exc:
        raise BlueprintError(f"領域生成の出力に失敗しました: {exc}") from exc
    return out_path


def _export_spool(
    spool: RegionSpool, blueprint: Blueprint, out_path: Path, *, format: str = "schem",
    max_memory_bytes: int | None = None,
) -> Path:
    """同じ処理内で検査・書込みを完了したspoolを出力する。所有側が寿命を管理する。

    再生成や再退避は行わない。未完成spoolと未対応形式はBlueprintError、
    I/O失敗もBlueprintErrorとし、既存ファイルはexport_rowsの原子的保存で保つ。
    max_memory_bytesは呼出し元が残したNBT出力用予算。指定時は配列確保前に検査する。
    """
    if format not in EXPORTERS:
        raise BlueprintError(f"未対応の出力形式: {format}")
    if not spool.complete:
        raise BlueprintError("未完成の出力spoolです")
    try:
        EXPORTERS[format]().export_rows(
            spool.rows(), spool.bounds, blueprint, out_path, palette=spool.palette,
            max_memory_bytes=max_memory_bytes,
        )
    except OSError as exc:
        raise BlueprintError(f"領域生成の出力に失敗しました: {exc}") from exc
    return out_path


class _RegionVolume(BlockVolume):
    def __init__(self, source: BlockVolume, bounds: AABB) -> None:
        super().__init__()
        self.source = source
        self.region = bounds

    @property
    def palette(self) -> list[BlockState]:
        return self.source.palette

    def bounds(self) -> AABB:
        return self.region

    def get(self, pos: Vec3) -> BlockState | None:
        return self.source.get(pos) if self.region.contains(pos) else None


def _box_json(bounds: AABB) -> dict[str, list[int]]:
    return {"min": bounds.min.to_list(), "max": bounds.max.to_list()}


def export_regions(
    volume: BlockVolume,
    blueprint: Blueprint,
    out_dir: Path,
    *,
    region_size: Vec3,
    input_sha256: str,
    format: str = "schem",
    seed: int | None = None,
) -> Path:
    """全生成済み volume を分割し、新規ディレクトリの manifest パスを返す。

    region_size は各軸の正整数。範囲は volume.bounds() の両端を含み、
    未配置セルも air として出力する。terrain 指定時は施工矩形と全取得セルを
    要求し、保護・未取得範囲を拒否する。input_sha256 は入力 JSON の生バイトの
    SHA-256。seed は生成時の上書き値を渡す。各成果物は共通 origin を保持する。
    既存出力先、空 volume、不正寸法・範囲は BlueprintError。出力失敗時には
    完成ディレクトリを残さず、既存出力は変更しない。生成処理は行わない。
    """
    if any(type(value) is not int or value <= 0 for value in region_size):
        raise BlueprintError("分割寸法は各軸とも正整数にしてください")
    if re.fullmatch(r"[0-9a-f]{64}", input_sha256) is None:
        raise BlueprintError("入力 JSON の SHA-256 を小文字16進数64桁で指定してください")
    if format not in EXPORTERS:
        raise BlueprintError(f"未対応の出力形式: {format}")
    bounds = volume.bounds()
    if bounds is None:
        raise BlueprintError("分割するブロックがありません")
    terrain = blueprint.terrain
    if terrain is not None:
        if bounds != terrain.region:
            raise BlueprintError("分割出力の範囲が terrain.region と一致しません")
        for box in terrain.protected:
            if all(bounds.min[i] <= box.max[i] and box.min[i] <= bounds.max[i] for i in range(3)):
                raise BlueprintError("分割出力が保護範囲と交差しています")
        for pos in iter_box(bounds):
            if (terrain.snapshot.get(terrain.snapshot.to_local(pos)) is None
                    or volume.get(pos) is None):
                raise BlueprintError(f"分割出力に未取得セルがあります: {pos}")
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise BlueprintError(f"分割出力先は既に存在します: {out_dir}")
    exporter = EXPORTERS[format]()
    manifest = {
        "formatVersion": 1,
        "format": format,
        "minecraftVersion": blueprint.minecraft_version,
        "seed": blueprint.seed if seed is None else seed,
        "inputSha256": input_sha256,
        "terrainSha256": None if terrain is None else terrain.snapshot.input_sha256,
        "origin": blueprint.origin.to_list(),
        "bounds": _box_json(bounds),
        "regionSize": region_size.to_list(),
        "regions": [],
    }
    try:
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{out_dir.name}-", dir=out_dir.parent) as tmp:
            staging = Path(tmp) / "bundle"
            staging.mkdir()
            for y in range(bounds.min.y, bounds.max.y + 1, region_size.y):
                for z in range(bounds.min.z, bounds.max.z + 1, region_size.z):
                    for x in range(bounds.min.x, bounds.max.x + 1, region_size.x):
                        lo = Vec3(x, y, z)
                        region = AABB(lo, Vec3.min(lo + region_size - Vec3(1, 1, 1), bounds.max))
                        filename = f"region-{len(manifest['regions']):06d}{exporter.extension}"
                        path = staging / filename
                        exporter.export(_RegionVolume(volume, region), blueprint, path)
                        manifest["regions"].append({
                            "file": filename,
                            "bounds": _box_json(region),
                            "offset": (lo - blueprint.origin).to_list(),
                            "sha256": sha256(path.read_bytes()).hexdigest(),
                        })
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            staging.rename(out_dir)
    except OSError as exc:
        raise BlueprintError(f"分割出力に失敗しました: {exc}") from exc
    return out_dir / "manifest.json"
