from dataclasses import replace

import pytest

from mcblueprint.blockdata import BlockData, load_block_data
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import (
    GeneratedRegion,
    GenerationLimits,
    generate_regions,
)
from mcblueprint.loader import load_blueprint, load_blueprint_dict
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.volume import CellAttributes, RegionVolume
from mcblueprint.weather import _random, apply_weather, required_halo


def region_with_block(state: str, *, reserved: str | None = None):
    bounds = AABB(Vec3(0, 0, 0), Vec3(2, 2, 2))
    volume = RegionVolume(bounds)
    volume.set(Vec3(1, 1, 1), BlockState.parse(state),
               CellAttributes(role="wall_main", element="wall", reserved=reserved,
                              owner="frame" if reserved else None))
    return GeneratedRegion(bounds, volume)


def test_zero_settings_preserve_blocks_even_with_randomness():
    region = region_with_block("stone_bricks")
    before = tuple(region.volume)

    apply_weather(region, load_block_data("1.21.11"), seed=8, config={}, ground_height=0,
                  randomness=1)

    assert tuple(region.volume) == before


@pytest.mark.parametrize("ground", ("stone_bricks", "dirt"))
def test_ground_plants_obey_support_rules(ground):
    from mcblueprint.support import check_support

    bounds = AABB(Vec3(0, 0, 0), Vec3(4, 2, 4))
    volume = RegionVolume(bounds)
    for x in range(5):
        for z in range(5):
            volume.set(Vec3(x, 0, z), BlockState.of(ground))
    apply_weather(GeneratedRegion(bounds, volume), load_block_data("1.21.11"),
                  seed=42, config={"age": 1, "vegetation": 1}, ground_height=0)
    assert not check_support(volume)
    plants = [state for _, state in volume if state.id in
              ("minecraft:short_grass", "minecraft:fern")]
    assert bool(plants) == (ground == "dirt")


def test_weathering_transition_changes_only_the_core_cell():
    region = region_with_block("stone_bricks")

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "cracks": {"amount": 1, "exposure": 1}},
        ground_height=0,
    )

    assert region.volume.get(Vec3(1, 1, 1)).id == "minecraft:cracked_stone_bricks"
    assert region.volume.attributes(Vec3(1, 1, 1)).element == "wall"


def test_structure_reservation_allows_only_crack_transition():
    region = region_with_block("stone_bricks", reserved="structure")

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "moss": {"amount": 1}, "cracks": {"amount": 1}},
        ground_height=0,
    )

    assert region.volume.get(Vec3(1, 1, 1)).id == "minecraft:cracked_stone_bricks"
    assert region.volume.attributes(Vec3(1, 1, 1)).reserved == "structure"


def test_circulation_reservation_is_unchanged():
    region = region_with_block("stone_bricks", reserved="circulation")
    before = tuple(region.volume)

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "cracks": {"amount": 1}, "missing_blocks": 1},
        ground_height=0,
    )

    assert tuple(region.volume) == before


def test_ownerless_structure_reservation_is_preserved():
    region = region_with_block("stone_bricks")
    pos = Vec3(1, 1, 1)
    region.volume.set(pos, region.volume.get(pos),
                      CellAttributes(role="wall_main", element="wall",
                                     reserved="structure"))

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "cracks": {"amount": 1}},
        ground_height=0,
    )

    assert region.volume.get(pos).id == "minecraft:stone_bricks"


def test_moss_uses_blockdata_transition():
    region = region_with_block("cobblestone")

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "moss": {"amount": 1}},
        ground_height=0,
    )

    assert region.volume.get(Vec3(1, 1, 1)).id == "minecraft:mossy_cobblestone"


def test_invalid_weather_transition_is_reported_and_skipped():
    source = load_block_data("1.21.11")
    blocks = {info.id: info for info in source.blocks()}
    blocks["minecraft:cracked_stone_bricks"] = replace(
        blocks["minecraft:cracked_stone_bricks"], shape="stairs",
    )
    data = BlockData(source.version, blocks, source.tints, source.families)
    region = region_with_block("stone_bricks")

    diagnostics = apply_weather(
        region, data, seed=8,
        config={"age": 1, "cracks": {"amount": 1}},
        ground_height=0,
    )

    assert region.volume.get(Vec3(1, 1, 1)).id == "minecraft:stone_bricks"
    assert diagnostics[0]["code"] == "weather_transition"


