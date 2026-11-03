"""属性・予約と領域分割の生成契約。"""

import hashlib
import json
import random
import weakref
from dataclasses import replace

import numpy as np
import pytest

from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GenerationLimits, generate, generate_regions
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.operations.base import derive_seed
from mcblueprint.operations.randomness import PaletteSampler, first_draws
from mcblueprint.schema import load_schema
from mcblueprint.volume import BlockVolume, CellAttributes, RegionVolume


def blueprint(operations, **extra):
    return load_blueprint_dict({
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "領域試験",
        "operations": operations, **extra,
    })


def normalized(bp, size, reverse=False, halo=3):
    cells = {}
    for region in generate_regions(
        bp, limits=GenerationLimits((size, None, size), halo), reverse=reverse,
    ):
        for pos, state in region.volume:
            if region.core.contains(pos):
                assert pos not in cells
                cells[pos] = state, region.volume.attributes(pos)
    return cells


def test_core_size_order_and_halo_do_not_change_mix_or_attributes():
    bp = blueprint([
        {"type": "fill", "from": [-5, 0, -4], "to": [65, 2, 35], "palette": "wall",
         "id": "wall", "role": "wall_main", "element": "house.wall", "face": "north",
         "source": "spec.features"},
        {"type": "replace", "from": [10, 1, 0], "to": [40, 1, 35], "match": "stone",
         "block": "bricks", "role": "trim", "element": "house.trim"},
    ], palettes={"wall": [{"block": "stone"}, {"block": "cobblestone", "weight": 2}]})
    expected = normalized(bp, 64)
    assert len({state for state, _ in expected.values()}) == 3
    for size in (16, 32, 64):
        for reverse in (False, True):
            assert normalized(bp, size, reverse, halo=0) == expected


@pytest.mark.parametrize("volume_type", [BlockVolume, RegionVolume])
def test_reserved_air_owner_and_atomic_conflicts(volume_type):
    volume = volume_type() if volume_type is BlockVolume else volume_type(
        AABB(Vec3(0, 0, 0), Vec3(0, 0, 0)),
    )
    pos = Vec3(0, 0, 0)
    air, stone = BlockState.of("air"), BlockState.of("stone")
    attrs = CellAttributes(role="path", reserved="circulation", owner="stairs")
    volume.set(pos, air, attrs)
    with pytest.raises(BlueprintError, match="予約セル"):
        volume.set(pos, stone, CellAttributes(owner="decor"))
    volume.set(pos, stone, CellAttributes(owner="decor"), on_conflict="skip")
    assert volume.get(pos) == air and volume.attributes(pos) == attrs
    with pytest.raises(BlueprintError, match="予約セル"):
        volume.set(pos, stone, CellAttributes(reserved="structure", owner="other"),
                   on_conflict="skip")
    volume.set(pos, stone, attrs)
    assert volume.get(pos) == stone and volume.attributes(pos) == attrs
    volume.set(pos, air, CellAttributes(role="path", owner="stairs"))
    assert volume.attributes(pos).reserved == "circulation"


def test_repeat_rotate_and_composite_inherit_reserved_air():
    bp = blueprint([
        {"type": "repeat", "count": 2, "offset": [40, 0, 0], "element": "stairs",
         "role": "frame", "reserved": "structure", "source": "spec.stairs",
         "operations": [{"type": "rotate", "angle": 90, "center": [0, 0, 0], "operations": [
             {"type": "fill", "from": [0, 0, 0], "to": [1, 0, 0], "block": "air",
              "face": "north"},
         ]}]},
    ])
    expected = normalized(bp, 64)
    assert expected == normalized(bp, 16, True)
    assert len(expected) == 4
    for state, attrs in expected.values():
        assert state.id == "minecraft:air"
        assert (attrs.role, attrs.element, attrs.reserved, attrs.face, attrs.source) == (
            "frame", "stairs", "structure", "east", "spec.stairs",
        )
    assert len({attrs.operation for _, attrs in expected.values()}) == 2


