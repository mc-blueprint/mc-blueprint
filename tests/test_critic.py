from dataclasses import replace

import pytest

from mcblueprint.critic import (
    CriticAccumulator,
    CriticPlan,
    Facade,
    FacadeCell,
    StructuralRun,
    evaluate,
    plan_from_metadata,
)
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model import AABB, Vec3
from mcblueprint.model.block import BlockState
from mcblueprint.operations.transform import Transform
from mcblueprint.volume import CellAttributes, RegionVolume


def test_plan_from_metadata_expands_ranges_and_openings():
    plan = plan_from_metadata({
        "columns": [{"id": "house#1.frame.column#1", "from": [1, 0, 2],
                     "to": [1, 2, 2]}],
        "beams": [{"id": "house#1.frame.beam#1", "from": [0, 3, 2],
                   "to": [2, 3, 2]}],
        "decorations": [{"operation": "/house#1/trim", "element": "house#1.facade.north",
                         "face": "north"}],
        "facades": [{"id": "house#1.facade.north", "normal": [0, 0, -1],
                     "origin": [0, 0, 0], "uAxis": [1, 0, 0], "vAxis": [0, 1, 0],
                     "from": [0, 0, 0], "to": [2, 2, 0],
                     "openings": [{"id": "window#1", "from": [1, 1, 0],
                                   "to": [1, 1, 0]}]}],
    })

    assert plan.columns[0].cells == tuple(Vec3(1, y, 2) for y in range(3))
    assert plan.beams[0].cells == tuple(Vec3(x, 3, 2) for x in range(3))
    assert len(plan.facades[0].cells) == 9
    assert sum(cell.opening for cell in plan.facades[0].cells) == 1
    assert {cell.u for cell in plan.facades[0].cells} == {0, 1, 2}
    assert plan.decoration_operations == {
        "/house#1/trim": ("house#1.facade.north", "north")}


def _fixture(*, opening=False, missing=(), missing_structure=(), decoration=(),
             fixed=False, depth_map=None):
    bounds = AABB(Vec3(-2, 0, -1), Vec3(5, 5, 1))
    volume = RegionVolume(bounds)
    surface = []
    for y in range(4):
        for x in range(4):
            depth = (depth_map or {}).get((x, y), 0)
            pos = Vec3(x, y, depth)
            is_open = opening and (x, y) == (0, 0)
            surface.append(FacadeCell(pos, x, y, 0, is_open))
            if (x, y) not in missing and not is_open:
                volume.set(pos, BlockState("minecraft:stone"), CellAttributes(
                    role="wall_main", element="house#1.facade.north", face="north",
                    operation="decoration" if (x, y) in decoration else None,
                ))
    columns = (StructuralRun("house#1.frame", (
        Vec3(-1, 4, 0), Vec3(-1, 5, 0), Vec3(2, 4, 0), Vec3(2, 5, 0),
    )),)
    for x in (-1, 2):
        for y in (4, 5):
            pos = Vec3(x, y, 0)
            if pos not in missing_structure:
                volume.set(pos, BlockState("minecraft:oak_log"), CellAttributes(
                    role="frame", element="house#1.frame", face="north",
                    reserved="structure", owner="house#1.frame",
                ))
    plan = CriticPlan(columns, (), (Facade("house#1.facade.north", "north", tuple(surface)),),
                      {"decoration": ("house#1.facade.north", "north")}, (0.2, 0.4),
                      frozenset({"wall_main"}) if fixed else frozenset())
    return plan, GeneratedRegion(bounds, volume)


def _copy_volume(source):
    volume = RegionVolume(source.region)
    for pos, state in source:
        volume.set(pos, state, source.attributes(pos))
    return volume


def test_critic_reports_column_and_missing_facade_and_openings():
    plan, region = _fixture(missing={(2, 2)}, opening=True)
    result = evaluate(plan, [region])

    codes = {item["code"] for item in result.diagnostics}
    assert "E203" in codes
    assert result.metrics["facade_surface_missing"]["value"] == 1
    assert result.metrics["flat_ratio"]["denominator"] == 1
    assert "E201" in codes
    assert result.metrics["column_continuity"]["value"] == 1


