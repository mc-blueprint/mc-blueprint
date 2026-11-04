"""接合の輪郭・所有者・回転・領域境界を実セルで検証する。"""

import copy
import random
from collections import deque

import pytest

from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GenerationLimits, generate, generate_regions
from mcblueprint.model import BlockState, Vec3
from mcblueprint.operations import ExecutionContext, build_operation
from mcblueprint.operations.transform import Transform
from mcblueprint.validator import validate_and_load
from mcblueprint.volume import BlockVolume


def building(element, position, **changes):
    return {"type": "building", "element": element, "position": position,
            "width": 7, "depth": 10, "floors": 1, "wall": "stone_bricks",
            "floor": "oak_planks", "roof": "oak_stairs", "stairs": "oak_stairs",
            "door": "oak_door", "window": "glass", **changes}


def junction():
    return {"type": "building_junction", "element": "junction#1", "owner": "junction#1",
            "buildings": [building("house#1", [0, 0, 0], floors=2),
                          building("house#2", [6, 0, 0], width=9)],
            "parts": [{"element": "house#1", "shape": "rectangle"},
                      {"element": "house#2", "shape": "rectangle"}],
            "contacts": [{"first": "house#1", "second": "house#2",
                          "from": [6, 0, 0], "to": [6, 4, 9]}],
            "nodes": [{"id": "a", "element": "house#1", "position": [5, 1, 7]},
                      {"id": "b", "element": "house#2", "position": [7, 1, 7]}],
            "passages": [{"from": "a", "to": "b"}], "roofPolicy": "envelope",
            "wall": "stone_bricks", "frame": "oak_log", "floor": "oak_planks",
            "stairs": "oak_stairs"}


def blueprint(raw):
    return {"formatVersion": 1, "minecraftVersion": "1.21.11", "name": "junction",
            "operations": [raw]}


def run(raw):
    bp, errors = validate_and_load(blueprint(raw))
    assert not errors, errors
    return bp.operations[0], generate(bp)


def test_different_width_height_ownership_openings_and_exposed_wall():
    op, volume = run(junction())
    assert volume.get(Vec3(6, 1, 7)) == BlockState.of("air")
    assert volume.get(Vec3(6, 3, 7)) == BlockState.of("stone_bricks")
    assert volume.get(Vec3(6, 4, 7)) == BlockState.of("oak_log")
    for point in op.shared:
        assert volume.attributes(point).owner == "junction#1"
    assert volume.attributes(Vec3(6, 7, 2)).owner != "junction#1"
    assert volume.get(Vec3(6, 7, 2)).id != "minecraft:air"


def test_v2_contacts_can_omit_the_foundation_floor():
    raw = junction()
    raw["contacts"][0]["from"][1] = 1
    op, volume = run(raw)
    assert volume.attributes(Vec3(6, 0, 7)).owner == "junction#1"
    assert volume.get(Vec3(6, 0, 7)).id != "minecraft:air"
    assert volume.get(Vec3(6, 1, 7)).id == "minecraft:air"
    assert Vec3(6, 0, 7) in op.shared


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_rotation_and_core_boundaries(angle):
    raw = junction()
    _, original = run(raw)
    transform = Transform.rotation(angle, Vec3(0, 0, 0)).then(
        Transform.translation(Vec3(-13, -2, 7)))
    wrapped = {"type": "translate", "rotation": angle, "offset": [-13, -2, 7],
               "operations": [raw]}
    bp, errors = validate_and_load(blueprint(wrapped))
    assert not errors
    expected = {transform.apply(p): transform.apply_state(s) for p, s in original}
    assert dict(generate(bp)) == expected
    actual = {}
    for region in generate_regions(bp, limits=GenerationLimits(region=(5, 5, 5))):
        actual.update((p, s) for p, s in region.volume if region.core.contains(p))
    assert actual == expected


def test_floor_difference_uses_supported_stairs():
    from mcblueprint.traversal import check_route

    raw = junction()
    raw["buildings"][1]["position"][1] = 1
    raw["contacts"][0].update({"from": [6, 1, 0], "to": [6, 5, 9]})
    raw["nodes"][0]["position"] = [4, 1, 7]
    raw["nodes"][1]["position"] = [8, 2, 7]
    op, volume = run(raw)
    assert op.steps
    assert volume.get(Vec3(5, 1, 7)).id == "minecraft:oak_stairs"
    assert volume.get(Vec3(6, 2, 7)).id == "minecraft:air"
    assert volume.get(Vec3(6, 1, 7)).id != "minecraft:air"
    assert check_route(volume, Vec3(4, 1, 7), Vec3(8, 2, 7),
                       "1.21.11").status == "reachable"


