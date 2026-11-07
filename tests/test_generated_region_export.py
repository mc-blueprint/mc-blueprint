"""領域生成から単一schematicまでの契約試験。"""

import gzip
import hashlib
import tracemalloc
import weakref
from dataclasses import replace

import nbtlib
import pytest

from mcblueprint import blockdata
from mcblueprint.errors import BlueprintError
from mcblueprint.exporters import EXPORTERS
from mcblueprint.exporters._spool import RegionSpool
from mcblueprint.generator import GeneratedRegion, GenerationLimits, generate, generate_regions
from mcblueprint.importers import read_terrain
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.model.block import BlockState
from mcblueprint.model.vec import AABB, Vec3
from mcblueprint.operations.box_like import iter_box
from mcblueprint.region_export import _export_spool, export_generated_regions
from mcblueprint.volume import RegionVolume


def blueprint(version="1.21.11", operations=None):
    return load_blueprint_dict({
        "formatVersion": 1, "minecraftVersion": version, "name": "領域出力",
        "origin": [-8, 2, 4],
        "operations": operations if operations is not None else [
            {"type": "fill", "from": [-3, -1, -2], "to": [4, 1, 3], "block": "stone"},
            {"type": "fill", "from": [-1, 0, -2], "to": [1, 1, 3], "block": "air"},
            {"type": "set", "position": [2, 1, 0], "block": "oak_stairs[facing=east]"},
        ],
    })


def assert_same(first, second):
    a, b = read_terrain(first), read_terrain(second)
    assert (a.bounds, a.offset, a.data_version) == (b.bounds, b.offset, b.data_version)
    for pos in iter_box(a.bounds):
        assert a.get(pos) == b.get(pos)


@pytest.mark.parametrize("fmt", ["schem", "litematic"])
def test_compression_level_preserves_nbt_bytes(tmp_path, monkeypatch, fmt):
    bp = blueprint()
    volume = generate(bp)
    fast, default = tmp_path / f"fast.{fmt}", tmp_path / f"default.{fmt}"
    monkeypatch.setattr("mcblueprint.exporters.litematic.time.time", lambda: 1700000000.0)
    EXPORTERS[fmt]().export(volume, bp, fast)

    def save_default(nbt, path):
        nbt.save(str(path))

    monkeypatch.setattr(f"mcblueprint.exporters.{fmt}.save_atomic", save_default)
    EXPORTERS[fmt]().export(volume, bp, default)
    assert gzip.decompress(fast.read_bytes()) == gzip.decompress(default.read_bytes())


@pytest.mark.parametrize("fmt", ["schem", "litematic"])
def test_export_budget_rejects_full_arrays_before_reading_rows(tmp_path, monkeypatch, fmt):
    bp = blueprint()
    bounds = AABB(Vec3(0, 0, 0), Vec3(399, 249, 399))
    path = tmp_path / f"keep.{fmt}"
    path.write_bytes(b"completed")

    def rows():
        pytest.fail("予算超過時に入力行を消費してはいけません")
        yield

    def allocate(*args, **kwargs):
        pytest.fail("予算超過時に全体配列を確保してはいけません")

    monkeypatch.setattr("mcblueprint.exporters.litematic.np.empty", allocate)
    with pytest.raises(BlueprintError, match="NBT.*max_memory_mb"):
        EXPORTERS[fmt]().export_rows(rows(), bounds, bp, path, palette=[None],
                                     max_memory_bytes=128 * 1024 * 1024)
    assert path.read_bytes() == b"completed"
    assert set(tmp_path.iterdir()) == {path}


@pytest.mark.parametrize("version", blockdata.supported_versions())
@pytest.mark.parametrize("fmt", ["schem", "litematic"])
def test_budgeted_spool_preserves_exact_nbt(tmp_path, monkeypatch, version, fmt):
    bp = blueprint(version)
    spool = RegionSpool(tmp_path / "cells", bp.bounds())
    spool.write(generate_regions(bp))
    first, second = tmp_path / f"old.{fmt}", tmp_path / f"new.{fmt}"
    monkeypatch.setattr("mcblueprint.exporters.litematic.time.time", lambda: 1700000000.0)
    _export_spool(spool, bp, first, format=fmt)
    _export_spool(spool, bp, second, format=fmt, max_memory_bytes=16 * 1024 * 1024)
    assert gzip.decompress(first.read_bytes()) == gzip.decompress(second.read_bytes())