def test_building_inherits_attributes_and_rotation():
    bp = blueprint([
        {"type": "building", "position": [32, 0, 0], "width": 7, "depth": 9,
         "floors": 2, "floorHeight": 4, "angle": 90,
         "wall": "stone", "floor": "oak_planks", "roof": "oak_stairs",
         "stairs": "oak_stairs", "window": "glass", "door": "oak_door",
         "role": "frame", "element": "house", "reserved": "structure", "face": "north"},
    ])
    cells = normalized(bp, 16)
    assert cells == normalized(bp, 64, True)
    assert {attrs.face for _, attrs in cells.values()} == {"east"}
    assert all(attrs.owner == "house" and attrs.reserved == "structure"
               for _, attrs in cells.values())
    assert any(state.id == "minecraft:air" for state, _ in cells.values())


@pytest.mark.parametrize("region_path", [False, True])
def test_reservation_body_optional_decoration_and_explicit_collision(region_path):
    ops = [
        {"type": "fill", "from": [0, 0, 0], "to": [20, 0, 0], "block": "air",
         "reserved": "circulation", "element": "stairs", "owner": "stair-builder"},
        {"type": "set", "position": [0, 0, 0], "block": "stone",
         "reserved": "circulation", "owner": "stair-builder"},
        {"type": "fill", "from": [0, 0, 0], "to": [20, 0, 0], "block": "bricks",
         "onConflict": "skip"},
    ]
    bp = blueprint(ops)
    result = normalized(bp, 16) if region_path else {
        p: (s, generate(bp).attributes(p)) for p, s in generate(bp)
    }
    assert result[Vec3(0, 0, 0)][0].id == "minecraft:stone"
    assert result[Vec3(1, 0, 0)][0].id == "minecraft:air"
    ops[-1].pop("onConflict")
    with pytest.raises(BlueprintError, match="予約セル"):
        normalized(blueprint(ops), 16) if region_path else generate(blueprint(ops))


def test_legacy_palette_draws_and_return_type_are_preserved():
    bp = blueprint([
        {"type": "fill", "from": [0, 0, 0], "to": [30, 0, 0], "palette": "wall"},
    ], seed=123, palettes={"wall": [{"block": "stone"}, {"block": "bricks"}]})
    volume = generate(bp)
    assert type(volume) is BlockVolume
    rng = random.Random(123)
    assert [state for _, state in volume] == [bp.palettes["wall"].choose(rng) for _ in range(31)]


@pytest.mark.parametrize("kwargs", [
    {"region": (0, None, 64)}, {"region": (64, True, 64)}, {"region": (None, None, 64)},
    {"halo": -1}, {"halo": True}, {"max_memory_mb": 0}, {"max_memory_mb": float("nan")},
    {"max_memory_mb": float("inf")}, {"max_memory_mb": True},
    {"region": None}, {"region": (64, 64)},
])
def test_invalid_limits(kwargs):
    with pytest.raises(BlueprintError, match="limits"):
        GenerationLimits(**kwargs)


def test_memory_shrinks_core_and_fails_when_halo_cannot_fit():
    bp = blueprint([{"type": "fill", "from": [0, 0, 0], "to": [63, 7, 63],
                     "block": "stone"}])
    limits = GenerationLimits((64, None, 64), 1, 0.2)
    iterator = generate_regions(bp, limits=limits)
    first = next(iterator)
    assert first.core.size.x < 64
    assert first.volume.nbytes < limits.max_memory_mb * 1024 * 1024
    with pytest.raises(BlueprintError, match="最小core"):
        next(generate_regions(bp, limits=replace(limits, halo=64)))


def test_weather_memory_estimate_shrinks_automatic_core():
    bp = blueprint([{"type": "fill", "from": [0, 0, 0], "to": [30, 30, 30],
                     "block": "stone"}])
    limits = GenerationLimits((31, 31, 31), halo=0, max_memory_mb=4)

    region = next(generate_regions(bp, limits=limits))

    assert region.core.size.x < 31
    assert region.volume.region.volume * 384 <= 4 * 1024 * 1024