@pytest.mark.parametrize("shape", ("round", "square"))
def test_rectangle_to_tower(shape):
    from mcblueprint.traversal import check_route

    raw = junction()
    raw["buildings"][1] = {"type": "tower", "element": "tower#1", "position": [10, 0, 5],
                            "shape": shape, "radius": 4, "size": 9, "height": 11,
                            "wall": "stone_bricks", "floor": "oak_planks", "floors": 4,
                            "battlement": False, "stairs": {"block": "oak_stairs"}}
    raw["buildings"][1].pop("size" if shape == "round" else "radius")
    raw["parts"][1] = {"element": "tower#1", "shape": shape}
    raw["contacts"] = [{"first": "house#1", "second": "tower#1",
                        "from": [6, 0, 3 if shape == "round" else 1],
                        "to": [6, 8, 7 if shape == "round" else 9]}]
    raw["nodes"][0]["position"] = [5, 1, 5]
    raw["nodes"][1].update(element="tower#1", position=[7, 1, 5])
    op, volume = run(raw)
    assert volume.get(Vec3(6, 1, 5)).id == "minecraft:air"
    assert volume.get(Vec3(6, 7, 4)).id != "minecraft:air"
    assert all(volume.attributes(p).owner == "junction#1" for p in op.shared)
    for y in (1, 5, 9):
        assert check_route(volume, Vec3(5, 1, 5), Vec3(10, y, 5),
                           "1.21.11").status == "reachable"


@pytest.mark.parametrize("change", ("contact", "owner", "overlap", "node", "headroom"))
def test_invalid_contact_owner_overlap_and_passage(change):
    raw = junction()
    if change == "contact":
        raw["contacts"][0]["to"][1] = 7
    elif change == "owner":
        raw["owner"] = "house#1"
    elif change == "overlap":
        raw["buildings"][1]["position"][0] = 5
    elif change == "node":
        raw["passages"][0]["to"] = "missing"
    else:
        raw["nodes"][1]["position"][1] = 5
    with pytest.raises(BlueprintError):
        build_operation(raw, "junction")


def test_foreign_reservation_is_an_error():
    from mcblueprint.volume import CellAttributes

    op = build_operation(junction(), "junction")
    volume = BlockVolume()
    volume.set(Vec3(6, 3, 7), BlockState.of("stone"),
               CellAttributes(reserved="structure", owner="foreign"))
    with pytest.raises(BlueprintError, match="予約"):
        op.apply(ExecutionContext(volume, random.Random(0), {}))


def test_legacy_join_policy_uses_existing_operation():
    raw = junction()
    raw["buildings"] = [building("house#1", [0, 0, 0]),
                        building("house#2", [0, 0, 9])]
    raw["contacts"][0].update({"from": [0, 0, 9], "to": [6, 4, 9]})
    raw["nodes"][0]["position"] = [4, 1, 8]
    raw["nodes"][1]["position"] = [4, 1, 10]
    raw["roofPolicy"] = "join"
    # 北面の主要入口を接合帯に置く入力は従来契約でも拒否する。
    with pytest.raises(BlueprintError, match="主要入口"):
        build_operation(raw, "junction")


def test_input_is_not_mutated():
    raw = junction()
    before = copy.deepcopy(raw)
    run(raw)
    assert raw == before


@pytest.mark.parametrize("layout", ("L", "T", "courtyard"))
def test_composite_layouts_close_shared_walls_and_reach_contacts(layout):
    from mcblueprint.traversal import check_route

    raw = junction()
    if layout == "L":
        buildings = [building("a", [0, 0, 0]), building("b", [15, 0, 3], angle=90)]
        contacts = [("a", "b", [6, 0, 3], [6, 4, 9], [5, 1, 7], [7, 1, 7])]
    elif layout == "T":
        buildings = [building("a", [0, 0, 0], width=13, depth=7),
                     building("b", [3, 0, 6])]
        contacts = [("a", "b", [3, 0, 6], [9, 4, 6], [6, 1, 5], [6, 1, 7])]
    else:
        buildings = [building("n", [0, 0, 0], width=19, depth=7),
                     building("w", [0, 0, 6], depth=13),
                     building("e", [12, 0, 6], depth=13),
                     building("s", [6, 0, 12], depth=7)]
        contacts = [("n", "w", [0, 0, 6], [6, 4, 6], [3, 1, 5], [3, 1, 7]),
                    ("n", "e", [12, 0, 6], [18, 4, 6], [15, 1, 5], [15, 1, 7]),
                    ("w", "s", [6, 0, 12], [6, 4, 18], [5, 1, 15], [7, 1, 15]),
                    ("s", "e", [12, 0, 12], [12, 4, 18], [11, 1, 15], [13, 1, 15])]
    raw.update(buildings=buildings, contacts=[], nodes=[], passages=[],
               parts=[{"element": b["element"], "shape": "rectangle"} for b in buildings])
    for i, (a, b, lo, hi, start, end) in enumerate(contacts):
        raw["contacts"].append({"first": a, "second": b, "from": lo, "to": hi})
        raw["nodes"].extend([{"id": f"{i}.a", "element": a, "position": start},
                             {"id": f"{i}.b", "element": b, "position": end}])
        raw["passages"].append({"from": f"{i}.a", "to": f"{i}.b"})
    op, volume = run(raw)
    for _, _, _, _, start, end in contacts:
        assert check_route(volume, Vec3.from_seq(start), Vec3.from_seq(end),
                           "1.21.11").status == "reachable"
    assert all(volume.get(p).id != "minecraft:air" for p in op.shared if p not in op.passages)
    if layout == "courtyard":
        assert volume.get(Vec3(9, 1, 9)) is None


