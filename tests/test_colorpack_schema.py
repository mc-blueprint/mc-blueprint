"""配色パックの共有契約を数値fixtureだけで検証する。"""

import copy
import json
import math
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator

from mcblueprint.colorpack import parse_colorpack, validate_colorpack
from mcblueprint.errors import BlueprintError

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "mcblueprint"


@pytest.fixture
def pack():
    return json.loads((ROOT / "tests/data/colorpack-v1.json").read_text(encoding="utf-8"))


def test_schema_and_vocabulary():
    package = (PACKAGE / "schema" / "colorpack.schema.json").read_bytes()
    assert package == (ROOT / "schema" / "colorpack.schema.json").read_bytes()
    schema = json.loads(package)
    Draft202012Validator.check_schema(schema)
    vocab = json.loads((PACKAGE / "data" / "colorpack-vocabulary.json").read_text("utf-8"))
    assert vocab["formatVersion"] == 1
    assert len(vocab["taxonomy"]["commit"]) == 40
    assert schema["properties"]["taxonomyVersion"]["const"] == vocab["taxonomy"]["version"]
    assert schema["properties"]["vocabularyVersion"]["const"] == vocab["vocabularyVersion"]
    for name, values in (
        ("style", vocab["styleMap"].values()),
        ("buildingType", vocab["buildingTypeMap"].values()),
        ("biomeLike", vocab["biomeMap"].values()),
        ("role", vocab["roleParts"]),
        ("material", vocab["materials"]),
    ):
        assert set(schema["$defs"][name]["enum"]) == set(values)
    assert len(set(vocab["biomeMap"].values())) == 16
    assert len(vocab["materials"]) == 18
    assert "roof_surface" not in vocab["roleParts"]["floor"]
    assert "floor_visible" not in vocab["roleParts"]["roof"]
    assert "other" not in vocab["buildingTypeMap"].values()


