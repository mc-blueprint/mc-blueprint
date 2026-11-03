import base64
import gzip
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from jsonschema import validate

from mcblueprint import blockdata
from mcblueprint import scene as scene_module
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.generator import GeneratedRegion
from mcblueprint.model import AABB, BlockState, Vec3
from mcblueprint.model.blueprint import Blueprint
from mcblueprint.scene import _source_descriptor, capture_sources, write_scene
from mcblueprint.volume import CellAttributes, RegionVolume

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("element", ["tower#1", "tower#1.body#1", "tower#10.body#1",
                                     "site#1", None])
@pytest.mark.parametrize("spec", [True, False])
def test_only_real_root_and_descendants_offer_params(element, spec):
    attr = CellAttributes(element=element, source="archetype.body", operation="body/0")
    context = {"massing": {"element": "tower#1", "params": {"height": 14}}} if spec else {}
    descriptor = _source_descriptor(attr, context)
    editable = spec and element in ("tower#1", "tower#1.body#1")
    if element:
        assert descriptor["element"] == ("tower#1" if editable else element)
    else:
        assert "element" not in descriptor
    assert descriptor.get("params") == (["height"] if editable else None)
    assert descriptor["spec"] == "archetype.body"
    assert descriptor["label"] == "archetype.body"
    assert descriptor["path"] == "body/0"


@dataclass(frozen=True)
class _FakeState:
    id: str

    def to_string(self):
        return self.id


def test_scene_chunks_are_schema_valid_and_hash_uncompressed_json(tmp_path):
    bounds = AABB(Vec3(-1, 0, -2), Vec3(15, 1, 14))
    volume = RegionVolume(bounds)
    attr = CellAttributes(element="house#1", source="features.wall", operation="house#1/wall")
    for pos in (Vec3(x, y, z) for x in range(-1, 16) for y in range(2)
                for z in range(-2, 15)):
        volume.set(pos, BlockState.parse("stone"), attr)
    region = GeneratedRegion(bounds, volume)
    cells = RegionSpool(tmp_path / "cells", bounds)
    source_path = tmp_path / "sources"
    source_table = {}
    cells.write(capture_sources([region], bounds, source_path, {}, source_table))
    blueprint = Blueprint(1, "1.21.11", "test", [], origin=bounds.min)
    path = tmp_path / "test.scene.json.gz"
    source = {"kind": "spec", "path": "test.yaml", "sha256": "0" * 64}
    preview = {"enabled": True, "shader": "vanilla", "shadows": True,
               "brightness": 0.5, "timeOfDay": 6000, "blockLight": True, "quality": "auto"}

    digest = write_scene(
        path, source_path, cells, source_table, blueprint, {}, source, preview, "plains",
    )
    raw = gzip.decompress(path.read_bytes())
    scene = json.loads(raw)
    validate(scene, json.loads((ROOT / "schema/scene.schema.json").read_text(encoding="utf-8")))

    assert digest == hashlib.sha256(raw).hexdigest()
    assert scene["origin"] == [-1, 0, -2]
    assert [chunk["at"] for chunk in scene["chunks"]] == [
        [0, 0, 0], [16, 0, 0], [0, 0, 16], [16, 0, 16],
    ]
    assert [len(base64.b64decode(chunk["blocks"])) for chunk in scene["chunks"]] == [
        1024, 64, 64, 4,
    ]
    assert len(scene["sources"]) == 1
    assert scene["sources"][0]["element"] == "house#1"
    ids = np.frombuffer(base64.b64decode(scene["chunks"][0]["sources"]), dtype="<u4")
    assert np.all(ids > 0)


@pytest.mark.parametrize("version", blockdata.supported_versions())
def test_scene_gzip_is_deterministic(tmp_path, version):
    bounds = AABB(Vec3(0, 0, 0), Vec3(0, 0, 0))
    volume = RegionVolume(bounds)
    volume.set(Vec3(0, 0, 0), BlockState.parse("sea_lantern"))
    cells = RegionSpool(tmp_path / "cells", bounds)
    cells.write([GeneratedRegion(bounds, volume)])
    source_path = tmp_path / "sources"
    source_path.write_bytes(bytes(4))
    blueprint = Blueprint(1, version, "test", [])
    source = {"kind": "blueprint", "path": "test.json", "sha256": "1" * 64}
    preview = {"enabled": True, "shader": "vanilla", "shadows": True,
               "brightness": 0.5, "timeOfDay": 6000, "blockLight": True, "quality": "auto"}
    args = (source_path, cells, {}, blueprint, {}, source, preview, "plains")
    one, two = tmp_path / "one.gz", tmp_path / "two.gz"

    write_scene(one, *args)
    write_scene(two, *args)

    assert one.read_bytes() == two.read_bytes()


@pytest.mark.parametrize(("state_count", "width"), [(65535, 16), (65536, 32)])
def test_scene_palette_index_width_boundary(tmp_path, monkeypatch, state_count, width):
    bounds = AABB(Vec3(0, 0, 0), Vec3(0, 0, 0))
    spool = RegionSpool(tmp_path / "cells", bounds)
    states = [_FakeState(f"minecraft:block_{index:05}") for index in range(state_count)]
    spool.palette = [None, *states]
    spool.path.write_bytes(struct.pack("<I", state_count))
    source_spool = tmp_path / "sources"
    source_spool.write_bytes(bytes(4))

    class FakeBlockData:
        tints = {key: {"plains": [0.7, 0.0, 0.0]} for key in ("grass", "foliage", "water")}

        def complete(self, state):
            return state

        def drawing(self, _state):
            return 0, 15, "model", "none", None

    monkeypatch.setattr(scene_module.blockdata, "load_block_data", lambda _version: FakeBlockData())
    blueprint = Blueprint(1, "1.21.11", "test", [])
    source = {"kind": "blueprint", "path": "test.json", "sha256": "0" * 64}
    preview = {"enabled": True, "shader": "vanilla", "shadows": True,
               "brightness": 0.5, "timeOfDay": 6000, "blockLight": True, "quality": "auto"}
    path = tmp_path / f"width-{width}.gz"

    write_scene(path, source_spool, spool, {}, blueprint, {}, source, preview, "plains")

    scene = json.loads(gzip.decompress(path.read_bytes()))
    assert scene["indexWidth"] == width
