import pytest

from mcblueprint.critic import (
    CriticPlan,
    Facade,
    FacadeCell,
    StructuralRun,
    evaluate,
)
from mcblueprint.errors import BlueprintError
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model import AABB, Vec3
from mcblueprint.model.block import BlockState
from mcblueprint.volume import CellAttributes, RegionVolume


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

