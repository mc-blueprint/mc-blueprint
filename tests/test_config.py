from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcblueprint.buildspec import SCALE_LIMITS, load_buildspec, resolve_buildspec
from mcblueprint.config import resolve_config
from mcblueprint.errors import BlueprintError


def test_schema_copies_match() -> None:
    root = Path(__file__).parents[1]
    for name in ("buildspec.schema.json", "config.schema.json"):
        assert json.loads((root / "schema" / name).read_text(encoding="utf-8")) == json.loads(
            (root / "src/mcblueprint/schema" / name).read_text(encoding="utf-8")
        )


def test_resolve_layers_and_sources(tmp_path: Path) -> None:
    (tmp_path / "mc-blueprint.yaml").write_text("weathering:\n  age: 0.5\n", encoding="utf-8")
    user = tmp_path / "user.yaml"
    user.write_text("weathering:\n  age: 0.4\n", encoding="utf-8")
    result = resolve_config(
        buildspec={"seed": 7, "materials": {"wall_main": "stone"}},
        project_dir=tmp_path,
        user_config=user,
        overrides=["weathering.age=0.8"],
    )
    assert result.values["weathering"]["age"] == 0.8
    assert result.origins["weathering.age"] == "cli"
    assert result.values["seed"] == 7
    assert result.values["palette"]["materials"] == {"wall_main": "stone"}


def test_reject_duplicate_yaml_keys(tmp_path: Path) -> None:
    user = tmp_path / "config.yaml"
    user.write_text("weathering:\n  age: 0.1\n  age: 0.2\n", encoding="utf-8")
    with pytest.raises(BlueprintError, match="重複"):
        resolve_config(user_config=user, project_dir=tmp_path)


def test_reject_unknown_setting_and_invalid_range(tmp_path: Path) -> None:
    user = tmp_path / "config.yaml"
    user.write_text("weathering:\n  agge: 0.2\n", encoding="utf-8")
    with pytest.raises(BlueprintError):
        resolve_config(user_config=user, project_dir=tmp_path)
    user.write_text("randomness: 2\n", encoding="utf-8")
    with pytest.raises(BlueprintError):
        resolve_config(user_config=user, project_dir=tmp_path)


def test_buildspec_load_and_duplicate_rejection(tmp_path: Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        "formatVersion: 1\nminecraftVersion: '26.3'\n",
        encoding="utf-8",
    )
    loaded = load_buildspec(spec)
    assert loaded["archetype"] == "house"
    assert loaded["style"] == "medieval"
    assert loaded["scale"] == "small"
    assert loaded["size"] == SCALE_LIMITS["small"]
    assert loaded["seed"] == 0
    assert loaded["site"] == {"terrain": "none", "biome": "plains"}
    assert loaded["interior"] == "minimal"
    spec.write_text("formatVersion: 1\nformatVersion: 1\n", encoding="utf-8")
    with pytest.raises(BlueprintError, match="重複"):
        load_buildspec(spec)


def test_style_defaults_are_resolved(tmp_path: Path) -> None:
    result = resolve_config(
        buildspec={"style": "medieval"}, user_config=tmp_path / "missing.yaml", project_dir=tmp_path
    )
    assert result.values["palette"]["materials"]["wall_main"] == ["brick", "stone"]
    assert result.origins["palette.materials.wall_main"] == "style:medieval"


def test_unknown_style_has_candidates(tmp_path: Path) -> None:
    with pytest.raises(BlueprintError, match="候補"):
        resolve_config(
            buildspec={"style": "unknown"},
            user_config=tmp_path / "missing.yaml",
            project_dir=tmp_path,
        )


def _spec(**values: object) -> dict:
    return {"minecraftVersion": "26.3", **values}


@pytest.mark.parametrize("scale,limit", SCALE_LIMITS.items())
def test_scale_limits(scale: str, limit: list[int]) -> None:
    assert load_buildspec_from_dict(_spec(scale=scale))["size"] == limit


def load_buildspec_from_dict(data: dict) -> dict:
    """Exercise the same public validation path used by Resolve."""
    from mcblueprint.buildspec import _normalize

    return _normalize(data)


