"""生成済み領域へ決定的な劣化を適用する。"""

from __future__ import annotations

import json
import math
import random
from collections import deque
from collections.abc import Mapping
from typing import Any

from mcblueprint.blockdata import BlockData
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import Vec3
from mcblueprint.operations.base import derive_seed

_NEIGHBORS = (
    (Vec3(1, 0, 0), "east"), (Vec3(-1, 0, 0), "west"),
    (Vec3(0, 1, 0), "top"), (Vec3(0, -1, 0), "bottom"),
    (Vec3(0, 0, 1), "south"), (Vec3(0, 0, -1), "north"),
)
_AIR = BlockState("minecraft:air")
_AIR_IDS = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
_WATER = {"minecraft:water", "minecraft:flowing_water"}
_NON_SOLID = _AIR_IDS | _WATER


def _random(seed: int, element: str | None, pos: Vec3, purpose: str) -> float:
    stage = derive_seed(seed, "weather", element or "")
    value = derive_seed(stage, purpose, json.dumps(pos.to_list(), separators=(",", ":")))
    return random.Random(value).random()


def _water_distances(volume, radius: int) -> dict[Vec3, int]:
    distances: dict[Vec3, int] = {}
    queue: deque[Vec3] = deque()
    for pos, state in volume:
        if state.id in _WATER:
            distances[pos] = 0
            queue.append(pos)
    while queue:
        pos = queue.popleft()
        distance = distances[pos]
        if distance == radius:
            continue
        for offset, _ in _NEIGHBORS:
            adjacent = pos + offset
            if volume.region.contains(adjacent) and adjacent not in distances:
                distances[adjacent] = distance + 1
                queue.append(adjacent)
    return distances


def _unit(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"weathering.{name} は有限の数値が必要です")
    if not 0 <= value <= 1:
        raise ValueError(f"weathering.{name} は0..1の範囲が必要です")
    return float(value)


def required_halo(config: Mapping[str, Any]) -> int:
    """weathering設定が参照する最大半径を返す。"""
    water = config.get("moss", {}).get("water_proximity", {}).get("radius", 0)
    ground = config.get("moss", {}).get("ground_proximity", {}).get("height", 0)
    if type(water) is not int or water < 0:
        raise ValueError("weathering.moss.water_proximity.radius は0以上の整数が必要です")
    if type(ground) is not int or ground < 0:
        raise ValueError("weathering.moss.ground_proximity.height は0以上の整数が必要です")
    needs_exposure = (
        config.get("vegetation", 0) or config.get("missing_blocks", 0)
        or config.get("moss", {}).get("north_facing", 0)
        or config.get("cracks", {}).get("amount", 0)
    )
    return max(water, ground, 1 if needs_exposure else 0)