def test_valid_pack_is_independent_and_read_only(pack, monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        pytest.fail("検証に外部通信は不要")

    monkeypatch.setattr(socket, "socket", no_network)
    before = copy.deepcopy(pack)
    result = validate_colorpack(MappingProxyType(pack))
    assert pack == before
    assert parse_colorpack(json.dumps(pack)) == result
    assert result.pack_version == "1.0.0"
    assert result.taxonomy_version == "0.1.0"
    assert result.vocabulary_version == "1.0.0"
    assert result.format_version == 1
    assert result.material_prior[0]["roles"]["wall_main"]["stone"] == .75
    assert result.relation_rule[0]["pair"] == ("wall_main", "roof")
    assert result.accent_rule[0]["maxAreaRatio"] == .1
    pack["palettePrior"][0]["roles"]["roof"]["oklab"]["L"][0] = 0
    assert result.palette_prior[0]["roles"]["roof"]["oklab"]["L"][0] == .2
    with pytest.raises(TypeError):
        result.provenance["inputs"] = 99
    with pytest.raises(TypeError):
        result.palette_prior[0]["roles"]["roof"]["oklab"]["L"][0] = 0


@pytest.mark.parametrize("field,value", [
    ("formatVersion", 2), ("formatVersion", True),
    ("packVersion", "1.0"), ("packVersion", "01.0.0"),
    ("packVersion", "1.0.0\n"),
    ("packVersion", "1.0.0-01"), ("taxonomyVersion", "9.0.0"),
    ("vocabularyVersion", "9.0.0"),
    ("block_color", []), ("palette_prior", []),
    ("palettePrior", None), ("materialPrior", {}),
])
def test_unknown_versions_and_old_format(pack, field, value):
    pack[field] = value
    with pytest.raises(BlueprintError, match=r"\$"):
        validate_colorpack(pack)


@pytest.mark.parametrize("when", [
    {"style": "gothic"}, {"buildingType": "house"}, {"biomeLike": "forest"},
    {"style": "medieval", "buildingType": "house"},
    {"style": "medieval", "biomeLike": "*"}, {"style": ""}, {"style": None},
    {"style": "medieval", "building_type": "house"},
])
def test_invalid_conditions(pack, when):
    pack["palettePrior"][0]["when"] = when
    with pytest.raises(BlueprintError, match=r"\$\.palettePrior\[0\].when"):
        validate_colorpack(pack)


@pytest.mark.parametrize("when", [
    {}, {"style": "medieval"}, {"style": "medieval", "biomeLike": "forest"},
    {"style": "medieval", "biomeLike": "forest", "buildingType": "house"},
])
def test_four_condition_levels(pack, when):
    for key in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        pack[key][0]["when"] = when
    validate_colorpack(pack)


@pytest.mark.parametrize("kind", ["palettePrior", "materialPrior", "relationRule", "accentRule"])
def test_duplicate_conditions_even_with_different_values(pack, kind):
    duplicate = copy.deepcopy(pack[kind][0])
    if kind == "palettePrior":
        duplicate["roles"]["roof"]["oklab"]["L"] = [0] * 5
    elif kind == "materialPrior":
        duplicate["roles"]["wall_main"] = {"wood": 1}
    elif kind == "relationRule":
        duplicate["dL"] = [0] * 5
    else:
        duplicate["maxAreaRatio"] = .5
    pack[kind].append(duplicate)
    with pytest.raises(BlueprintError, match="重複"):
        validate_colorpack(pack)


@pytest.mark.parametrize("kind", ["palettePrior", "materialPrior"])
def test_disjoint_roles_at_same_condition(pack, kind):
    duplicate = copy.deepcopy(pack[kind][0])
    duplicate["roles"] = {"frame": next(iter(duplicate["roles"].values()))}
    pack[kind].append(duplicate)
    validate_colorpack(pack)


@pytest.mark.parametrize("pair", [
    ["roof", "roof"], ["roof"], ["roof", "floor", "trim"], ["roof", "unknown"],
])
def test_ambiguous_pair(pack, pair):
    pack["relationRule"][0]["pair"] = pair
    with pytest.raises(BlueprintError, match="pair"):
        validate_colorpack(pack)


def test_reversed_pair_is_duplicate(pack):
    row = copy.deepcopy(pack["relationRule"][0])
    row["pair"].reverse()
    pack["relationRule"].append(row)
    with pytest.raises(BlueprintError, match="重複"):
        validate_colorpack(pack)


@pytest.mark.parametrize("component", ["L", "a", "b"])
def test_reversed_palette_quantiles(pack, component):
    pack["palettePrior"][0]["roles"]["roof"]["oklab"][component].reverse()
    with pytest.raises(BlueprintError, match=component):
        validate_colorpack(pack)


@pytest.mark.parametrize("component", ["dL", "dC", "dh"])
def test_reversed_relation_quantiles(pack, component):
    row = pack["relationRule"][0]
    (row["dh"]["quantiles"] if component == "dh" else row[component]).reverse()
    with pytest.raises(BlueprintError, match=component):
        validate_colorpack(pack)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_mapping_and_json(pack, value):
    pack["relationRule"][0]["dh"]["origin"] = value
    for call, raw in [(validate_colorpack, pack), (parse_colorpack, json.dumps(pack))]:
        with pytest.raises(BlueprintError, match=r"\$\.relationRule\[0\].dh.origin"):
            call(raw)


def test_json_number_overflow(pack):
    text = json.dumps(pack).replace('"origin": 3.1', '"origin": 1e9999')
    with pytest.raises(BlueprintError, match=r"\$\.relationRule\[0\].dh.origin"):
        parse_colorpack(text)


def test_circular_boundary(pack):
    dh = pack["relationRule"][0]["dh"]
    dh.update(origin=-math.pi, quantiles=[-math.pi, -1, 0, 1, math.pi])
    validate_colorpack(pack)
    dh["origin"] = math.pi
    with pytest.raises(BlueprintError, match="origin"):
        validate_colorpack(pack)


def test_closed_numeric_boundaries_and_direct_support(pack):
    value = pack["palettePrior"][0]["roles"]["roof"]
    value.update(n=1, subjects=1, nEff=1, support={"status": "direct"})
    value["oklab"] = {"L": [0, 0, .5, 1, 1], "a": [-.4] * 5, "b": [.4] * 5}
    row = pack["relationRule"][0]
    limit = math.sqrt(.32)
    row.update(dL=[-1, -1, 0, 1, 1], dC=[-limit, -limit, 0, limit, limit])
    row["dh"]["origin"] = math.nextafter(math.pi, -math.inf)
    pack["materialPrior"][0]["roles"]["wall_main"] = {"stone": 0, "wood": 1}
    pack["accentRule"][0]["maxAreaRatio"] = 0
    pack["accentRule"].append({"when": {}, "role": "trim", "maxAreaRatio": 1})
    validate_colorpack(pack)


def test_duplicate_condition_is_independent_of_key_order(pack):
    row = pack["accentRule"][0]
    row["when"] = {"style": "medieval", "biomeLike": "forest", "buildingType": "house"}
    duplicate = copy.deepcopy(row)
    duplicate["when"] = dict(reversed(list(row["when"].items())))
    pack["accentRule"].append(duplicate)
    with pytest.raises(BlueprintError, match="重複"):
        validate_colorpack(pack)


@pytest.mark.parametrize("probabilities", [
    {"stone": 0}, {"stone": -.1, "wood": 1.1}, {"stone": .3, "wood": .3},
    {"unknown": 1}, {},
])
def test_invalid_material_distribution(pack, probabilities):
    pack["materialPrior"][0]["roles"]["wall_main"] = probabilities
    with pytest.raises(BlueprintError, match="materialPrior"):
        validate_colorpack(pack)


def test_probability_tolerance(pack):
    probabilities = pack["materialPrior"][0]["roles"]["wall_main"]
    probabilities["stone"] += .0000005
    validate_colorpack(pack)
    probabilities["stone"] += .000001
    with pytest.raises(BlueprintError, match="確率"):
        validate_colorpack(pack)


def test_single_material_probability_tolerance(pack):
    pack["materialPrior"][0]["roles"]["wall_main"] = {"stone": 1.0000005}
    validate_colorpack(pack)


def test_probability_lower_boundary(pack):
    pack["materialPrior"][0]["roles"]["wall_main"] = {"stone": 0.999999}
    validate_colorpack(pack)


def test_probability_upper_boundary(pack):
    pack["materialPrior"][0]["roles"]["wall_main"] = {"stone": 1.000001}
    validate_colorpack(pack)


@pytest.mark.parametrize("total", [
    math.nextafter(0.999999, -math.inf), math.nextafter(1.000001, math.inf),
])
def test_probability_just_outside_boundary(pack, total):
    pack["materialPrior"][0]["roles"]["wall_main"] = {"stone": total}
    with pytest.raises(BlueprintError, match=r"\$\.materialPrior\[0\].roles.wall_main"):
        validate_colorpack(pack)


def test_probability_sum_overflow(pack):
    pack["materialPrior"][0]["roles"]["wall_main"] = {"stone": 1e308, "wood": 1e308}
    for call, raw in [(validate_colorpack, pack), (parse_colorpack, json.dumps(pack))]:
        with pytest.raises(BlueprintError, match=r"\$\.materialPrior\[0\].roles.wall_main"):
            call(raw)


def test_json_integer_digit_limit(pack):
    text = json.dumps(pack).replace('"inputs": 4', '"inputs": ' + "1" * 4301)
    with pytest.raises(BlueprintError, match=r"\$:"):
        parse_colorpack(text)


@pytest.mark.parametrize("kind", ["palettePrior", "relationRule"])
@pytest.mark.parametrize("field,value", [("n", 5), ("subjects", 3)])
def test_support_cannot_exceed_provenance(pack, kind, field, value):
    row = pack[kind][0]
    support = row["roles"]["roof"] if kind == "palettePrior" else row
    support[field] = value
    with pytest.raises(BlueprintError, match=rf"\$\.{kind}\[0\].*\.{field}:"):
        validate_colorpack(pack)


@pytest.mark.parametrize("kind", ["palettePrior", "relationRule"])
def test_smoothed_parent_chain(pack, kind):
    for when, parent in [
        ({"style": "medieval"}, {}),
        ({"style": "medieval", "biomeLike": "forest"}, {"style": "medieval"}),
        ({"style": "medieval", "biomeLike": "forest", "buildingType": "house"},
         {"style": "medieval", "biomeLike": "forest"}),
    ]:
        row = copy.deepcopy(pack[kind][0])
        row["when"] = when
        values = row["roles"].values() if kind == "palettePrior" else [row]
        for value in values:
            value.update(n=0, subjects=0, nEff=0,
                         support={"status": "smoothed", "sourceWhen": parent})
        pack[kind].append(row)
    validate_colorpack(pack)
    value = row["roles"]["roof"] if kind == "palettePrior" else row
    value["support"]["sourceWhen"] = row["when"]
    with pytest.raises(BlueprintError, match="sourceWhen"):
        validate_colorpack(pack)


@pytest.mark.parametrize("kind", ["palettePrior", "relationRule"])
@pytest.mark.parametrize("problem", ["missing", "unrelated", "positive", "direct", "support_order"])
def test_invalid_support(pack, kind, problem):
    row = pack[kind][0]
    row["when"] = {"style": "medieval", "biomeLike": "forest"}
    value = row["roles"]["roof"] if kind == "palettePrior" else row
    value.update(n=0, subjects=0, nEff=0, support={"status": "smoothed", "sourceWhen": {}})
    if problem == "unrelated":
        value["support"]["sourceWhen"] = {"style": "modern"}
    elif problem == "positive":
        value.update(n=1, subjects=1, nEff=1)
    elif problem == "direct":
        value["support"] = {"status": "direct"}
    elif problem == "support_order":
        value.update(n=1, subjects=2, nEff=3)
    with pytest.raises(BlueprintError, match=kind):
        validate_colorpack(pack)


@pytest.mark.parametrize("key", [
    "dataLicense", "inputs", "subjects", "licenses", "attributions", "snapshot", "generator",
])
def test_missing_provenance(pack, key):
    del pack["provenance"][key]
    with pytest.raises(BlueprintError, match="provenance"):
        validate_colorpack(pack)


def test_empty_synthetic_pack(pack):
    for key in ("palettePrior", "materialPrior", "relationRule", "accentRule"):
        pack[key] = []
    pack["provenance"].update(inputs=0, subjects=0, licenses={}, attributions=[])
    pack["packVersion"] = "1.2.3-alpha.1+fixture"
    validate_colorpack(pack)


@pytest.mark.parametrize("path,value", [
    (("palettePrior", 0, "roles", "roof", "oklab", "L"), [0] * 4),
    (("palettePrior", 0, "roles", "roof", "oklab", "L"), [1.1] * 5),
    (("palettePrior", 0, "roles", "roof", "oklab", "a"), [-.41] * 5),
    (("palettePrior", 0, "roles", "roof", "oklab", "b"), [.41] * 5),
    (("palettePrior", 0, "roles"), {"unknown": {}}),
    (("palettePrior", 0, "roles"), {}),
    (("relationRule", 0, "dL"), [-1.01] * 5),
    (("relationRule", 0, "dC"), [.57] * 5),
    (("relationRule", 0, "dh", "quantiles"), [3.15] * 5),
    (("accentRule", 0, "maxAreaRatio"), 1.01),
    (("accentRule", 0, "maxAreaRatio"), -.01),
    (("palettePrior", 0, "roles", "roof", "n"), -1),
    (("palettePrior", 0, "roles", "roof", "subjects"), 1.5),
    (("palettePrior", 0, "roles", "roof", "nEff"), -1),
    (("provenance", "dataLicense"), " "),
    (("provenance", "snapshot", "sha256"), "bad-hash"),
    (("provenance", "snapshot", "sha256"), "0" * 64 + "\n"),
    (("provenance", "generator", "commit"), "branch-name"),
    (("provenance", "subjects"), 5),
    (("provenance", "licenses"), {}),
    (("provenance", "attributions"), []),
    (("provenance", "attributions", 0, "license"), "unknown"),
])
def test_invalid_boundaries_and_provenance(pack, path, value):
    target = pack
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(BlueprintError, match=r"\$"):
        validate_colorpack(pack)


def test_relation_parent_requires_same_orientation(pack):
    row = copy.deepcopy(pack["relationRule"][0])
    row.update(when={"style": "medieval"}, n=0, subjects=0, nEff=0,
               support={"status": "smoothed", "sourceWhen": {}})
    row["pair"].reverse()
    pack["relationRule"].append(row)
    with pytest.raises(BlueprintError, match="向き"):
        validate_colorpack(pack)


def test_parent_requires_same_role(pack):
    row = copy.deepcopy(pack["palettePrior"][0])
    row["when"] = {"style": "medieval"}
    value = row["roles"]["roof"]
    value.update(n=0, subjects=0, nEff=0,
                 support={"status": "smoothed", "sourceWhen": {}})
    row["roles"] = {"trim": value}
    pack["palettePrior"].append(row)
    with pytest.raises(BlueprintError, match="親分布"):
        validate_colorpack(pack)


@pytest.mark.parametrize("text,path", [
    ('{"provenance":{"inputs":0,"inputs":1}}', r"\$\.provenance.inputs"),
    ('{"palettePrior":[{"when":{"style":"a","style":"b"}}]}', r"\$\.palettePrior\[0\].when.style"),
    ('{"formatVersion":', r"\$"),
    ('[]', r"\$"),
])
def test_json_entry_errors(text, path):
    with pytest.raises(BlueprintError, match=path):
        parse_colorpack(text)