def test_stream_releases_regions_and_does_not_construct_whole_volume(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("全体volumeを作成した")
    monkeypatch.setattr(BlockVolume, "__init__", forbidden)
    bp = blueprint([{"type": "fill", "from": [0, 0, 0], "to": [127, 3, 63],
                     "block": "stone"}])
    iterator = generate_regions(bp, limits=GenerationLimits((16, None, 16), 0))
    count = total = 0
    refs = []
    while True:
        try:
            region = next(iterator)
        except StopIteration:
            break
        assert all(ref() is None for ref in refs)
        refs.append(weakref.ref(region.volume))
        count += 1
        total += len(region.volume)
        del region
    assert count == 32 and total == 128 * 4 * 64


def test_effective_halo_and_allocation_failure(monkeypatch):
    bp = blueprint([{"type": "fill", "from": [0, 0, 0], "to": [63, 1, 63],
                     "block": "stone"}])
    region = next(generate_regions(
        bp, limits=GenerationLimits((16, None, 16), 0), reference_radius=5,
    ))
    assert region.volume.region.max == Vec3(20, 1, 20)
    def fail(*args, **kwargs):
        raise MemoryError
    monkeypatch.setattr(RegionVolume, "__init__", fail)
    with pytest.raises(BlueprintError, match="メモリを確保"):
        next(generate_regions(bp))


def test_nonlocal_copy_is_rejected_even_outside_first_core_but_legacy_still_works():
    bp = blueprint([
        {"type": "set", "position": [0, 0, 0], "block": "stone"},
        {"type": "translate", "offset": [100, 0, 0], "operations": [
            {"type": "copy", "from": [0, 0, 0], "to": [0, 0, 0], "offset": [1, 0, 0]},
        ]},
    ])
    assert len(generate(bp)) == 1
    with pytest.raises(BlueprintError, match="copy"):
        next(generate_regions(bp))


def test_legacy_copy_preserves_source_attributes_and_accepts_explicit_overrides():
    bp = blueprint([
        {"type": "set", "position": [0, 0, 0], "block": "air",
         "role": "path", "reserved": "circulation", "element": "entry"},
        {"type": "copy", "from": [0, 0, 0], "to": [0, 0, 0], "offset": [1, 0, 0],
         "source": "copy.entry"},
    ])
    volume = generate(bp)
    copied = volume.attributes(Vec3(1, 0, 0))
    assert (copied.role, copied.reserved, copied.owner, copied.source) == (
        "path", "circulation", "entry", "copy.entry",
    )
    assert volume.get(Vec3(1, 0, 0)).id == "minecraft:air"


def test_attribute_schema_and_seed_contract():
    import jsonschema

    raw = {"formatVersion": 1, "minecraftVersion": "1.21.11", "name": "属性",
           "operations": [{"type": "set", "position": [0, 0, 0], "block": "air",
                           "id": "door", "reserved": "circulation", "owner": "entry",
                           "face": "north", "onConflict": "skip"}]}
    jsonschema.validate(raw, load_schema())
    raw["operations"][0]["reserved"] = "unknown"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, load_schema())
    expected = hashlib.sha256(json.dumps(
        [42, "generate", "塔"], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).digest()
    assert derive_seed(42, "generate", "塔") == int.from_bytes(expected, "big")


def test_vector_mt_matches_integer_seed_including_leading_zero_words():
    seeds = [0, 1, 2**32 - 1, 2**32, 2**64, 2**224, 2**256 - 1]
    seeds += [int.from_bytes(hashlib.sha256(str(i).encode()).digest(), 'big')
              for i in range(2000)]
    words = np.frombuffer(b''.join(seed.to_bytes(32, 'big') for seed in seeds), dtype='>u4')
    actual = first_draws(words.reshape(-1, 8))
    assert np.array_equal(actual, [random.Random(seed).random() for seed in seeds])


def test_sampler_eviction_and_small_memory_preserve_values():
    bp = blueprint([
        {"type": "fill", "from": [0, 0, 0], "to": [63, 0, 0], "palette": "wall"},
    ], palettes={"wall": [{"block": "stone"}, {"block": "bricks"}]})
    sampler = PaletteSampler(bp.bounds(), 256 * 1024)
    sampler.cache_limit = 64
    prefix = hashlib.sha256(b'prefix,')
    first = [(tile, choices.copy()) for tile, choices in sampler.tiles(
        bp.bounds(), prefix, bp.palettes['wall'],
    )]
    assert sampler._cache_bytes <= 64
    second = list(sampler.tiles(bp.bounds(), prefix, bp.palettes['wall']))
    assert all(a == b and np.array_equal(x, y)
               for (a, x), (b, y) in zip(first, second, strict=True))
    assert normalized(bp, 16, True) == normalized(bp, 64)
    limits = GenerationLimits((64, None, 64), 0, 0.2)
    assert sum(len(r.volume) for r in generate_regions(bp, limits=limits)) == 64


def test_bulk_fill_preserves_pre_vectorization_draws_and_transforms():
    bp = blueprint([
        {"type": "rotate", "center": [0, 0, 0], "angle": 90, "operations": [
            {"type": "fill", "from": [-17, -2, -3], "to": [34, 2, 4], "palette": "wall",
             "element": "塔", "face": "north"},
        ]},
    ], seed=123, palettes={"wall": [{"block": "stone", "weight": 0.3},
                                   {"block": "bricks", "weight": 1.7},
                                   {"block": "cobblestone", "weight": 0.1}]})
    expected = normalized(bp, 64)
    for pos, (state, attrs) in expected.items():
        stage = derive_seed(123, "generate", attrs.element)
        value = derive_seed(stage, attrs.operation, json.dumps([pos.to_list(), "mix"]))
        assert state == bp.palettes['wall'].choose(random.Random(value))
    assert normalized(bp, 16, True, 16) == expected
    assert normalized(bp, 32, False, 16) == expected


@pytest.mark.parametrize("region_path", [False, True])
@pytest.mark.parametrize("joined_roof", [False, True])
def test_join_children_keep_explicit_attributes_and_reservation(region_path, joined_roof):
    def house(position):
        return {"type": "building", "position": position, "width": 7, "depth": 10,
                "wall": "stone", "floor": "oak_planks", "roof": "oak_stairs",
                "window": "glass", "door": "oak_door"}
    first, second = house([0, 0, 0]), house([6, 0, 18])
    second['angle'] = 180
    first.update(role='frame', element='first', reserved='structure', owner='first.owner')
    second.update(role='wall_main', element='second', owner='second.owner')
    operations = [{"type": "join_buildings", "buildings": [first, second],
                   "faces": ["south", "north"], "wall": "bricks", "joinRoof": joined_roof,
                   "openings": [{"floor": 0, "offset": 3}],
                   "role": "trim", "element": "join"}]
    bp = blueprint(operations)
    if region_path:
        cells = normalized(bp, 16)
        assert cells == normalized(bp, 32, True)
    else:
        volume = generate(bp)
        cells = {pos: (state, volume.attributes(pos)) for pos, state in volume}
    first_attrs = cells[Vec3(0, 2, 0)][1]
    assert (first_attrs.role, first_attrs.element, first_attrs.reserved, first_attrs.owner) == (
        'frame', 'first', 'structure', 'first.owner',
    )
    second_attrs = cells[Vec3(0, 2, 18)][1]
    assert second_attrs.role == 'wall_main' and second_attrs.element == 'second'
    assert cells[Vec3(0, 2, 9)][1].element == 'join'
    operations.append({"type": "set", "position": [0, 2, 0], "block": "bricks"})
    with pytest.raises(BlueprintError, match='予約セル'):
        normalized(blueprint(operations), 16) if region_path else generate(blueprint(operations))
