"""NFR11向けの決定的な合成Blueprint fixtureを生成する。"""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFINITION_VERSION = "generation-profile-v1"
MEDIUM = (64, 48, 64)
HUGE = (256, 128, 256)
SEEDS = {
    "castle": 1101,
    "town": 1102,
    "terrain": 1103,
    "underground": 1104,
    "vegetation": 1105,
}


def _fill(start: tuple[int, int, int], end: tuple[int, int, int], block: str) -> dict[str, Any]:
    return {"type": "fill", "from": list(start), "to": list(end), "block": f"minecraft:{block}"}


def _box(size: tuple[int, int, int], seed: int, kind: str) -> list[dict[str, Any]]:
    xsize, ysize, zsize = size
    top = ysize - 1
    operations: list[dict[str, Any]] = []

    if kind == "castle":
        wall_height = min(36, top - 2)
        thickness = max(3, xsize // 16)
        operations.extend(
            [
                _fill((0, 1, 0), (xsize - 1, wall_height, thickness - 1), "stone_bricks"),
                _fill(
                    (0, 1, zsize - thickness), (xsize - 1, wall_height, zsize - 1), "stone_bricks"
                ),
                _fill(
                    (0, 1, thickness),
                    (thickness - 1, wall_height, zsize - thickness - 1),
                    "stone_bricks",
                ),
                _fill(
                    (xsize - thickness, 1, thickness),
                    (xsize - 1, wall_height, zsize - thickness - 1),
                    "stone_bricks",
                ),
            ]
        )
        tower = max(8, xsize // 8)
        tower_height = min(44, top - 2)
        for x, z in (
            (0, 0),
            (xsize - tower, 0),
            (0, zsize - tower),
            (xsize - tower, zsize - tower),
        ):
            operations.append(
                _fill((x, 1, z), (x + tower - 1, tower_height, z + tower - 1), "cobblestone")
            )
        keep = max(12, xsize // 4)
        cx, cz = (xsize - keep) // 2, (zsize - keep) // 2
        operations.extend(
            [
                _fill(
                    (cx, 1, cz), (cx + keep - 1, min(28, top - 2), cz + keep - 1), "stone_bricks"
                ),
            ]
        )

    elif kind == "town":
        rng = random.Random(seed)
        stride = max(12, xsize // 4)
        footprint = max(7, stride - 4)
        for z in range(3, zsize - footprint, stride):
            for x in range(3, xsize - footprint, stride):
                width = footprint - rng.randrange(0, 2)
                depth = footprint - rng.randrange(0, 2)
                height = min(12 + rng.randrange(0, 5), top - 3)
                operations.extend(
                    [
                        _fill((x, 1, z), (x + width - 1, 1, z + depth - 1), "cobblestone"),
                        _fill((x, 2, z), (x + width - 1, height, z), "oak_planks"),
                        _fill(
                            (x, 2, z + depth - 1),
                            (x + width - 1, height, z + depth - 1),
                            "oak_planks",
                        ),
                        _fill((x, 2, z + 1), (x, height, z + depth - 2), "oak_planks"),
                        _fill(
                            (x + width - 1, 2, z + 1),
                            (x + width - 1, height, z + depth - 2),
                            "oak_planks",
                        ),
                        _fill(
                            (x, height + 1, z),
                            (x + width - 1, height + 1, z + depth - 1),
                            "spruce_planks",
                        ),
                    ]
                )

    elif kind == "terrain":
        rng = random.Random(seed)
        step = max(4, xsize // 16)
        for z in range(0, zsize, step):
            for x in range(0, xsize, step):
                height = 5 + rng.randrange(max(1, min(18, top - 6)))
                operations.append(
                    _fill(
                        (x, 0, z),
                        (min(x + step - 1, xsize - 1), height - 1, min(z + step - 1, zsize - 1)),
                        "dirt",
                    )
                )
                operations.append(
                    _fill(
                        (x, height, z),
                        (min(x + step - 1, xsize - 1), height, min(z + step - 1, zsize - 1)),
                        "grass_block",
                    )
                )

    elif kind == "underground":
        depth = min(18, top - 1)
        operations.extend(
            [
                _fill((0, 0, 0), (xsize - 1, depth, zsize - 1), "stone"),
                _fill((1, 2, zsize // 2 - 2), (xsize - 2, 5, zsize // 2 + 2), "air"),
                _fill((xsize // 2 - 2, 2, 1), (xsize // 2 + 2, 5, zsize - 2), "air"),
                _fill((1, 2, zsize // 2), (xsize - 2, 2, zsize // 2), "oak_planks"),
            ]
        )

    elif kind == "vegetation":
        rng = random.Random(seed)
        count = max(12, xsize // 3)
        margin = 3
        for _ in range(count):
            x = rng.randrange(margin, xsize - margin)
            z = rng.randrange(margin, zsize - margin)
            trunk = rng.randrange(4, min(9, top - 1))
            operations.extend(
                [
                    _fill((x, 1, z), (x, trunk, z), "oak_log"),
                    _fill((x - 2, trunk - 1, z - 2), (x + 2, trunk + 1, z + 2), "oak_leaves"),
                    _fill((x - 1, trunk + 2, z - 1), (x + 1, trunk + 2, z + 1), "oak_leaves"),
                ]
            )

    operations.extend(
        [
            _fill((0, 0, 0), (0, 0, 0), "stone_bricks"),
            _fill((xsize - 1, top, zsize - 1), (xsize - 1, top, zsize - 1), "stone_bricks"),
        ]
    )
    return operations


def generate_profiles(output_dir: Path | None = None) -> list[dict[str, Any]]:
    output_dir = output_dir or ROOT / "benchmarks"
    fixtures = output_dir / "fixtures"
    record_path = output_dir / "generation-profiles.json"
    fixtures.mkdir(parents=True, exist_ok=True)
    profiles = []
    cases = [(kind, "medium", MEDIUM) for kind in SEEDS]
    cases.append(("castle", "huge", HUGE))
    for kind, scale, size in cases:
        name = f"profile-{kind}-{scale}"
        seed = SEEDS[kind]
        blueprint = {
            "formatVersion": 1,
            "minecraftVersion": "1.21.11",
            "name": name,
            "operations": _box(size, seed, kind),
        }
        path = fixtures / f"{name}.json"
        path.write_text(
            json.dumps(blueprint, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        profiles.append(
            {
                "name": name,
                "kind": kind,
                "scale": scale,
                "dimensions": list(size),
                "seed": seed,
                "definition_version": DEFINITION_VERSION,
                "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "viewer_reference": {
                    "scene": {
                        "castle": "castle",
                        "town": "town",
                        "terrain": "terrain",
                        "underground": "underground",
                        "vegetation": "vegetation",
                    }[kind],
                    "difference": (
                        "viewerの固定sceneはfixture-v1のscene JSON。"
                        "これは既存Operationによる生成入力であり、同一データではない。"
                    ),
                },
            }
        )
    record = {"schema_version": 1, "definition_version": DEFINITION_VERSION, "profiles": profiles}
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return profiles


if __name__ == "__main__":
    for profile in generate_profiles():
        print(f"{profile['name']}: {profile['input_sha256']}")
