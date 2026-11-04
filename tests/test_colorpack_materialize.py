"""人工の固定パックで条件選択・優先順・関係検査と既存経路を検証する。"""

import copy
import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest

from mcblueprint.blockdata import load_block_data, supported_versions
from mcblueprint.colorpack import validate_colorpack
from mcblueprint.colorpack_selection import (
    conditions,
    lookup,
    material_choice,
    quantile,
    relation_checks,
    target_color,
)
from mcblueprint.colors import face_linear, linear_to_oklab
from mcblueprint.errors import BlueprintError
from mcblueprint.materialize import materialize
from mcblueprint.validator import validate


@pytest.fixture
def raw():
    return json.loads((Path(__file__).parent / "data/colorpack/selection.json").read_text(
        encoding="utf-8"))


def cells(*roles, version="1.21.11"):
    return {"formatVersion": 1, "minecraftVersion": version, "name": "colorpack",
            "operations": [{"type": "set", "position": [i, 0, 0],
                            "block": {"role": role, "shape": shape, "state": {}}}
                           for i, (role, shape) in enumerate(roles)]}


def test_quantiles_and_same_component_level(raw):
    assert quantile([0, 2, 4, 6, 8], 0.15) == pytest.approx(1)
    assert quantile([0, 2, 4, 6, 8], 0.625) == 5
    assert quantile([0, 2, 4, 6, 8], 0) == 0
    assert quantile([0, 2, 4, 6, 8], 1) == 8
    distribution = raw["palettePrior"][0]["roles"]["wall_main"]
    settings = {"variation": 0, "saturation": 0.5, "accent": 0.5}
    assert target_color(distribution, settings, 1, "wall_main") == (0.4, 0, 0)
    settings["variation"] = 1
    with patch("mcblueprint.colorpack_selection.random.Random.random", return_value=0):
        assert target_color(distribution, settings, 1, "wall_main") == pytest.approx(
            (0.2, -0.1, -0.1))


def test_role_parent_search_and_smoothed_source(raw):
    full = {"style": "medieval", "buildingType": "house", "biomeLike": "plains"}
    parent = {"style": "medieval", "biomeLike": "plains"}
    distribution = copy.deepcopy(raw["palettePrior"][0]["roles"]["wall_main"])
    distribution.update(n=0, subjects=0, nEff=0,
                        support={"status": "smoothed", "sourceWhen": {}})
    raw["palettePrior"].append({"when": full, "roles": {"wall_main": distribution}})
    raw["palettePrior"].append({"when": parent, "roles": {"roof": copy.deepcopy(
        raw["palettePrior"][0]["roles"]["roof"])}})
    pack = validate_colorpack(raw)
    chain = conditions("medieval", "house", "plains")
    assert chain == (full, parent, {"style": "medieval"}, {})
    assert lookup(pack.palette_prior, chain, "wall_main")[1] == {
        "source": "pack", "when": full, "supportSourceWhen": {}}
    assert lookup(pack.palette_prior, chain, "roof")[1]["when"] == parent
    assert lookup(pack.palette_prior, chain, "frame")[0] is None
    assert conditions("medieval", "town", "unknown") == ({"style": "medieval"}, {})
    assert conditions("unknown", "house", "plains") == ({},)
    assert conditions("medieval", "house", "minecraft:plains") == chain


def test_probability_boundary_parent_and_bias(raw):
    rows = raw["materialPrior"]
    for value, expected in ((0, "stone"), (0.749, "stone"), (0.75, "wood"), (0.999, "wood")):
        with patch("mcblueprint.colorpack_selection.random.Random.random", return_value=value):
            assert material_choice(rows, ({},), "wall_main", ["wood", "stone"], 0)[0] == expected
    rows.insert(0, {"when": {"style": "medieval"},
                    "roles": {"wall_main": {"glass": 1}}})
    choice, source = material_choice(rows, ({"style": "medieval"}, {}), "wall_main",
                                     ["stone", "wood"], 0)
    assert choice in ("stone", "wood") and source["when"] == {}
    assert material_choice(rows, ({},), "wall_main", ["brick"], 0)[0] is None
    choices = [material_choice(rows, ({},), "wall_main", ["stone", "wood"], seed)[0]
               for seed in range(200)]
    assert 125 < choices.count("stone") < 175


