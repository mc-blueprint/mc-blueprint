import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from mcblueprint import colorpack
from mcblueprint.errors import BlueprintError

ROOT = Path(__file__).resolve().parent.parent


def _resources(root: Path) -> tuple[dict, dict[str, bytes]]:
    pack = json.loads((ROOT / "tests/data/colorpack-v1.json").read_text(encoding="utf-8"))
    payloads = {
        "pack": json.dumps(pack).encode(),
        "license": b"Test license\n",
        "vocabulary": (ROOT / "src/mcblueprint/data/colorpack-vocabulary.json").read_bytes(),
    }
    hashes = {key: hashlib.sha256(value).hexdigest() for key, value in payloads.items()}
    (root / "colorpack").mkdir(parents=True)
    (root / "colorpack/colorpack.json").write_bytes(payloads["pack"])
    (root / "colorpack/DATA_LICENSE").write_bytes(payloads["license"])
    (root / "colorpack-vocabulary.json").write_bytes(payloads["vocabulary"])
    entry = {
        "repository": "https://example.test/color-stats",
        "commit": "a" * 40,
        "packVersion": pack["packVersion"],
        "taxonomyVersion": pack["taxonomyVersion"],
        "vocabularyVersion": pack["vocabularyVersion"],
        "sha256": hashes,
    }
    (root / "lock.json").write_text(json.dumps({"colorpack": entry}), encoding="utf-8")
    return entry, payloads


def test_load_colorpack_returns_locked_pack_from_directory(tmp_path: Path, monkeypatch) -> None:
    _, payloads = _resources(tmp_path)
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)

    loaded = colorpack.load_colorpack()

    assert loaded.pack.pack_version == loaded.pack_version == "1.0.0"
    assert loaded.taxonomy_version == "0.1.0"
    assert loaded.vocabulary_version == "1.0.0"
    assert loaded.pack_sha256 == hashlib.sha256(payloads["pack"]).hexdigest()


def test_load_colorpack_reads_package_resources_from_zip(tmp_path: Path, monkeypatch) -> None:
    _, _ = _resources(tmp_path)
    archive = tmp_path / "resources.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for path in tmp_path.rglob("*"):
            if path.is_file() and path != archive:
                zipped.write(path, path.relative_to(tmp_path).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        root = zipfile.Path(zipped)
        monkeypatch.setattr(colorpack, "_data_root", lambda: root)
        assert colorpack.load_colorpack().pack.pack_version == "1.0.0"


def test_load_colorpack_returns_none_when_lock_and_pack_are_absent(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)
    assert colorpack.load_colorpack() is None


@pytest.mark.parametrize("missing", ["lock", "pack", "license", "vocabulary"])
def test_load_colorpack_rejects_partial_distribution(
    tmp_path: Path, monkeypatch, missing: str,
) -> None:
    _resources(tmp_path)
    target = {
        "lock": tmp_path / "lock.json",
        "pack": tmp_path / "colorpack/colorpack.json",
        "license": tmp_path / "colorpack/DATA_LICENSE",
        "vocabulary": tmp_path / "colorpack-vocabulary.json",
    }[missing]
    target.unlink()
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)
    with pytest.raises(BlueprintError):
        colorpack.load_colorpack()


@pytest.mark.parametrize("change", ["pack", "license", "vocabulary"])
def test_load_colorpack_rejects_modified_contents(
    tmp_path: Path, monkeypatch, change: str,
) -> None:
    _resources(tmp_path)
    target = {
        "pack": tmp_path / "colorpack/colorpack.json",
        "license": tmp_path / "colorpack/DATA_LICENSE",
        "vocabulary": tmp_path / "colorpack-vocabulary.json",
    }[change]
    target.write_bytes(target.read_bytes() + b"changed")
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)
    with pytest.raises(BlueprintError, match="SHA-256"):
        colorpack.load_colorpack()


def test_load_colorpack_rejects_unknown_version(tmp_path: Path, monkeypatch) -> None:
    entry, _ = _resources(tmp_path)
    entry["packVersion"] = "2.0.0"
    (tmp_path / "lock.json").write_text(json.dumps({"colorpack": entry}), encoding="utf-8")
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)
    with pytest.raises(BlueprintError, match="packVersion"):
        colorpack.load_colorpack()


def test_load_colorpack_rejects_duplicate_json_keys(tmp_path: Path, monkeypatch) -> None:
    entry, _ = _resources(tmp_path)
    path = tmp_path / "colorpack/colorpack.json"
    raw = path.read_text(encoding="utf-8")
    duplicate = raw.replace('"formatVersion": 1', '"formatVersion": 1, "formatVersion": 1')
    path.write_text(duplicate, encoding="utf-8")
    entry["sha256"]["pack"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "lock.json").write_text(json.dumps({"colorpack": entry}), encoding="utf-8")
    monkeypatch.setattr(colorpack, "_data_root", lambda: tmp_path)
    with pytest.raises(BlueprintError, match="重複"):
        colorpack.load_colorpack()


def test_load_colorpack_propagates_io_errors(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "lock.json").write_text("{}", encoding="utf-8")

    class BrokenRoot:
        def joinpath(self, *parts):
            return BrokenResource()

    class BrokenResource:
        def joinpath(self, *parts):
            return self

        def read_text(self, **kwargs):
            raise OSError("read failed")

        def is_file(self):
            return False

    monkeypatch.setattr(colorpack, "_data_root", lambda: BrokenRoot())
    with pytest.raises(OSError, match="read failed"):
        colorpack.load_colorpack()
