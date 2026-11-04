"""共通投影の基準セル・ray・開口・ローカル評価窓の契約。"""

import copy

import pytest

from mcblueprint.facade_projection import plan_facade_runs, project_facades
from mcblueprint.model import AABB, Vec3
from mcblueprint.operations.transform import Transform


@pytest.mark.parametrize("angle", (0, 90, 180, 270))
def test_opening_projection_and_partial_windows_keep_signed_local_origin(angle):
    rotation = Transform.rotation(angle, Vec3(0, 0, 0))
    transform = rotation.then(Transform.translation(Vec3(-17, 2, -23)))
    bounds = transform.bounds(AABB(Vec3(0, 0, 0), Vec3(8, 3, 0)))
    opening = transform.apply(Vec3(2, 1, 2))
    metadata = {"params": {"angle": angle}, "facades": [{
        "id": "wall", "normal": rotation.apply(Vec3(0, 0, -1)).to_list(),
        "origin": transform.apply(Vec3(0, 0, 0)).to_list(),
        "uAxis": rotation.apply(Vec3(3, 0, 0)).to_list(),
        "from": bounds.min.to_list(), "to": bounds.max.to_list(),
        "openings": [{"from": opening.to_list(), "to": opening.to_list(),
                      "projection": {"u": [6, 6], "v": [1, 1]}}],
    }]}
    original = copy.deepcopy(metadata)
    facade, = project_facades(metadata)
    cells = {c.pos: c for c in facade.cells}
    assert metadata == original
    assert len(cells) == 36
    assert sum(c.opening for c in cells.values()) == 1
    projection = facade.wall_projection()
    for x in range(9):
        for y in range(4):
            pos = transform.apply(Vec3(x, y, 0))
            cell = cells[pos]
            assert (cell.u, cell.v) == (x, y)
            assert cell.window == (x // 4, 0)
            assert cell.opening == (x == 2 and y == 1)
            assert facade.depth_at(pos) == cell.depth
            exterior = transform.apply(Vec3(x, y, -2))
            assert facade.ray_key(pos) == facade.ray_key(exterior)
            assert facade.depth_at(exterior) - cell.depth == 2
            assert (facade.ray_key(pos) in projection) == (not cell.opening)


def test_round_steps_on_same_ray_remain_independent_coverage_cells():
    facade, = project_facades({"params": {"shape": "round", "radius": 5}, "facades": [{
        "id": "segment", "normal": [1, 0, 1], "origin": [5, 0, 0],
        "uAxis": [-5, 0, 5], "from": [0, 0, 0], "to": [5, 0, 5], "openings": [],
    }]})
    steps = [c for c in facade.cells if c.pos.x == 5]
    assert {c.pos for c in steps} == {Vec3(5, 0, z) for z in range(3)}
    assert len({facade.ray_key(c.pos) for c in steps}) == 1
    assert {c.depth for c in steps} == {0, 1, 2}
    assert all(c.depth == facade.depth_at(c.pos) for c in steps)
    assert len(facade.wall_projection()) < len(facade.cells)


@pytest.mark.parametrize("face", ("north", "south", "east", "west"))
@pytest.mark.parametrize("radius,expected", [
    (4, [(-4, -4, 2), (-3, -3, 3), (-2, 2, 4), (3, 3, 3), (4, 4, 2)]),
    (5, [(-5, -5, 2), (-4, -4, 3), (-3, -3, 4), (-2, 2, 5),
         (3, 3, 4), (4, 4, 3), (5, 5, 2)]),
    (8, [(-8, -8, 2), (-7, -7, 4), (-6, -5, 6), (-4, -3, 7), (-2, 2, 8),
         (3, 4, 7), (5, 6, 6), (7, 7, 4), (8, 8, 2)]),
])
def test_round_runs_keep_legacy_extreme_depths_and_placement_order(face, radius, expected):
    sign = -1 if face in ("north", "west") else 1
    assert plan_facade_runs({"radius": radius, "shape": "round"}, face) == [
        (start, end, sign * fixed) for start, end, fixed in expected]


@pytest.mark.parametrize("face,house,square", [
    ("north", (0, 8, 0), (-5, 5, -5)),
    ("south", (0, 8, 10), (-5, 5, 5)),
    ("east", (0, 10, 8), (-5, 5, 5)),
    ("west", (0, 10, 0), (-5, 5, -5)),
])
def test_house_and_square_runs_keep_full_side_and_fixed_coordinate(face, house, square):
    assert plan_facade_runs({"width": 9, "depth": 11}, face) == [house]
    assert plan_facade_runs({"radius": 5, "shape": "square"}, face) == [square]