def test_vegetation_uses_supported_exposed_air_cells():
    region = region_with_block("stone_bricks")

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "vegetation": 1},
        ground_height=0,
    )

    plants = {
        pos: state for pos, state in region.volume
        if state.id in {
            "minecraft:vine", "minecraft:oak_leaves",
            "minecraft:short_grass", "minecraft:fern",
        }
    }
    assert plants
    for pos, state in plants.items():
        if state.id in {"minecraft:short_grass", "minecraft:fern"}:
            supports = (pos + Vec3(0, -1, 0),)
        else:
            supports = (
                pos + Vec3(1, 0, 0), pos + Vec3(-1, 0, 0),
                pos + Vec3(0, 0, 1), pos + Vec3(0, 0, -1),
            )
        assert any(
            (support_state := region.volume.get(support)) is not None
            and support_state.id not in {
                "minecraft:air", "minecraft:cave_air", "minecraft:void_air",
                "minecraft:water", "minecraft:flowing_water",
            }
            for support in supports
        )


def test_weather_rejects_region_over_memory_budget():
    region = region_with_block("stone_bricks")

    with pytest.raises(BlueprintError, match="max_memory_mb"):
        apply_weather(
            region, load_block_data("1.21.11"), seed=8, config={},
            ground_height=0, max_memory_mb=0.0001,
        )


def test_required_halo_expands_beyond_default_region_margin():
    assert required_halo({
        "moss": {"water_proximity": {"radius": 20},
                 "ground_proximity": {"height": 4}},
        "cracks": {"amount": 0.2},
    }) == 20


def _weather_blueprint():
    return load_blueprint_dict({
        "formatVersion": 1,
        "minecraftVersion": "1.21.11",
        "name": "weather region invariance",
        "operations": [
            {"type": "fill", "from": [-25, 0, -25], "to": [25, 0, 25],
             "block": "stone_bricks", "role": "floor", "element": "floor"},
            {"type": "fill", "from": [-25, 1, -25], "to": [25, 6, -25],
             "block": "stone_bricks", "role": "wall", "element": "north", "face": "north"},
            {"type": "fill", "from": [-25, 1, 25], "to": [25, 6, 25],
             "block": "stone_bricks", "role": "wall", "element": "south", "face": "south"},
            {"type": "fill", "from": [-25, 1, -24], "to": [-25, 6, 24],
             "block": "stone_bricks", "role": "wall", "element": "west", "face": "west"},
            {"type": "fill", "from": [25, 1, -24], "to": [25, 6, 24],
             "block": "stone_bricks", "role": "wall", "element": "east", "face": "east"},
            {"type": "set", "position": [0, 1, -26],
             "block": "water", "role": "water", "element": "water"},
        ],
    })


def _weathered_cells(core_size: int, reverse: bool, settings: dict):
    blueprint = _weather_blueprint()
    cells = {}
    limits = GenerationLimits((core_size, core_size, core_size), halo=16)
    for region in generate_regions(
        blueprint, limits=limits, reference_radius=required_halo(settings), reverse=reverse,
    ):
        apply_weather(
            region, load_block_data(blueprint.minecraft_version), seed=blueprint.seed,
            config=settings, ground_height=0, randomness=0.25,
        )
        for pos, state in region.volume:
            if region.core.contains(pos):
                cells[pos] = state, region.volume.attributes(pos)
    return cells


def test_weather_is_invariant_across_core_sizes_and_traversal_order():
    settings = {
        "age": 0.7,
        "moss": {
            "amount": 0.65,
            "water_proximity": {"weight": 0.8, "radius": 18},
            "ground_proximity": {"weight": 0.4, "height": 6},
            "north_facing": 0.35,
        },
        "cracks": {"amount": 0.45, "exposure": 0.5},
        "missing_blocks": 0.2,
        "vegetation": 0.1,
    }
    expected = _weathered_cells(64, False, settings)

    assert required_halo(settings) == 18
    for size in (3, 16, 32, 64):
        for reverse in (False, True):
            assert _weathered_cells(size, reverse, settings) == expected