def test_critic_marks_a_missing_expected_column_as_structural_error():
    plan, region = _fixture(missing_structure={Vec3(2, 5, 0)})
    result = evaluate(plan, [region])

    assert result.metrics["column_continuity"]["value"] == 0
    assert any(item["code"] == "E101" for item in result.diagnostics)


def test_critic_fails_when_no_core_observes_an_expected_facade():
    plan, region = _fixture()
    outside = GeneratedRegion(AABB(Vec3(-2, 0, -1), Vec3(-1, 5, 1)), region.volume)

    result = evaluate(plan, [outside])

    assert any(item["code"] == "E203" for item in result.diagnostics)


def test_critic_fails_when_a_core_omits_part_of_an_expected_column():
    plan, region = _fixture()
    partial = GeneratedRegion(AABB(Vec3(-2, 0, -1), Vec3(1, 5, 1)), region.volume)

    result = evaluate(plan, [partial])

    assert result.metrics["column_continuity"]["value"] < 1
    structural = next(item for item in result.diagnostics if item["code"] == "E101")
    assert any(item.get("reason") == "unobserved" for item in structural["violations"])


def test_critic_counts_decoration_and_ignores_fixed_role_mix():
    plan, region = _fixture(decoration={(0, 0), (1, 0), (2, 0), (3, 0)}, fixed=True)
    result = evaluate(plan, [region])

    assert result.metrics["decoration_density"][
        "house#1.facade.north:north"]["value"] == 25
    assert result.metrics["facade_mix"] == {}
    assert not any(item["code"] == "E305" for item in result.diagnostics)


def test_critic_uses_partial_windows_and_round_segment_baselines():
    depth_map = {(x, y): x % 2 for y in range(4) for x in range(4)}
    plan, region = _fixture(depth_map=depth_map)
    result = evaluate(plan, [region])
    assert result.metrics["flat_ratio"]["value"] == 0
    assert not any(item["code"] == "E201" for item in result.diagnostics)

    cells = []
    for segment in (0, 1):
        for v in range(2):
            for u in range(2):
                z = -segment * 2
                pos = Vec3(u, v, z)
                cells.append(FacadeCell(pos, u, v, segment * 2, segment=segment))
    volume = RegionVolume(AABB(Vec3(0, 0, -2), Vec3(3, 3, 0)))
    for cell in cells:
        volume.set(cell.pos, BlockState("minecraft:stone"), CellAttributes(
            role="wall_main", element="house#1.facade.north", face="north"))
    round_plan = CriticPlan(plan.columns, (),
                            (Facade("house#1.facade.north", "north", tuple(cells)),),
                            {}, (0, 1))
    round_result = evaluate(round_plan, [GeneratedRegion(volume.region, volume)])
    assert round_result.metrics["flat_ratio"]["denominator"] == 2
    assert round_result.metrics["flat_ratio"]["value"] == 1


def test_critic_handles_rotated_negative_coordinate_facade():
    bounds = AABB(Vec3(-2, 0, -5), Vec3(2, 4, -2))
    volume = RegionVolume(bounds)
    cells = []
    for y in range(4):
        for z in range(-5, -1):
            pos = Vec3(0, y, z)
            cells.append(FacadeCell(pos, z, y, 0))
            volume.set(pos, BlockState("minecraft:stone"), CellAttributes(
                role="wall_main", element="tower#1.facade.east", face="east"))
    plan = CriticPlan((StructuralRun("tower#1.frame", (Vec3(-1, 0, -4),)),), (),
                      (Facade("tower#1.facade.east", "east", tuple(cells)),), {}, (0, 1))
    result = evaluate(plan, [GeneratedRegion(bounds, volume)])
    assert result.metrics["flat_ratio"]["denominator"] == 2
    assert result.metrics["flat_ratio"]["value"] == 1