def apply_weather(
    region: GeneratedRegion,
    block_data: BlockData,
    *,
    seed: int,
    config: Mapping[str, Any],
    ground_height: int | Mapping[tuple[int, int], int],
    randomness: float = 0.0,
    max_memory_mb: float = 2048,
) -> tuple[dict[str, Any], ...]:
    """snapshotからWeather候補を決めてcoreへ反映する。

    ground_heightは元地形または基準面のY座標（一定値、または(x,z)表）。
    configはconfig.weathering、randomnessは全体設定の乱数強度。
    """
    volume = region.volume
    age = _unit(config.get("age", 0), "age")
    randomness = _unit(randomness, "randomness")
    if (isinstance(max_memory_mb, bool) or not isinstance(max_memory_mb, int | float)
            or not math.isfinite(max_memory_mb) or max_memory_mb <= 0):
        raise ValueError("max_memory_mb は有限の正数が必要です")
    if volume.region.volume * 384 > max_memory_mb * 1024 * 1024:
        raise BlueprintError("Weatherの領域・距離場・候補が max_memory_mb に収まりません")
    moss = config.get("moss", {})
    cracks = config.get("cracks", {})
    moss_amount = _unit(moss.get("amount", 0), "moss.amount")
    crack_amount = _unit(cracks.get("amount", 0), "cracks.amount")
    missing_amount = _unit(config.get("missing_blocks", 0), "missing_blocks")
    vegetation_amount = _unit(config.get("vegetation", 0), "vegetation")
    water = moss.get("water_proximity", {})
    ground = moss.get("ground_proximity", {})
    water_weight = _unit(water.get("weight", 0), "moss.water_proximity.weight")
    ground_weight = _unit(ground.get("weight", 0), "moss.ground_proximity.weight")
    north_weight = _unit(moss.get("north_facing", 0), "moss.north_facing")
    crack_exposure = _unit(cracks.get("exposure", 0), "cracks.exposure")
    water_radius = water.get("radius", 0)
    ground_range = ground.get("height", 0)
    if type(water_radius) is not int or water_radius < 0:
        raise ValueError("weathering.moss.water_proximity.radius は0以上の整数が必要です")
    if type(ground_range) is not int or ground_range < 0:
        raise ValueError("weathering.moss.ground_proximity.height は0以上の整数が必要です")
    results: list[tuple[Vec3, BlockState, Any]] = []
    plants: list[tuple[Vec3, BlockState, Any, Vec3]] = []
    bounds = volume.region
    distances = _water_distances(volume, water_radius) if water_radius else {}
    diagnostics: list[dict[str, Any]] = []

    for y in range(region.core.min.y, region.core.max.y + 1):
        for z in range(region.core.min.z, region.core.max.z + 1):
            for x in range(region.core.min.x, region.core.max.x + 1):
                pos = Vec3(x, y, z)
                original = volume.get(pos)
                if original is None:
                    if volume.attributes(pos).reserved is not None:
                        continue
                    below = volume.get(pos + Vec3(0, -1, 0))
                    below_attr = volume.attributes(pos + Vec3(0, -1, 0))
                    if (below_attr.role in ("floor", "path")
                            or below and below.id.endswith(("_stairs", "_door"))):
                        continue
                    if _random(seed, None, pos, "vegetation") >= vegetation_amount * age:
                        continue
                    for offset, face in _NEIGHBORS:
                        support = pos + offset
                        support_state = volume.get(support)
                        support_attr = volume.attributes(support)
                        if (support_state is None or support_attr.reserved is not None
                                or support_state.id in _NON_SOLID
                                or support_attr.role in ("floor", "path")
                                or support_state.id.endswith(("_stairs", "_door"))):
                            continue
                        if not bounds.contains(support):
                            continue
                        plant_ids = (("minecraft:short_grass", "minecraft:fern")
                                     if face == "bottom" else
                                     ("minecraft:vine", "minecraft:oak_leaves")
                                     if face in ("north", "south", "east", "west") else ())
                        candidates = []
                        for plant_id in plant_ids:
                            plant = block_data.get(plant_id)
                            if plant is None:
                                continue
                            candidate = BlockState(plant_id)
                            if plant_id == "minecraft:vine":
                                if face not in plant.properties:
                                    continue
                                candidate = candidate.with_property(face, "true")
                            elif plant_id == "minecraft:oak_leaves":
                                for key, value in (("distance", "1"), ("persistent", "true")):
                                    if key in plant.properties and value in plant.properties[key]:
                                        candidate = candidate.with_property(key, value)
                            if not block_data.check_state(candidate):
                                candidates.append(candidate)
                        if candidates:
                            choice = min(
                                int(_random(seed, None, pos, "plant-kind") * len(candidates)),
                                len(candidates) - 1,
                            )
                            plants.append((
                                pos, candidates[choice], volume.attributes(pos), support,
                            ))
                            break
                    continue
                attr = volume.attributes(pos)
                info = block_data.get(original.id)
                if info is None:
                    continue
                exposed = set()
                for offset, face in _NEIGHBORS:
                    adjacent = pos + offset
                    neighbor = volume.get(adjacent)
                    if bounds.contains(adjacent) and (
                        neighbor is None or neighbor.id in _AIR_IDS
                    ):
                        exposed.add(face)
                reserved = attr.reserved
                if reserved == "circulation":
                    continue
                if reserved == "structure" and attr.owner is None:
                    continue
                protected = (
                    attr.role in ("floor", "path")
                    or original.id.endswith(("_stairs", "_door"))
                )
                water_factor = 0.0
                if water_radius and pos in distances:
                    water_factor = water_weight * max(0.0, 1 - distances[pos] / water_radius)
                base_y = (ground_height[(x, z)] if isinstance(ground_height, Mapping)
                          else ground_height)
                ground_factor = (ground_weight * max(
                    0.0, 1 - max(0, y - base_y) / ground_range,
                ) if ground_range else 0.0)
                moss_probability = moss_amount * (
                    age + water_factor + ground_factor + (north_weight if "north" in exposed else 0)
                )
                crack_probability = crack_amount * (
                    age + crack_exposure * bool(
                        exposed.intersection({"top", "north", "south", "east", "west"}),
                    )
                )
                if moss_amount:
                    moss_probability = min(1.0, moss_probability + randomness * _random(
                        seed, attr.element, pos, "moss-noise",
                    ))
                else:
                    moss_probability = 0.0
                if crack_amount:
                    crack_probability = min(1.0, crack_probability + randomness * _random(
                        seed, attr.element, pos, "crack-noise",
                    ))
                else:
                    crack_probability = 0.0
                transition = info.weathering
                target = original
                can_moss = reserved is None
                crack_id = transition.get("crack")
                can_crack = reserved != "structure" or bool(crack_id and block_data.get(crack_id))
                moss_id = transition.get("moss")
                moss_hit = can_moss and bool(moss_id) and _random(
                    seed, attr.element, pos, "moss",
                ) < moss_probability
                crack_hit = can_crack and bool(crack_id) and _random(
                    seed, attr.element, pos, "crack",
                ) < crack_probability
                selected = "moss" if moss_hit and (
                    not crack_hit or moss_probability >= crack_probability
                ) else "crack" if crack_hit else None
                selected_id = (moss_id if selected == "moss" else crack_id) if selected else None
                if selected_id:
                    candidate = BlockState(selected_id, original.properties)
                    candidate_info = block_data.get(candidate.id)
                    preserves = candidate_info and all(
                        key in candidate_info.properties and value in candidate_info.properties[key]
                        for key, value in original.properties
                    )
                    if (candidate_info and candidate_info.shape == info.shape and preserves
                            and not block_data.check_state(candidate)):
                        target = candidate
                    else:
                        diagnostics.append({
                            "code": "weather_transition",
                            "position": pos.to_list(),
                            "source": original.to_string(),
                            "target": candidate.to_string(),
                        })
                side_exposed = bool(exposed.intersection({"north", "south", "east", "west"}))
                if (reserved is None and not protected and target == original and side_exposed
                        and _random(
                    seed, attr.element, pos, "missing",
                ) < missing_amount * age):
                    target = _AIR
                if target != original:
                    results.append((pos, target, attr))

    removed = {pos for pos, state, _ in results if state.id in _AIR_IDS}
    results.extend(
        (pos, state, attr) for pos, state, attr, support in plants if support not in removed
    )
    for pos, state, attr in results:
        volume.set(pos, state, attr)
    return tuple(diagnostics)