@pytest.mark.parametrize("version", blockdata.supported_versions())
@pytest.mark.parametrize("fmt", ["schem", "litematic"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("reuse", [False, True])
def test_round_trip(tmp_path, version, fmt, reverse, reuse):
    bp = blueprint(version)
    old, new = tmp_path / f"old.{fmt}", tmp_path / f"new.{fmt}"
    volume = generate(bp)
    EXPORTERS[fmt]().export(volume, bp, old)
    limits = GenerationLimits(region=(3, 2, 4), halo=1)
    regions = generate_regions(bp, limits=limits, reverse=reverse)
    if reuse:
        spool = RegionSpool(tmp_path / "cells", bp.bounds())
        spool.write(regions)
        result = _export_spool(spool, bp, new, format=fmt)
        spool.path.unlink()
    else:
        result = export_generated_regions(regions, bp, new, format=fmt)
    assert result == new
    assert_same(old, new)
    snapshot = read_terrain(new)
    bounds = volume.bounds()
    assert snapshot.offset == bounds.min - bp.origin
    assert snapshot.data_version == blockdata.data_version(version)
    blocks = blockdata.load_block_data(version)
    for pos in iter_box(bounds):
        expected = blocks.complete(volume.get(pos) or BlockState.parse("air"))
        assert snapshot.get(pos - bounds.min) == expected
    assert set(tmp_path.iterdir()) == {old, new}


@pytest.mark.parametrize("fmt", ["schem", "litematic"])
def test_palette_width_and_all_air(tmp_path, fmt):
    data = blockdata.load_block_data("1.21.11")
    ids = [key for key in sorted(data._blocks) if not data.get(key).properties][:150]
    for name, operations in [
        ("palette", [{"type": "set", "position": [x - 80, 0, 0], "block": key}
                     for x, key in enumerate(ids)]),
        ("air", [{"type": "fill", "from": [-4, 0, -2], "to": [3, 1, 2], "block": "air"}]),
    ]:
        bp = blueprint(operations=operations)
        old, new = tmp_path / f"{name}-old.{fmt}", tmp_path / f"{name}-new.{fmt}"
        EXPORTERS[fmt]().export(generate(bp), bp, old)
        export_generated_regions(generate_regions(bp, limits=GenerationLimits(
            region=(17, 1, 2), halo=2,
        )), bp, new, format=fmt)
        assert_same(old, new)


def test_halo_is_discarded_and_consumed_volumes_are_released(tmp_path):
    bp = blueprint(operations=[
        {"type": "fill", "from": [0, 0, 0], "to": [3, 0, 0], "block": "stone"},
    ])
    references = []

    def regions():
        for x in range(4):
            assert all(ref() is None for ref in references)
            volume = RegionVolume(AABB(Vec3(-1, 0, 0), Vec3(4, 0, 0)))
            for pos in iter_box(volume.region):
                volume.set(pos, BlockState.parse("gold_block"))
            pos = Vec3(x, 0, 0)
            volume.set(pos, BlockState.parse("stone"))
            references.append(weakref.ref(volume))
            yield GeneratedRegion(AABB(pos, pos), volume)
            del volume

    out = tmp_path / "out.schem"
    export_generated_regions(regions(), bp, out)
    assert all(ref() is None for ref in references)
    snapshot = read_terrain(out)
    assert all(snapshot.get(Vec3(x, 0, 0)) == BlockState.parse("stone") for x in range(4))


@pytest.mark.parametrize("fmt", ["schem", "litematic"])
@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("existing", [False, True])
def test_output_failure_preserves_completed_file(tmp_path, monkeypatch, fmt, failure, existing):
    bp = blueprint()
    out = tmp_path / f"out.{fmt}"
    if existing:
        out.write_bytes(b"completed")

    def fail(self, stream, **kwargs):
        stream.write(b"incomplete")
        raise failure("interrupted")

    monkeypatch.setattr(nbtlib.File, "write", fail)
    expected = BlueprintError if failure is OSError else failure
    with pytest.raises(expected):
        export_generated_regions(generate_regions(bp), bp, out, format=fmt)
    assert out.read_bytes() == b"completed" if existing else not out.exists()
    assert list(tmp_path.iterdir()) == ([out] if existing else [])


@pytest.mark.parametrize("mode", ["gap", "overlap", "outside", "volume", "generation", "spool"])
def test_bad_core_and_generation_or_spool_failure(tmp_path, monkeypatch, mode):
    bp = blueprint()
    regions = list(generate_regions(bp))
    if mode == "gap":
        regions = []
    elif mode == "overlap":
        regions *= 2
    elif mode == "outside":
        regions = [replace(regions[0], core=AABB(Vec3(-99, 0, 0), Vec3(99, 0, 0)))]
    elif mode == "volume":
        regions = [replace(regions[0], volume=RegionVolume(AABB(Vec3(0, 0, 0), Vec3(0, 0, 0))))]
    elif mode == "generation":
        first = regions[0]
        def interrupted():
            yield first
            raise RuntimeError("generation")
        regions = interrupted()
    elif mode == "spool":
        def fail(*args):
            raise OSError("disk full")
        monkeypatch.setattr(RegionSpool, "_seek", fail)
    out = tmp_path / "out.schem"
    out.write_bytes(b"completed")
    with pytest.raises(RuntimeError if mode == "generation" else BlueprintError):
        export_generated_regions(regions, bp, out)
    assert out.read_bytes() == b"completed"
    assert list(tmp_path.iterdir()) == [out]


@pytest.mark.parametrize("mode", ["new", "gap", "rewrite", "truncated"])
def test_incomplete_spool_cannot_replace_output(tmp_path, mode):
    bp = blueprint()
    spool = RegionSpool(tmp_path / "cells", bp.bounds())
    out = tmp_path / "out.schem"
    out.write_bytes(b"completed")
    if mode in ("rewrite", "truncated"):
        spool.write(generate_regions(bp))
    if mode in ("gap", "rewrite"):
        with pytest.raises(BlueprintError, match="被覆"):
            spool.write([])
    elif mode == "truncated":
        spool.path.write_bytes(b"")
    with pytest.raises(BlueprintError):
        _export_spool(spool, bp, out)
    assert out.read_bytes() == b"completed"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mode", ["valid", "overlap", "gap"])
def test_irregular_cores_across_tiles(tmp_path, reverse, mode):
    bp = blueprint(operations=[
        {"type": "fill", "from": [-5, -2, -3], "to": [6, 1, 4], "block": "stone"},
    ])
    bounds = bp.bounds()
    volume = next(generate_regions(bp)).volume
    cores = [
        AABB(bounds.min, Vec3(-3, 1, 4)),
        AABB(Vec3(-2, -2, -3), Vec3(6, -1, 4)),
        AABB(Vec3(-2, 0, -3), bounds.max),
    ]
    if mode == "overlap":
        cores.append(AABB(Vec3(-4, -1, -2), Vec3(5, 0, 3)))
    elif mode == "gap":
        cores.pop()
    if reverse:
        cores.reverse()
    spool = RegionSpool(tmp_path / "cells", bounds)
    regions = [GeneratedRegion(core, volume) for core in cores]
    if mode != "valid":
        with pytest.raises(BlueprintError, match="重複" if mode == "overlap" else "被覆"):
            spool.write(regions)
    else:
        spool.write(regions)
        assert spool.complete
        assert all(spool.palette[int(index)] == BlockState.parse("stone")
                   for row in spool.rows() for index in row)


@pytest.mark.parametrize("offset", [Vec3(0, 0, 0), Vec3(-17, -13, -9)])
def test_ragged_first_core_does_not_expand_index_per_cell(tmp_path, offset):
    bounds = AABB(offset, offset + Vec3(64, 64, 64))
    cores = [AABB(offset + Vec3(x, y, z), offset + Vec3(
        min(x + 63, 64), min(y + 63, 64), min(z + 63, 64),
    )) for y in (0, 64) for z in (0, 64) for x in (0, 64)]
    hashes = []
    for reverse in (False, True):
        ordered = list(reversed(cores)) if reverse else cores

        def regions(ordered=ordered):
            for core in ordered:
                volume = RegionVolume(core)
                volume.set(core.min, BlockState.parse("stone"))
                yield GeneratedRegion(core, volume)
                del volume

        spool = RegionSpool(tmp_path / f"cells-{reverse}", bounds)
        tracemalloc.start()
        try:
            spool.write(regions())
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 8 * 1024 * 1024
        assert spool.complete
        assert sum(int((row != 0).sum()) for row in spool.rows()) == 8
        hashes.append(hashlib.sha256(spool.path.read_bytes()).hexdigest())
    assert hashes[0] == hashes[1]