@pytest.mark.parametrize("version", supported_versions())
def test_pack_shapes_mix_precedence_and_missing_role(raw, version):
    pack = validate_colorpack(raw)
    plan = cells(("wall_main", "full"), ("wall_main", "stairs"), ("wall_main", "slab"),
                 ("accent", "full"), version=version)
    style = {"name": "medieval", "roles": {"wall_main": ["wood", "stone"], "accent": "wood"}}
    result = materialize(plan, style=style, colorpack=pack, building_type="house", seed=4)
    assert result == materialize(plan, style=style, colorpack=pack, building_type="house", seed=4)
    assert validate(result.blueprint) == []
    assert result.blueprint["palettes"]
    assert result.max_area_ratios == {"accent": pytest.approx(0.03)}
    assert result.selection_sources["accent"]["color"]["source"] == "d28"
    assert result.selection_sources["wall_main"]["material"]["source"] == "pack"
    registry = load_block_data(version)
    assert len({registry.get(s.id).family for s in result.roles["wall_main"].values()}) == 1
    explicit = materialize(plan, style=style, colorpack=pack, palette={"materials": {
        "wall_main": {"material": "wood", "color": [0.5, 0, 0]}}})
    assert explicit.selection_sources["wall_main"] == {
        "material": {"source": "explicit"}, "color": {"source": "explicit"}}
    color_only = materialize(plan, style=style, colorpack=pack, palette={"materials": {
        "wall_main": {"color": [0.5, 0, 0]}}})
    assert color_only.selection_sources["wall_main"]["material"]["source"] == "pack"
    material_only = materialize(plan, style=style, colorpack=pack, palette={"materials": {
        "wall_main": "wood"}})
    assert material_only.selection_sources["wall_main"]["color"]["source"] == "pack"


@pytest.mark.parametrize("version", supported_versions())
def test_pack_rejects_invalid_explicit_choices(raw, version):
    pack = validate_colorpack(raw)
    for request, match in (("minecraft:glass", "必要形状"), ("not_a_material", "候補"),
                           ("minecraft:oak_stairs[half=bad]", "固定IDの状態")):
        with pytest.raises(BlueprintError, match=match):
            materialize(cells(("wall_main", "stairs"), version=version), colorpack=pack,
                        palette={"materials": {"wall_main": request}})


def selected(a, b):
    return {"wall_main": ("a", {}, (a,) * 5), "roof": ("b", {}, (b,) * 5)}


def test_signed_relations_circular_hue_and_achromatic(raw):
    rule = raw["relationRule"][0]
    rule.update(dL=[0.1] * 5, dC=[0] * 5,
                dh={"origin": 0.02, "quantiles": [-0.01, -0.005, 0, 0.005, 0.01]})
    a = (0.4, 0.1 * math.cos(math.pi - 0.01), 0.1 * math.sin(math.pi - 0.01))
    b = (0.5, 0.1 * math.cos(-math.pi + 0.01), 0.1 * math.sin(-math.pi + 0.01))
    rule["dL"] = [0.09, 0.095, 0.1, 0.105, 0.11]
    rule["dC"] = [-0.01, -0.005, 0, 0.005, 0.01]
    settings = {"contrast": 0.5, "saturation": 0.5}
    errors, checks, _ = relation_checks(selected(a, b), [rule], ({},), settings)
    assert not errors
    assert all(c["applied"] and c["source"] == "pack" for c in checks)
    _, checks, _ = relation_checks(selected((0.4, 0, 0), b), [rule], ({},), settings)
    assert checks[-1]["reason"] == "achromatic" and not checks[-1]["applied"]
    errors, _, _ = relation_checks(selected(b, a), [rule], ({},), settings)
    assert any(e["metric"] == "dL" for e in errors)


def test_fixed_ids_fail_relation_without_reroll(raw):
    raw["relationRule"][0]["dL"] = [0.9] * 5
    plan = cells(("wall_main", "full"), ("roof", "full"))
    result = materialize(plan, colorpack=validate_colorpack(raw), palette={"materials": {
        "wall_main": "minecraft:stone", "roof": "minecraft:stone"}})
    assert result.attempts == 20
    assert result.fixed_roles == {"wall_main", "roof"}
    assert all(states["full"].id == "minecraft:stone" for states in result.roles.values())
    assert not result.blueprint["palettes"]
    assert any(d["source"] == "pack" and d["metric"] == "dL" for d in result.diagnostics)