def test_legacy_policy_output_is_identical():
    raw = junction()
    raw["buildings"] = [building("house#1", [0, 0, 0]),
                        building("house#2", [6, 0, 18], angle=180)]
    raw["contacts"][0].update({"from": [0, 0, 9], "to": [6, 4, 9]})
    raw["nodes"][0]["position"] = [4, 1, 8]
    raw["nodes"][1]["position"] = [4, 1, 10]
    raw["roofPolicy"] = "join"
    op, volume = run(raw)
    expected = BlockVolume()
    op.legacy.apply(ExecutionContext(expected, random.Random(0), {}))
    assert dict(volume) == dict(expected)


def test_planned_roof_palettes_match_build_and_partitioned_emit():
    raw = junction()
    profiles, wrappers = [], []
    for item in raw["buildings"]:
        model = build_operation(item, item["element"])
        operations = model.expand()
        operations[-1].pop("block")
        operations[-1]["palette"] = "roof_mix"
        wrappers.append({"type": "translate", "element": item["element"],
                         "offset": item["position"], "operations": operations})
        profiles.append({"element": item["element"], "offset": item["position"],
                         "params": {"width": model.width, "depth": model.depth,
                                    "floors": model.floors, "floor_height": model.floor_height}})
    raw.update(buildings=wrappers, profiles=profiles)
    data = blueprint(raw)
    data.update(seed=42, palettes={"roof_mix": [
        {"block": "oak_stairs", "weight": 1}, {"block": "spruce_stairs", "weight": 1}]})
    bp, errors = validate_and_load(data)
    assert not errors
    expected = dict(generate(bp))
    for reverse in (False, True):
        actual = {}
        for region in generate_regions(bp, limits=GenerationLimits(region=(5, 5, 5), halo=0),
                                       reverse=reverse):
            actual.update((p, s) for p, s in region.volume if region.core.contains(p))
        assert actual == expected


def test_planned_join_reuses_the_existing_roof_geometry():
    raw = junction()
    raw["buildings"] = [building("house#1", [0, 0, 0]),
                        building("house#2", [6, 0, 18], angle=180)]
    raw["contacts"][0].update({"from": [0, 0, 9], "to": [6, 4, 9]})
    raw["nodes"][0]["position"] = [4, 1, 8]
    raw["nodes"][1]["position"] = [4, 1, 10]
    raw["roofPolicy"] = "join"
    native = build_operation(raw, "native")
    expected = BlockVolume()
    ctx = ExecutionContext(expected, random.Random(0), {})
    for roof in native.legacy._roof_operations:
        ctx.execute(roof)
    profiles, wrappers = [], []
    for item in raw["buildings"]:
        part = build_operation(item, item["element"])
        wrappers.append({"type": "translate", "element": item["element"],
                         "offset": item["position"], "rotation": item.get("angle", 0),
                         "operations": part.expand()})
        profiles.append({"element": item["element"], "offset": item["position"],
                         "params": {"width": part.width, "depth": part.depth,
                                    "floors": part.floors, "floor_height": part.floor_height,
                                    "angle": item.get("angle", 0)}})
    raw.update(buildings=wrappers, profiles=profiles, roof="oak_stairs")
    op, _ = run(raw)
    actual = op.roof_cells(ExecutionContext(BlockVolume(), random.Random(0), {}))
    assert actual == {p: s for p, s in expected if p not in op.shared and p not in op.passages}


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
@pytest.mark.parametrize("floor_height", (4, 5))
def test_unequal_roof_heights_close_the_attic_to_outside_air(angle, floor_height):
    raw = junction()
    raw["buildings"][0]["floorHeight"] = floor_height
    wrapped = {"type": "translate", "rotation": angle, "offset": [-3, -2, 4],
               "operations": [raw]}
    bp, errors = validate_and_load(blueprint(wrapped))
    assert not errors, errors
    volume = generate(bp)
    transform = Transform.rotation(angle, Vec3(0, 0, 0)).then(
        Transform.translation(Vec3(-3, -2, 4)))
    start = transform.apply(Vec3(7, 5, 5))
    assert volume.get(start) is None or volume.get(start).id == "minecraft:air"
    bounds = bp.bounds()
    seen, pending = {start}, deque([start])
    while pending:
        point = pending.popleft()
        assert bounds.contains(point), f"屋根裏から外気へ到達: {point}"
        for delta in (Vec3(1, 0, 0), Vec3(-1, 0, 0), Vec3(0, 1, 0),
                      Vec3(0, -1, 0), Vec3(0, 0, 1), Vec3(0, 0, -1)):
            neighbor = point + delta
            state = volume.get(neighbor)
            if neighbor not in seen and (state is None or state.id == "minecraft:air"):
                seen.add(neighbor)
                pending.append(neighbor)
    actual = {}
    for region in generate_regions(bp, limits=GenerationLimits(region=(5, 5, 5), halo=0)):
        actual.update((p, s) for p, s in region.volume if region.core.contains(p))
    assert actual == dict(volume)
