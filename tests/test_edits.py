import hashlib

import pytest

from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.edits import _reserved_bounds, apply_edits
from mcblueprint.errors import BlueprintError
from mcblueprint.operations.registry import build_operation
from mcblueprint.planning import plan_buildspec


def _scene():
    return {"formatVersion": 1, "size": [8, 8, 8],
            "sources": [{"id": 1, "element": "tower#1", "params": ["height"]}]}


def _edit(kind, **fields):
    return {"formatVersion": 1, "scene": "0" * 64,
            "edits": [{"id": "edit-1", "kind": kind, **fields}]}


def _blueprint():
    return {"formatVersion": 1, "operations": [
        {"type": "set", "position": [0, 0, 0], "block": "minecraft:stone"},
    ]}


def test_param_applies_without_mutating_source():
    source = {"formatVersion": 1, "minecraftVersion": "1.21.11",
              "archetype": "tower", "elements": {}}
    result = apply_edits(_scene(), _edit("param", element="tower#1", param="height", value=11),
                         source)
    assert result["elements"] == {"tower#1": {"height": 11}}
    assert source["elements"] == {}


def test_replace_and_delete_use_inclusive_regions():
    source = _blueprint() | {"overrides": []}
    scene = _scene() | {"origin": [-3, 2, -1]}
    edits = {"formatVersion": 1, "scene": "0" * 64, "edits": [
        {"id": "r", "kind": "replace", "region": [[1, 2, 3], [4, 5, 6]],
         "block": "minecraft:stone"},
        {"id": "d", "kind": "delete", "at": [0, 0, 0]},
    ]}
    result = apply_edits(scene, edits, source)
    assert result["overrides"] == [
        {"id": "r", "region": [[-2, 4, 2], [1, 7, 5]], "block": "minecraft:stone"},
        {"id": "d", "region": [[-3, 2, -1], [-3, 2, -1]], "block": "minecraft:air"},
    ]


def test_duplicate_conflicting_id_rejected():
    edits = _edit("delete", at=[0, 0, 0])
    edits["edits"].append({"id": "edit-1", "kind": "delete", "at": [1, 0, 0]})
    with pytest.raises(BlueprintError, match="競合"):
        apply_edits(_scene(), edits, _blueprint())


def test_out_of_bounds_and_unknown_param_rejected():
    with pytest.raises(BlueprintError, match="範囲外"):
        apply_edits(_scene(), _edit("delete", at=[8, 0, 0]),
                    _blueprint())
    with pytest.raises(BlueprintError, match="許可"):
        apply_edits(_scene(), _edit("param", element="tower#1", param="other", value=1),
                    {"formatVersion": 1, "minecraftVersion": "1.21.11",
                     "archetype": "tower", "scale": "small"})


def test_scene_digest_mismatch_rejected():
    edits = _edit("delete", at=[0, 0, 0])
    edits["scene"] = hashlib.sha256(b"different").hexdigest()
    with pytest.raises(BlueprintError, match="hash"):
        apply_edits(_scene(), edits, _blueprint(),
                    scene_bytes=b"scene")


def _reserved_operation(data, reservation, operation_id):
    if (data.get("reserved") == reservation
            and data.get("id") == operation_id):
        return data
    for child in data.get("operations", []):
        found = _reserved_operation(child, reservation, operation_id)
        if found:
            return found
    return None


@pytest.mark.parametrize(("reservation", "operation_id"), [
    ("circulation", "house#1.entry"),
    ("structure", "house#1.frame"),
])
def test_reserved_plan_regions_reject_edits(reservation, operation_id):
    source = {"formatVersion": 1, "minecraftVersion": "1.21.11",
              "archetype": "house", "scale": "small"}
    planned = plan_buildspec(resolve_buildspec(source))
    operation_data = _reserved_operation(planned.blueprint, reservation, operation_id)
    if operation_data is None:
        pytest.fail(f"予約Operationが見つかりません: {operation_id}")
    _, _, box = next(bounds for bounds in _reserved_bounds(planned.blueprint["operations"][0])
                     if bounds[0] == operation_id)
    scene_bounds = build_operation(planned.blueprint["operations"][0], "test").bounds()
    world = box.min.to_list()
    origin = scene_bounds.min.to_list()
    local = [world[i] - origin[i] for i in range(3)]
    scene = _scene() | {"origin": origin, "size": scene_bounds.size.to_list()}
    edit = _edit("replace", region=[local, local], block="minecraft:stone")

    with pytest.raises(BlueprintError, match=operation_id):
        apply_edits(scene, edit, source)