def test_critic_respects_core_and_memory_budget():
    plan, region = _fixture()
    left = GeneratedRegion(AABB(Vec3(-2, 0, -1), Vec3(1, 5, 1)), region.volume)
    right = GeneratedRegion(AABB(Vec3(2, 0, -1), Vec3(5, 5, 1)), region.volume)
    split = evaluate(plan, [right, left])
    whole = evaluate(plan, [region])
    assert split.metrics == whole.metrics
    assert evaluate(plan, [right, left, left]).metrics == whole.metrics

    with pytest.raises(BlueprintError, match="面要約"):
        evaluate(plan, [region], max_memory_mb=0.000001)


def test_critic_uses_weathered_core_instead_of_stale_halo():
    plan, whole = _fixture(decoration={(0, 0), (1, 0), (2, 0), (3, 0)})
    left_volume = _copy_volume(whole.volume)
    right_volume = _copy_volume(whole.volume)
    for pos, _state in whole.volume:
        attr = whole.volume.attributes(pos)
        if attr.face == "north" and attr.element == "house#1.facade.north":
            if pos.x >= 2:
                left_volume.set(pos, BlockState("minecraft:stone_bricks"), attr)
            if pos.x < 2:
                right_volume.set(pos, BlockState("minecraft:air"), attr)
    left = GeneratedRegion(AABB(Vec3(-2, 0, -1), Vec3(1, 5, 1)), left_volume)
    right = GeneratedRegion(AABB(Vec3(2, 0, -1), Vec3(5, 5, 1)), right_volume)

    assert evaluate(plan, [right, left]).metrics == evaluate(plan, [whole]).metrics


def test_outside_depth_core_is_merged_before_finalizing():
    plan, region = _fixture(depth_map={(x, y): -(x % 2) for x in range(4) for y in range(4)})
    facade = plan.facades[0]
    plan = replace(plan, facades=(replace(facade, cells=tuple(
        replace(c, pos=Vec3(c.pos.x, c.pos.y, 0)) for c in facade.cells)),))
    for cell in plan.facades[0].cells:
        region.volume.set(cell.pos, BlockState.of("stone"), CellAttributes(
            role="wall_main", element=facade.element, face=facade.face))
    front = GeneratedRegion(AABB(Vec3(-2, 0, -1), Vec3(5, 5, -1)), region.volume)
    back = GeneratedRegion(AABB(Vec3(-2, 0, 0), Vec3(5, 5, 1)), region.volume)
    accumulator = CriticAccumulator(plan)
    accumulator.consume(back)
    accumulator.consume(front)
    result = accumulator.finalize()
    assert result.metrics == evaluate(plan, [region]).metrics
    assert result.metrics["flat_ratio"]["value"] == 0
    assert result.metrics["facade_surface_missing"]["value"] == 0
    assert accumulator.finalize() == result


def test_only_explicit_decoration_operations_are_counted():
    plan, region = _fixture(decoration={(0, 0), (1, 0), (2, 0), (3, 0)})
    pos = Vec3(3, 0, 0)
    region.volume.set(pos, BlockState.of("stone"), replace(
        region.volume.attributes(pos), operation="decorative-looking-id"))
    result = evaluate(plan, [region])
    assert result.metrics["decoration_density"]["house#1.facade.north:north"]["numerator"] == 3
    assert evaluate(replace(plan, decoration_operations={}), [region]).metrics[
        "decoration_density"]["house#1.facade.north:north"]["value"] == 0


def test_decoration_metadata_is_used_for_final_cells():
    _plan, region = _fixture(decoration={(0, 0), (1, 0), (2, 0), (3, 0)})
    planning = {
        "columns": [{"id": "house#1.frame", "from": [-1, 4, 0], "to": [-1, 5, 0]}],
        "beams": [],
        "facades": [{"id": "house#1.facade.north", "normal": [0, 0, -1],
                     "origin": [0, 0, 0], "uAxis": [1, 0, 0],
                     "from": [0, 0, 0], "to": [3, 3, 0], "openings": []}],
        "decorations": [{"operation": "decoration", "element": "house#1.facade.north",
                         "face": "north"}],
    }
    result = evaluate(plan_from_metadata(planning, decoration_range=(0.2, 0.4)), [region])
    assert result.metrics["decoration_density"]["house#1.facade.north:north"] == {
        "value": 25, "numerator": 4, "denominator": 16}
    assert not any(d["code"] == "E202" for d in result.diagnostics)
    del planning["decorations"]
    assert plan_from_metadata(planning).decoration_operations == {}


