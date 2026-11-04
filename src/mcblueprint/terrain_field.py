"""世界座標ノイズと全体侵食から、領域に依存しない2D地形を計画する。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from dataclasses import dataclass
from typing import Any

import numpy as np
from pyfastnoiselite.pyfastnoiselite import FastNoiseLite, NoiseType

from mcblueprint.blockdata import BlockData
from mcblueprint.config import DEFAULTS, load_schema, validate_schema
from mcblueprint.errors import BlueprintError
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.base import derive_seed

RELIEF = {"flat": 0.0, "hill": 0.35, "mountain": 0.85,
          "plateau": 0.60, "valley": 0.50, "island": 0.55}
NOISE = {"library": "pyfastnoiselite", "version": "0.0.7",
         "type": "OpenSimplex2", "normalization": "weighted-amplitude",
         "lacunarity": 2, "coordinateType": "float32"}


def _digest(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                     allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sample_noise(coords: np.ndarray, seed: int, *, wavelength: float,
                 octaves: int, persistence: float) -> np.ndarray:
    """(2または3, N)世界座標から正規化ノイズを返す。列の順序・分割に依存しない。

    wavelengthは正、octavesは1以上、persistenceは0〜1の解決済み設定。
    各オクターブの振幅和で正規化し、サンプル集合のmin/maxは使わない。
    """
    coords = np.ascontiguousarray(coords, dtype=np.float32)
    noise = FastNoiseLite(seed=seed % 2147483648)
    noise.noise_type = NoiseType.NoiseType_OpenSimplex2
    result = np.zeros(coords.shape[1], dtype=np.float64)
    weight, total = 1.0, 0.0
    for octave in range(octaves):
        noise.frequency = 2**octave / wavelength
        result += weight * noise.gen_from_coords(coords)
        total += weight
        weight *= persistence
        if weight == 0:
            break
    return np.clip(result / total, -1, 1)


def _bilinear(field, x, z):
    ix, iz = min(int(x), field.shape[1] - 2), min(int(z), field.shape[0] - 2)
    u, v = x - ix, z - iz
    a, b = field[iz, ix:ix + 2]
    c, d = field[iz + 1, ix:ix + 2]
    height = (a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v
    gradient = ((b - a) * (1 - v) + (d - c) * v,
                (c - a) * (1 - u) + (d - b) * u)
    return height, gradient, (iz, ix, u, v)


def _transfer(field, cell, amount):
    iz, ix, u, v = cell
    field[iz:iz + 2, ix:ix + 2] += amount * np.array([
        [(1 - u) * (1 - v), u * (1 - v)], [(1 - u) * v, u * v]
    ])


def _erode(field, amount, seed):
    result = field.copy()
    if not amount or min(field.shape) < 2 or np.ptp(field) == 0:
        return result
    rng = random.Random(seed)
    nz, nx = field.shape
    for _ in range(round(amount * field.size * 2)):
        x, z = rng.random() * (nx - 1), rng.random() * (nz - 1)
        dx = dz = sediment = 0.0
        speed = water = 1.0
        for _ in range(32):
            old, (gx, gz), cell = _bilinear(result, x, z)
            dx, dz = dx * 0.05 - gx * 0.95, dz * 0.05 - gz * 0.95
            length = math.hypot(dx, dz)
            if length == 0:
                angle = rng.random() * math.tau
                dx, dz = math.cos(angle), math.sin(angle)
            else:
                dx, dz = dx / length, dz / length
            x, z = x + dx, z + dz
            if not (0 <= x < nx - 1 and 0 <= z < nz - 1):
                _transfer(result, cell, sediment)
                break
            new, _, _ = _bilinear(result, x, z)
            delta = new - old
            capacity = max(-delta, 0.01) * speed * water * 4
            if delta > 0 or sediment > capacity:
                deposit = min(delta, sediment) if delta > 0 else (sediment - capacity) * 0.3
                _transfer(result, cell, deposit)
                sediment -= deposit
            else:
                removed = min((capacity - sediment) * 0.3, -delta)
                # 4頂点の最小値を超えて削らず、地形帯の底を守る。
                iz, ix, _, _ = cell
                removed = min(removed, max(0, float(result[iz:iz + 2, ix:ix + 2].min())))
                _transfer(result, cell, -removed)
                sediment += removed
            speed = math.sqrt(max(0, speed * speed - delta * 4))
            water *= 0.95
        else:
            _transfer(result, _bilinear(result, x, z)[2], sediment)
    return result


def _interpolate(field, width, depth):
    xs = np.linspace(0, field.shape[1] - 1, width)
    zs = np.linspace(0, field.shape[0] - 1, depth)
    rows = np.array([np.interp(xs, np.arange(field.shape[1]), row) for row in field])
    return np.array([np.interp(zs, np.arange(field.shape[0]), col) for col in rows.T]).T


@dataclass(frozen=True)
class TerrainColumn:
    """1列の計画。top以下を充填し、上3層をsoil、それより下をrockにする。

    surfaceはtopの材、waterはtopより上の水面（なければNone）。vegetationは
    grass（高さ1）またはtree（高さ5・半径2）の配置候補。領域生成側が切り出す。
    """

    top: int
    surface: str
    soil: str
    rock: str
    water: int | None
    vegetation: str | None


@dataclass(frozen=True)
class TerrainField:
    """Siteで確定した2D地表。配列は[z,x]、高さは世界座標の整数。

    sampleはXZの閉区間を読み取り専用viewで返す。領域外はBlueprintError。
    to_dictの全体場・設定・ノイズ方式・sha256をcheckpointに保存できる。
    """

    bounds: AABB
    band_top: int
    kind: str
    seed: int
    settings: dict[str, Any]
    heights: np.ndarray
    coarse_delta: np.ndarray
    water_level: int

    def sample(self, region: AABB) -> np.ndarray:
        if (region.min.x < self.bounds.min.x or region.max.x > self.bounds.max.x
                or region.min.z < self.bounds.min.z or region.max.z > self.bounds.max.z):
            raise BlueprintError("地形サンプルが計画のXZ範囲外です")
        return self.heights[
            region.min.z - self.bounds.min.z:region.max.z - self.bounds.min.z + 1,
            region.min.x - self.bounds.min.x:region.max.x - self.bounds.min.x + 1,
        ]

    def to_dict(self) -> dict[str, Any]:
        data = {"formatVersion": 1, "bounds": {"from": self.bounds.min.to_list(),
                "to": self.bounds.max.to_list()}, "bandTop": self.band_top,
                "kind": self.kind, "seed": self.seed, "settings": copy.deepcopy(self.settings),
                "noise": dict(NOISE), "heights": self.heights.tolist(),
                "coarseDelta": self.coarse_delta.tolist(), "waterLevel": self.water_level}
        return {**data, "sha256": _digest(data)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TerrainField:
        """to_dictの場を検証して復元する。破損・方式違いは再生成を要求する。"""
        data = copy.deepcopy(data)
        digest = data.pop("sha256")
        if digest != _digest(data) or data["formatVersion"] != 1 or data["noise"] != NOISE:
            raise BlueprintError("地形場のhashまたはノイズ方式が不一致です。再生成してください")
        bounds = AABB(Vec3.from_seq(data["bounds"]["from"]),
                      Vec3.from_seq(data["bounds"]["to"]))
        heights = np.array(data["heights"], dtype=np.int32)
        delta = np.array(data["coarseDelta"], dtype=np.float64)
        heights.flags.writeable = delta.flags.writeable = False
        return cls(bounds, data["bandTop"], data["kind"], data["seed"],
                   data["settings"], heights, delta, data["waterLevel"])

    def column(self, x: int, z: int, blockdata: BlockData) -> TerrainColumn:
        """世界座標の列の表層・水・植生候補を返す。必要材のない版はエラー。

        傾斜は全体場の隣接列、水際は水平L1距離2以内で測る。Weatherの植生設定は
        参照しない。樹木は5格子の候補だけに置き、外形内に樹冠・頭上余裕を確保する。
        """
        top = int(self.sample(AABB(Vec3(x, 0, z), Vec3(x, 0, z)))[0, 0])
        ix, iz = x - self.bounds.min.x, z - self.bounds.min.z
        lo_x, hi_x = max(0, ix - 2), min(self.heights.shape[1] - 1, ix + 2)
        lo_z, hi_z = max(0, iz - 2), min(self.heights.shape[0] - 1, iz + 2)
        near_water = any(
            self.heights[b, a] < self.water_level
            for b in range(lo_z, hi_z + 1) for a in range(lo_x, hi_x + 1)
            if abs(a - ix) + abs(b - iz) <= 2
        )
        slope = max(abs(top - int(self.heights[b, a])) for a, b in (
            (max(0, ix - 1), iz), (min(self.heights.shape[1] - 1, ix + 1), iz),
            (ix, max(0, iz - 1)), (ix, min(self.heights.shape[0] - 1, iz + 1)),
        ))
        water = self.water_level if top < self.water_level else None
        surface = ("sand" if near_water or water is not None else "stone" if slope >= 2 else
                   "snow_block" if top >= self.bounds.min.y +
                   0.8 * (self.band_top - self.bounds.min.y) else "grass_block")
        soil = "sandstone" if surface == "sand" else "dirt"
        materials = {"stone": "stone", "dirt": "soil", "grass_block": "soil",
                     "sand": "glass", "sandstone": "stone", "snow_block": "ice_snow"}
        for name in (surface, soil, "stone"):
            info = blockdata.get(f"minecraft:{name}")
            if info is None or info.shape != "full" or info.material != materials[name]:
                raise BlueprintError(f"地形材 {name} が版の材質分類と一致しません")
        vegetation = None
        if surface == "grass_block" and water is None and top < self.bounds.max.y:
            rng = random.Random(derive_seed(self.seed, "site.vegetation", f"{x},{z}"))
            amount = self.settings["vegetation"]
            if (x % 5 == 2 and z % 5 == 2 and top + 5 <= self.bounds.max.y
                    and self.bounds.min.x + 2 <= x <= self.bounds.max.x - 2
                    and self.bounds.min.z + 2 <= z <= self.bounds.max.z - 2
                    and rng.random() < amount):
                vegetation = "tree"
            elif rng.random() < amount * 0.3:
                vegetation = "grass"
        return TerrainColumn(top, f"minecraft:{surface}", f"minecraft:{soil}",
                             "minecraft:stone", water, vegetation)


def plan_terrain(bounds: AABB, kind: str, settings: dict[str, Any], seed: int,
                 *, clearance: int = 0) -> TerrainField:
    """D61の地形帯を計画する。settingsはterrain設定（省略値は既定値）。

    clearanceは建築最高部・頭上・表層などSiteで予約した高さの合計。
    H=size.y-1-clearanceが1未満、未対応地形、水位が地形帯外ならBlueprintError。
    2D全体高さと最大65×65の侵食差だけを保持し、3D場は作らない。
    """
    if kind not in RELIEF:
        raise BlueprintError(f"未対応の地形タイプ: {kind}")
    validate_schema({"terrain": settings}, load_schema("config.schema.json"))
    config = copy.deepcopy(DEFAULTS["terrain"])
    config.update(settings)
    config["noise"] = {**DEFAULTS["terrain"]["noise"], **settings.get("noise", {})}
    height = bounds.size.y - 1 - clearance
    if clearance < 0 or height < 1:
        raise BlueprintError("地形帯Hは1以上で、計画余裕は非負である必要があります")
    water = config["water_level"]
    if water is None:
        water = bounds.min.y + math.floor(height * (0.25 if kind == "island" else 0.15) + 0.5)
    if not bounds.min.y <= water <= bounds.min.y + height:
        raise BlueprintError("terrain.water_level が地形帯外です")
    width, depth = bounds.size.x, bounds.size.z
    x, z = np.meshgrid(np.arange(width) + bounds.min.x, np.arange(depth) + bounds.min.z)
    nx = (x - (bounds.min.x + bounds.max.x) / 2) / max(0.5, (width - 1) / 2)
    nz = (z - (bounds.min.z + bounds.max.z) / 2) / max(0.5, (depth - 1) / 2)
    radius = np.sqrt(nx * nx + nz * nz)
    shape = np.clip(1 - radius, 0, 1)
    if kind == "mountain":
        shape **= 1.5
    elif kind == "plateau":
        shape = np.clip((1 - np.maximum(abs(nx), abs(nz))) * 3, 0, 1)
    elif kind == "valley":
        shape = abs(nx)
    relief = height * RELIEF[kind] * config["amplitude"]
    noise = config["noise"]
    wavelength = max(2, noise["scale"] * (2 - 1.5 * config["roughness"]))
    values = np.full((depth, width), height * 0.15)
    if relief:
        perturbation = sample_noise(np.array([x.ravel(), z.ravel()]),
                                    derive_seed(seed, "site.noise", kind),
                                    wavelength=wavelength, octaves=noise["octaves"],
                                    persistence=noise["persistence"]).reshape(depth, width)
        if kind == "plateau":
            perturbation *= 1 - shape
        values += relief * (shape + 0.25 * perturbation)
        if kind == "island":
            edge = np.clip((1 - np.maximum(abs(nx), abs(nz))) * 3, 0, 1)
            island_limit = (water - bounds.min.y) * edge + relief * edge
            values = np.minimum(values, island_limit)
    coarse = _interpolate(values, min(65, width), min(65, depth))
    delta = _erode(coarse, config["erosion"], derive_seed(seed, "site.erosion", kind)) - coarse
    values += _interpolate(delta, width, depth)
    if kind == "island" and relief:
        values = np.minimum(values, island_limit)
    heights = (np.floor(np.clip(values, 0, height) + 0.5) + bounds.min.y).astype(np.int32)
    heights.flags.writeable = delta.flags.writeable = False
    return TerrainField(bounds, bounds.min.y + height, kind, seed, config, heights, delta, water)