def test_weather_treats_world_bounds_as_exposed_air():
    bounds = AABB(Vec3(0, 0, 0), Vec3(2, 2, 2))
    volume = RegionVolume(bounds)
    pos = Vec3(1, 1, 0)
    volume.set(pos, BlockState.of("cobblestone"))
    region = GeneratedRegion(bounds, volume, bounds)

    apply_weather(region, load_block_data("1.21.11"), seed=8,
                  config={"age": 0, "moss": {"amount": 1, "north_facing": 1}},
                  ground_height=0)

    assert volume.get(pos).id == "minecraft:mossy_cobblestone"


def test_weather_does_not_treat_missing_in_bounds_reference_as_air():
    bounds = AABB(Vec3(0, 0, 0), Vec3(2, 2, 2))
    world_bounds = AABB(Vec3(0, 0, -1), Vec3(2, 2, 2))
    volume = RegionVolume(bounds)
    pos = Vec3(1, 1, 0)
    volume.set(pos, BlockState.of("cobblestone"))
    volume.set(Vec3(1, 1, 1), BlockState.of("stone"))
    region = GeneratedRegion(bounds, volume, world_bounds)

    apply_weather(region, load_block_data("1.21.11"), seed=8,
                  config={"age": 0, "moss": {"amount": 1, "north_facing": 1}},
                  ground_height=0)

    assert volume.get(pos).id == "minecraft:cobblestone"


def test_fixed_house_weather_matches_core_sizes_with_missing_and_vegetation():
    blueprint = load_blueprint("examples/house.json")
    settings = {"age": 1, "missing_blocks": 0.5, "vegetation": 1}
    expected = None
    for size in (3, 16, 64):
        cells = {}
        limits = GenerationLimits((size, size, size), halo=1)
        for region in generate_regions(
            blueprint, limits=limits, reference_radius=required_halo(settings),
        ):
            apply_weather(region, load_block_data(blueprint.minecraft_version),
                          seed=blueprint.seed, config=settings, ground_height=0)
            cells.update({
                pos: (state, region.volume.attributes(pos))
                for pos, state in region.volume if region.core.contains(pos)
            })
        if expected is None:
            expected = cells
        else:
            assert cells == expected


def _weather_sample_rates(kind: str, settings: dict, count: int = 256):
    high_positions = []
    low_positions = []
    bounds = AABB(Vec3(0, 0, 0), Vec3(count * 20 + 20, 2, 2))
    volume = RegionVolume(bounds)
    stone = BlockState.parse("stone_bricks" if kind == "crack" else "cobblestone")
    water = BlockState.parse("water")
    ground_height = {}
    for index in range(count):
        base = index * 20
        high = Vec3(base + 3, 1, 1)
        low = Vec3(base + 11, 1, 1)
        high_positions.append(high)
        low_positions.append(low)
        for label, pos in (("high", high), ("low", low)):
            volume.set(pos, stone, CellAttributes(role="wall", element=f"{label}-{index}"))
            ground_height[(pos.x, pos.z)] = 1 if label == "high" else -8
        if kind == "water":
            volume.set(high + Vec3(1, 0, 0), water)
            volume.set(low + Vec3(4, 0, 0), water)
        elif kind == "north":
            volume.set(low + Vec3(0, 0, -1), stone)
        elif kind == "crack":
            for offset in (
                Vec3(1, 0, 0), Vec3(-1, 0, 0), Vec3(0, 1, 0),
                Vec3(0, -1, 0), Vec3(0, 0, 1), Vec3(0, 0, -1),
            ):
                volume.set(low + offset, stone)
    region = GeneratedRegion(bounds, volume)
    apply_weather(
        region, load_block_data("1.21.11"), seed=2468, config=settings,
        ground_height=ground_height if kind == "ground" else 0,
    )
    mossy = "minecraft:mossy_cobblestone"
    cracked = "minecraft:cracked_stone_bricks"
    result_id = cracked if kind == "crack" else mossy
    high_rate = sum(volume.get(pos).id == result_id for pos in high_positions) / count
    low_rate = sum(volume.get(pos).id == result_id for pos in low_positions) / count
    return high_rate, low_rate


@pytest.mark.parametrize("kind", ["water", "ground", "north"])
def test_moss_probability_increases_with_local_conditions(kind):
    moss = {"amount": 0.8}
    if kind == "water":
        moss["water_proximity"] = {"weight": 0.8, "radius": 4}
    elif kind == "ground":
        moss["ground_proximity"] = {"weight": 0.8, "height": 4}
    else:
        moss["north_facing"] = 0.8

    high_rate, low_rate = _weather_sample_rates(
        kind, {"age": 0.1, "moss": moss},
    )

    assert high_rate - low_rate > 0.25