def test_missing_planning_is_an_error():
    with pytest.raises(BlueprintError, match="Critic計画"):
        plan_from_metadata({"columns": [], "beams": []})


def test_outer_decoration_does_not_hide_missing_base_wall():
    plan, region = _fixture(missing={(2, 2)})
    region.volume.set(Vec3(2, 2, -1), BlockState.of("stone"), CellAttributes(
        role="trim", element="house#1.facade.north", face="north", operation="decoration"))
    result = evaluate(plan, [region])
    assert result.metrics["facade_surface_missing"]["value"] == 1
    assert any(d["code"] == "E203" for d in result.diagnostics)


def test_missing_wall_diagnostic_points_to_its_override():
    plan, region = _fixture()
    region.volume.set(Vec3(2, 2, 0), BlockState.of("air"), CellAttributes(
        role="override", element="override#remove-wall", source="overrides.remove-wall",
        operation="/override#remove-wall"))
    result = evaluate(plan, [region])
    diagnostic = next(d for d in result.diagnostics if d["code"] == "E203")
    assert "overrides[remove-wall]" in diagnostic["repairPaths"]


def test_empty_decoration_plan_is_zero_density_with_existing_wall():
    plan, region = _fixture()
    result = evaluate(replace(plan, decoration_operations={}), [region])
    assert result.metrics["decoration_density"]["house#1.facade.north:north"] == {
        "value": 0, "numerator": 0, "denominator": 16}
    assert any(d["code"] == "E202" for d in result.diagnostics)


@pytest.mark.parametrize("decoration", (False, True))
def test_nonair_base_wall_without_attributes_is_a_failure(decoration):
    plan, region = _fixture()
    pos = Vec3(2, 2, 0)
    region.volume.set(pos, BlockState.of("stone"), CellAttributes())
    if decoration:
        region.volume.set(pos + Vec3(0, 0, -1), BlockState.of("stone"), CellAttributes(
            role="trim", element="house#1.facade.north", face="north"))
    result = evaluate(plan, [region])
    assert result.metrics["facade_surface_missing"]["value"] == 1


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_metadata_windows_follow_signed_local_axes(angle):
    transform = Transform.rotation(angle, Vec3(0, 0, 0)).then(
        Transform.translation(Vec3(-17, 0, -23)))
    normal = transform.apply_direction("north")
    box = transform.bounds(AABB(Vec3(0, 0, 0), Vec3(8, 3, 0)))
    origin = transform.apply(Vec3(0, 0, 0))
    axis = transform.apply(Vec3(1, 0, 0)) - origin
    plan = plan_from_metadata({
        "columns": [{"id": "frame", "from": [-17, 0, -23], "to": [-17, 0, -23]}],
        "beams": [], "facades": [{"id": "wall", "normal": list({
            "north": (0, 0, -1), "south": (0, 0, 1),
            "east": (1, 0, 0), "west": (-1, 0, 0)}[normal]),
            "origin": origin.to_list(), "uAxis": axis.to_list(),
            "from": box.min.to_list(), "to": box.max.to_list(), "openings": []}],
    })
    volume = RegionVolume(transform.bounds(AABB(Vec3(0, 0, -1), Vec3(8, 3, 0))))
    for x in range(9):
        for y in range(4):
            volume.set(transform.apply(Vec3(x, y, 0)), BlockState.of("stone"), CellAttributes(
                role="wall_main", element="wall", face=normal))
            if x in (0, 4):
                volume.set(transform.apply(Vec3(x, y, -1)), BlockState.of("stone"),
                           CellAttributes(role="trim", element="wall", face=normal))
    result = evaluate(plan, [GeneratedRegion(volume.region, volume)])
    assert result.metrics["flat_ratio"] == {"value": 1 / 3, "numerator": 1, "denominator": 3}
    assert not any(d["code"] == "E201" for d in result.diagnostics)