def test_pack_order_seed_and_d28_equivalence(raw):
    plan = cells(("wall_main", "full"), ("roof", "full"))
    style = {"name": "medieval", "roles": {"wall_main": ["stone", "wood"], "roof": "wood"}}
    for key in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        child = copy.deepcopy(raw[key][0])
        child["when"] = {"style": "medieval"}
        raw[key].append(child)
    pack = validate_colorpack(raw)
    result = materialize(plan, style=style, colorpack=pack, seed=7)
    for key in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        raw[key].reverse()
    raw["materialPrior"][0]["roles"]["wall_main"] = {"wood": 0.25, "stone": 0.75}
    raw["palettePrior"][0]["roles"] = dict(reversed(list(raw["palettePrior"][0]["roles"].items())))
    style["roles"]["wall_main"].reverse()
    assert result == materialize(plan, style=style, colorpack=validate_colorpack(raw), seed=7)
    assert result.selection_sources["wall_main"]["color"]["when"] == {"style": "medieval"}
    assert len({materialize(cells(("wall_main", "full")), style=style, colorpack=pack, seed=s)
                .roles["wall_main"]["full"].id for s in range(12)}) > 1
    for key in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        raw[key] = []
    fallback = materialize(plan, style=style, colorpack=validate_colorpack(raw), seed=7)
    d28 = materialize(plan, style=style, seed=7)
    assert fallback.blueprint == d28.blueprint
    assert fallback.roles == d28.roles


def test_pair_parent_and_interval_mapping(raw):
    parent = copy.deepcopy(raw["relationRule"][0])
    parent["when"] = {"style": "medieval"}
    parent.update(dL=[0, 0.1, 0.2, 0.3, 0.4], dC=[-0.1, -0.05, 0, 0.05, 0.1])
    raw["relationRule"].append(parent)
    chain = conditions("medieval", "house", "plains")
    rule, source = lookup(validate_colorpack(raw).relation_rule, chain, ("roof", "wall_main"))
    assert rule["pair"] == ("wall_main", "roof")
    assert source["when"] == {"style": "medieval"}
    _, checks, _ = relation_checks(selected((0.4, 0, 0), (0.5, 0.05, 0)), [parent], chain,
                                   {"contrast": 0, "saturation": 1})
    assert (checks[0]["minimum"], checks[0]["maximum"]) == pytest.approx((0.1, 0.3))
    assert (checks[1]["minimum"], checks[1]["maximum"]) == pytest.approx((-0.15, 0.15))


@pytest.mark.parametrize("version", supported_versions())
def test_tinted_real_block_relation_and_walking(raw, version):
    pack = validate_colorpack(raw)
    plan = cells(("wall_main", "full"), ("roof", "full"), version=version)
    registry = load_block_data(version)
    actuals = []
    for biome in ("plains", "swamp"):
        result = materialize(plan, colorpack=pack, biome=biome, palette={"materials": {
            "wall_main": "minecraft:stone", "roof": "minecraft:grass_block"}})
        first = registry.get("minecraft:stone")
        second = registry.get("minecraft:grass_block")
        a = linear_to_oklab(face_linear(first, first.faces["side"], registry, biome, 2))
        b = linear_to_oklab(face_linear(second, second.faces["top"], registry, biome, 2))
        check = next(c for c in result.relation_checks if c["metric"] == "dL")
        assert check["actual"] == pytest.approx(b[0] - a[0])
        actuals.append(check["actual"])
    assert actuals[0] != actuals[1]
    walking = cells(("floor", "stairs"), ("floor", "slab"), version=version)
    walking["operations"][1]["block"]["state"] = {"type": "double"}
    result = materialize(walking, colorpack=pack, palette={"materials": {"floor": "wood"}})
    assert validate(result.blueprint) == []
    walking["operations"][0]["block"]["state"] = {"half": "top"}
    with pytest.raises(BlueprintError, match="歩行"):
        materialize(walking, colorpack=pack, palette={"materials": {"floor": "wood"}})


