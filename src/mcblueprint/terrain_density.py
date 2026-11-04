"""2D地形場から局所密度と必須空間の接続計画を評価する。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from mcblueprint.config import load_schema, validate_schema
from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.base import derive_seed
from mcblueprint.terrain_field import TerrainField, _digest, sample_noise
from mcblueprint.volume import BlockVolume, RegionVolume


@dataclass(frozen=True)
class CaveNode:
    """中心と半径が整数の楕円体。隣接節点は内部を重ねて接続する。"""

    id: str
    center: Vec3
    radius: Vec3

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "center": self.center.to_list(), "radius": self.radius.to_list()}


@dataclass(frozen=True)
class DensityTile:
    """領域内だけの(y,z,x)配列。cavityは主空洞、openingはアーチの必須開口。"""

    density: np.ndarray
    cavity: np.ndarray
    support: np.ndarray
    opening: np.ndarray


def _ellipsoid(x, y, z, node, margin=0):
    c, r = node.center, node.radius
    return ((x - c.x) / (r.x + margin)) ** 2 + ((y - c.y) / (r.y + margin)) ** 2 + (
        (z - c.z) / (r.z + margin)) ** 2 <= 1


@dataclass(frozen=True)
class TerrainDensity:
    """3D配列を保持しない地形計画。fieldはH04の場をそのまま共有する。

    sampleは世界座標の閉区間を局所評価する。呼出し側が領域の寿命・大きさを管理する。
    to_dict/from_dictは形状・接続グラフと2D場をJSONへ保存・復元する。
    """

    field: TerrainField
    overhangs: bool
    arches: bool
    main_cave: bool
    entrances: int
    nodes: tuple[CaveNode, ...]
    edges: tuple[tuple[str, str], ...]
    arch: AABB | None

    def to_dict(self) -> dict[str, Any]:
        data = {"formatVersion": 1, "field": self.field.to_dict(),
                "overhangs": self.overhangs, "arches": self.arches,
                "mainCave": self.main_cave, "entrances": self.entrances,
                "caveGraph": {"nodes": [n.to_dict() for n in self.nodes],
                              "edges": [list(edge) for edge in self.edges]},
                "arch": None if self.arch is None else {
                    "from": self.arch.min.to_list(), "to": self.arch.max.to_list()}}
        return {**data, "sha256": _digest(data)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TerrainDensity:
        """破損・未知版・形状不一致はBlueprintError。旧計画の移行は行わない。"""
        try:
            field = TerrainField.from_dict(data["field"])
            validate_schema({"terrain": field.settings}, load_schema("config.schema.json"))
            if (field.heights.shape != (field.bounds.size.z, field.bounds.size.x)
                    or not field.bounds.min.y < field.band_top <= field.bounds.max.y
                    or np.any(field.heights < field.bounds.min.y)
                    or np.any(field.heights > field.band_top)
                    or not field.bounds.min.y <= field.water_level <= field.band_top):
                raise BlueprintError("密度計画の2D場と範囲が不一致です。再生成してください")
            plan = plan_density(field, overhangs=data["overhangs"], arches=data["arches"],
                                main_cave=data["mainCave"], entrances=data["entrances"])
            if plan.to_dict() != data:
                raise BlueprintError("密度計画のhashまたは形状が不一致です。再生成してください")
            return plan
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise BlueprintError("密度計画が不正です。再生成してください") from exc

    @property
    def water_level(self) -> int | None:
        """主空洞の既定水位はなし。明示水位は地上と同じ世界座標。"""
        if self.main_cave and self.field.settings["water_level"] is None:
            return None
        return self.field.water_level

    def sample(self, region: AABB) -> DensityTile:
        """世界座標で局所密度を返す。計画外の照会はBlueprintError。"""
        field = self.field
        if not field.bounds.contains(region.min) or not field.bounds.contains(region.max):
            raise BlueprintError("密度サンプルが計画範囲外です")
        y, z, x = np.meshgrid(
            np.arange(region.min.y, region.max.y + 1, dtype=np.float64),
            np.arange(region.min.z, region.max.z + 1, dtype=np.float64),
            np.arange(region.min.x, region.max.x + 1, dtype=np.float64), indexing="ij")
        height = field.sample(region)[None, :, :]
        density = height - y + np.zeros_like(x)
        band = field.band_top - field.bounds.min.y
        noise = field.settings["noise"]
        coords = np.array([x.ravel(), y.ravel(), z.ravel()])
        options = {"wavelength": max(2, noise["scale"] *
                                    (2 - 1.5 * field.settings["roughness"])),
                   "octaves": noise["octaves"], "persistence": noise["persistence"]}
        if self.overhangs and field.settings["amplitude"]:
            displacement = sample_noise(coords, derive_seed(field.seed, "site.density", field.kind),
                                        **options).reshape(x.shape)
            density += displacement * band * 0.12 * field.settings["amplitude"]
        cavity = np.zeros(x.shape, dtype=bool)
        support = np.zeros(x.shape, dtype=bool)
        for node in self.nodes:
            # 主空洞の壁・床・天井も形状として確保する。
            shell = _ellipsoid(x, y, z, node, 2)
            density[shell] = np.maximum(density[shell], 1)
            support |= shell
            cavity |= _ellipsoid(x, y, z, node)
        amount = field.settings["caves"]
        if amount:
            branches = sample_noise(coords, derive_seed(field.seed, "site.caves", field.kind),
                                    **options).reshape(x.shape)
            cut = ((abs(branches) < amount * 0.14) & (y < height - 3)
                   & (y > field.bounds.min.y + 2))
            density[cut] = -1
        support &= ~cavity
        density[support] = np.maximum(density[support], 1)
        opening = np.zeros(x.shape, dtype=bool)
        if self.arch is not None:
            a, b = self.arch.min, self.arch.max
            inside = ((x >= a.x) & (x <= b.x) & (z >= a.z) & (z <= b.z)
                      & (y >= a.y) & (y <= b.y))
            width = max(1, (b.x - a.x) // 5)
            opening = ((x >= a.x) & (x <= b.x) & (y > a.y) & (y <= b.y)) & (
                                ((x - (a.x + b.x) / 2) /
                                  ((b.x - a.x) / 2 - width)) ** 2 +
                                 ((y - a.y) / (b.y - a.y - 2)) ** 2 < 1)
            support = inside & ~opening
            density[opening] = -1
            density[support] = 1
        density[cavity] = -1
        support &= ~cavity
        density[y == field.bounds.min.y] = 1
        return DensityTile(density, cavity, support, opening)


def plan_density(field: TerrainField, *, overhangs: bool = True, arches: bool = False,
                 main_cave: bool = False, entrances: int = 1) -> TerrainDensity:
    """必須空洞・アーチを計画する。収まらない寸法や入口数はエラー。

    主空洞は中央の楕円体と入口までの楕円体列で保証し、caves=0でも維持する。
    入口は西・東・北・南の順で最大4個。枝洞ノイズは接続保証の対象外。
    """
    if any(type(v) is not bool for v in (overhangs, arches, main_cave)):
        raise BlueprintError("密度形状の指定は真偽値が必要です")
    if type(entrances) is not int or not 1 <= entrances <= 4:
        raise BlueprintError("主空洞の入口数は1〜4が必要です")
    bounds = field.bounds
    band = field.band_top - bounds.min.y
    if (main_cave or arches) and (min(bounds.size.x, bounds.size.z) < 15 or band < 16):
        raise BlueprintError("必須空洞・アーチにはXZ各15以上・地形帯16以上が必要です")
    nodes, edges = [], []
    cx, cz = (bounds.min.x + bounds.max.x) // 2, (bounds.min.z + bounds.max.z) // 2
    if main_cave:
        radius = Vec3(max(3, bounds.size.x // 4), max(3, band // 5),
                      max(3, bounds.size.z // 4))
        center = Vec3(cx, bounds.min.y + radius.y + 3, cz)
        nodes.append(CaveNode("cave#0", center, radius))
        ends = [Vec3(bounds.min.x, center.y, cz), Vec3(bounds.max.x, center.y, cz),
                Vec3(cx, center.y, bounds.min.z), Vec3(cx, center.y, bounds.max.z)]
        r = max(2, min(radius.y, radius.x, radius.z) // 2)
        for entrance, end in enumerate(ends[:entrances]):
            distance = max(abs(end.x - cx), abs(end.z - cz))
            steps = math.ceil(distance / r)
            previous = "cave#0"
            for step in range(1, steps + 1):
                p = Vec3(round(cx + (end.x - cx) * step / steps), center.y,
                         round(cz + (end.z - cz) * step / steps))
                node = CaveNode(f"cave.entrance#{entrance}.{step}", p, Vec3(r, r, r))
                nodes.append(node)
                edges.append((previous, node.id))
                previous = node.id
    arch = None
    if arches:
        arch = AABB(Vec3(bounds.min.x + bounds.size.x // 5, bounds.min.y, cz - 2),
                    Vec3(bounds.max.x - bounds.size.x // 5,
                         bounds.min.y + max(8, band // 2), cz + 2))
        if main_cave:
            raise BlueprintError("主空洞とアーチは別の密度計画で指定してください")
    return TerrainDensity(field, overhangs, arches, main_cave, entrances,
                          tuple(nodes), tuple(edges), arch)


def check_density_region(plan: TerrainDensity, volume: BlockVolume | RegionVolume,
                         region: AABB) -> list[str]:
    """完成coreの必須空洞・アーチ開口・支持部を照合する。欠落を種類ごとに返す。

    空洞の水は明示水位以下だけ許す。建物予約や落下ブロック検査とは独立した検査。
    volumeは世界座標のgetを持つ完成領域で、region内の全セルを参照できること。
    """
    failures = set()
    for y in range(region.min.y, region.max.y + 1, 16):
        for z in range(region.min.z, region.max.z + 1, 32):
            for x in range(region.min.x, region.max.x + 1, 32):
                box = AABB(Vec3(x, y, z), Vec3(min(x + 31, region.max.x),
                                             min(y + 15, region.max.y),
                                             min(z + 31, region.max.z)))
                tile = plan.sample(box)
                for cavity, kind, mask in ((True, "主空洞が閉塞しています", tile.cavity),
                                           (True, "アーチの開口が閉塞しています", tile.opening),
                                           (False, "地形支持部が欠落しています", tile.support)):
                    if kind in failures:
                        continue
                    for iy, iz, ix in np.argwhere(mask):
                        p = box.min + Vec3(int(ix), int(iy), int(iz))
                        state = volume.get(p)
                        allowed = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
                        if plan.water_level is not None and p.y <= plan.water_level:
                            allowed.add("minecraft:water")
                        if (state is None or (state.id not in allowed if cavity
                                              else state.id != "minecraft:stone")):
                            failures.add(kind)
                            break
    return sorted(failures)