@pytest.mark.parametrize(
    "archetype,features",
    [
        ("house", {"floors": True}),
        ("house", {"floors": 3}),
        ("house", {"floor_height": 3}),
        ("house", {"rooff": "gable"}),
        ("tower", {"shape": "circle"}),
        ("tower", {"floors": False}),
        ("tower", {"battlement": "yes"}),
        ("tower", {"floorz": 5}),
    ],
)
def test_archetype_features_are_closed_and_typed(archetype: str, features: dict) -> None:
    with pytest.raises(BlueprintError):
        load_buildspec_from_dict(_spec(archetype=archetype, features=features))


def test_feature_typo_includes_candidates() -> None:
    with pytest.raises(BlueprintError, match="候補.*floors"):
        load_buildspec_from_dict(_spec(features={"flors": 2}))


@pytest.mark.parametrize("value", [True, 257, [1, 2], [1, 2, 3, 4], [1, 2, 0]])
def test_size_is_bounded_integer_triplet(value: object) -> None:
    with pytest.raises(BlueprintError):
        load_buildspec_from_dict(_spec(size=value))


@pytest.mark.parametrize(
    "values,reason",
    [
        ({"archetype": "castle"}, "Massing"),
        ({"interior": "furnished"}, "Detail"),
        ({"site": {"terrain": "mountain"}}, "Site"),
    ],
)
def test_unsupported_requests_name_stage_and_milestone(values: dict, reason: str) -> None:
    with pytest.raises(BlueprintError, match=reason):
        load_buildspec_from_dict(_spec(**values))


def test_material_color_object_and_range() -> None:
    valid = {"wall_main": {"color": [0.6, 0.1, -0.2], "material": "stone"}}
    assert load_buildspec_from_dict(_spec(materials=valid))["materials"] == valid
    invalid = {"wall_main": {"color": [0.6, 0.5, 0], "material": "stone"}}
    with pytest.raises(BlueprintError):
        load_buildspec_from_dict(_spec(materials=invalid))
    config_material = {"color": [0.6, 0.1, -0.2], "material": "stone"}
    assert (
        resolve_config(
            buildspec={"config": {"palette": {"materials": {"wall_main": config_material}}}}
        ).values["palette"]["materials"]["wall_main"]
        == config_material
    )


def test_resolve_api_is_shared_and_detects_same_layer_conflict(tmp_path: Path) -> None:
    source = tmp_path / "spec.yaml"
    source.write_text("minecraftVersion: '26.3'\nfeatures:\n  floors: 2\n", encoding="utf-8")
    result = resolve_buildspec(source, project_dir=tmp_path, user_config=tmp_path / "none.yaml")
    assert result.buildspec["features"] == {"floors": 2}
    assert result.config == result.buildspec["config"]
    assert result.origins["seed"].startswith("buildspec:")
    with pytest.raises(BlueprintError, match="BuildSpec.seed"):
        resolve_buildspec(
            _spec(seed=1, config={"seed": 2}),
            project_dir=tmp_path,
            user_config=tmp_path / "none.yaml",
        )


@pytest.mark.parametrize("version", ["1.21.11", "26.1.2", "26.2", "26.3"])
def test_supported_minecraft_versions(version: str) -> None:
    assert load_buildspec_from_dict(_spec(minecraftVersion=version))["minecraftVersion"] == version


def test_resolve_buildspec_resolves_terrain_path(tmp_path: Path) -> None:
    source = tmp_path / "spec.yaml"
    source.write_text(
        "minecraftVersion: '26.3'\nsite:\n  terrain: terrain.schem\n", encoding="utf-8"
    )
    result = resolve_buildspec(source, project_dir=tmp_path, user_config=tmp_path / "none.yaml")
    assert result.buildspec["site"]["terrain"] == str((tmp_path / "terrain.schem").resolve())


def test_building_features_must_fit_the_size_limit() -> None:
    with pytest.raises(BlueprintError, match="size"):
        load_buildspec_from_dict(_spec(archetype="tower", features={"floors": 20}, scale="small"))