def test_crack_probability_increases_on_exposed_surfaces():
    high_rate, low_rate = _weather_sample_rates(
        "crack", {"age": 0.1, "cracks": {"amount": 0.8, "exposure": 0.8}},
    )

    assert high_rate - low_rate > 0.25


def test_bottom_only_exposure_does_not_increase_cracking():
    region = region_with_block("stone_bricks")
    pos = Vec3(1, 1, 1)
    for offset in (
        Vec3(1, 0, 0), Vec3(-1, 0, 0), Vec3(0, 1, 0),
        Vec3(0, 0, 1), Vec3(0, 0, -1),
    ):
        support = pos + offset
        region.volume.set(support, BlockState.parse("stone_bricks"))

    apply_weather(
        region, load_block_data("1.21.11"), seed=8,
        config={"age": 0, "cracks": {"amount": 1, "exposure": 1}},
        ground_height=0,
    )

    assert region.volume.get(pos).id == "minecraft:stone_bricks"


def test_missing_blocks_preserve_floors_stairs_doors_headroom_and_circulation():
    bounds = AABB(Vec3(0, 0, 0), Vec3(8, 5, 3))
    volume = RegionVolume(bounds)
    floor = Vec3(1, 0, 1)
    stairs = Vec3(3, 0, 1)
    door = Vec3(5, 1, 1)
    walk_air = Vec3(7, 2, 1)
    wall = Vec3(0, 2, 1)
    volume.set(floor, BlockState.parse("stone_bricks"),
               CellAttributes(role="floor", element="floor"))
    volume.set(stairs, BlockState.parse("oak_stairs"),
               CellAttributes(role="stairs", element="stairs"))
    volume.set(door, BlockState.parse("oak_door"),
               CellAttributes(role="door", element="door"))
    volume.set(walk_air, BlockState.parse("air"),
               CellAttributes(role="path", reserved="circulation", owner="stairs"))
    volume.set(wall, BlockState.parse("stone_bricks"),
               CellAttributes(role="wall", element="external"))
    core = GeneratedRegion(bounds, volume)

    apply_weather(
        core, load_block_data("1.21.11"), seed=8,
        config={"age": 1, "missing_blocks": 1, "vegetation": 1},
        ground_height=0,
    )

    assert volume.get(floor).id == "minecraft:stone_bricks"
    assert volume.get(stairs).id == "minecraft:oak_stairs"
    assert volume.get(door).id == "minecraft:oak_door"
    assert volume.get(Vec3(1, 1, 1)) is None
    assert volume.get(Vec3(3, 1, 1)) is None
    assert volume.get(Vec3(5, 2, 1)) is None
    assert volume.get(walk_air).id == "minecraft:air"
    assert volume.get(wall).id == "minecraft:air"


def test_higher_crack_probability_wins_when_both_transitions_hit():
    pos = Vec3(1, 1, 1)
    seed = next(seed for seed in range(1000) if (
        _random(seed, "wall", pos, "moss") < 0.5
        and _random(seed, "wall", pos, "crack") < 0.9
    ))
    region = region_with_block("stone_bricks")
    region.volume.set(pos, region.volume.get(pos),
                      CellAttributes(role="wall_main", element="wall"))

    apply_weather(
        region, load_block_data("1.21.11"), seed=seed,
        config={
            "age": 1,
            "moss": {"amount": 0.5},
            "cracks": {"amount": 0.9, "exposure": 0},
        },
        ground_height=0,
    )

    assert region.volume.get(pos).id == "minecraft:cracked_stone_bricks"


def test_equal_transition_probabilities_prefer_moss():
    pos = Vec3(1, 1, 1)
    seed = next(seed for seed in range(1000) if (
        _random(seed, "wall", pos, "moss") < 0.8
        and _random(seed, "wall", pos, "crack") < 0.8
    ))
    region = region_with_block("stone_bricks")
    region.volume.set(pos, region.volume.get(pos),
                      CellAttributes(role="wall_main", element="wall"))

    apply_weather(
        region, load_block_data("1.21.11"), seed=seed,
        config={
            "age": 1,
            "moss": {"amount": 0.8},
            "cracks": {"amount": 0.8, "exposure": 0},
        },
        ground_height=0,
    )

    assert region.volume.get(pos).id == "minecraft:mossy_stone_bricks"