def test_missing_face_and_pack_d28_pair_sources(raw):
    plan = cells(("wall_main", "full"), ("roof", "full"), ("frame", "full"))
    result = materialize(plan, colorpack=validate_colorpack(raw), palette={"materials": {
        "wall_main": "minecraft:air", "roof": "minecraft:stone", "frame": "minecraft:stone"}})
    assert {d["source"] for d in result.diagnostics} == {"pack", "d28"}
    assert all(not check["applied"] and check["reason"] == "color_unavailable"
               for check in result.relation_checks)


def test_pack_retries_keep_smallest_normalized_violation(raw, monkeypatch):
    import importlib

    module = importlib.import_module("mcblueprint.materialize")
    original = module.relation_checks
    observed = []

    def record(selected, *args):
        result = original(selected, *args)
        if len(selected) == 2:
            errors = result[0]
            observed.append((len(errors), sum(e["violation"] for e in errors)))
        return result

    raw["relationRule"][0].update(dL=[0.99] * 5, dC=[-0.5, -0.25, 0, 0.25, 0.5],
                                  dh={"origin": 0, "quantiles": [-3, -1, 0, 1, 3]})
    monkeypatch.setattr(module, "relation_checks", record)
    result = materialize(cells(("wall_main", "full"), ("roof", "full")),
                         colorpack=validate_colorpack(raw), palette={"variation": 1, "materials": {
                             "wall_main": "minecraft:stone", "roof": "wood"}})
    assert result.attempts == 20
    score = len(result.diagnostics), sum(e["violation"] for e in result.diagnostics)
    assert score == min(observed)


def test_area_limit_uses_rule_role_and_its_parent_condition(raw):
    raw["accentRule"].extend([
        {"when": {}, "role": "trim", "maxAreaRatio": 0.2},
        {"when": {"style": "medieval"}, "role": "trim", "maxAreaRatio": 0.1},
    ])
    result = materialize(cells(("trim", "full"), ("wall_main", "full")),
                         colorpack=validate_colorpack(raw), building_type="house",
                         palette={"accent": 0.5})
    assert result.max_area_ratios == {"trim": 0.05, "accent": 0.04}
    assert result.selection_sources["trim"]["maxAreaRatio"] == {
        "source": "pack", "when": {"style": "medieval"}}
    assert "maxAreaRatio" not in result.selection_sources["wall_main"]


@pytest.mark.parametrize("version", supported_versions())
@pytest.mark.parametrize("block,applied", [("stone", False), ("red_concrete", True)])
@pytest.mark.parametrize("empty_pack", [False, True])
def test_d28_metric_records_distinguish_achromatic_hue(raw, version, block, applied, empty_pack):
    for key in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        raw[key] = []
    pack = validate_colorpack(raw) if empty_pack else None
    plan = cells(("wall_main", "full"), ("wall_secondary", "full"), version=version)
    result = materialize(plan, colorpack=pack, palette={"materials": {
        "wall_main": f"minecraft:{block}", "wall_secondary": f"minecraft:{block}"}})
    checks = {check["metric"]: check for check in result.relation_checks}
    assert set(checks) == {"dC", "dh"}
    assert all(check["source"] == "d28" and check["pair"] == ["wall_main", "wall_secondary"]
               for check in checks.values())
    assert checks["dC"]["applied"] and checks["dC"]["actual"] == 0
    assert checks["dh"]["applied"] is applied
    if applied:
        assert checks["dh"]["actual"] == 0
        assert checks["dh"]["minimum"] == 0
        assert checks["dh"]["maximum"] == math.pi / 3
    else:
        assert checks["dh"]["reason"] == "achromatic"
        assert "actual" not in checks["dh"]
    assert not result.diagnostics
    assert result.attempts == 1
    assert [op["block"] for op in result.blueprint["operations"]] == [f"minecraft:{block}"] * 2


def test_d28_lightness_records_success_and_failure():
    for roof, passes in (("minecraft:white_concrete", True), ("minecraft:stone", False)):
        result = materialize(cells(("wall_main", "full"), ("roof", "full")),
                             palette={"materials": {"wall_main": "minecraft:stone", "roof": roof}})
        check, = result.relation_checks
        assert check["source"] == "d28" and check["metric"] == "dL" and check["applied"]
        assert (check["minimum"] <= check["actual"] <= check["maximum"]) == passes
        assert bool(result.diagnostics) is not passes
