from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path

import pytest

from mcblueprint.assets import SUPPORTED_VERSIONS, _cached_assets_valid, ensure_assets
from mcblueprint.errors import BlueprintError


def _jar(path: Path, entries: dict[str, bytes] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in (entries or {
            "assets/minecraft/blockstates/stone.json": b"{}",
            "assets/minecraft/models/block/stone.json": b"{}",
            "assets/minecraft/textures/block/stone.png": b"png",
            "assets/minecraft/textures/block/subfolder/stone.png": b"png",
            "assets/minecraft/textures/entity/chest/normal.png": b"png",
            "assets/minecraft/textures/entity/zombie/zombie.png": b"excluded",
            "assets/minecraft/textures/colormap/grass.png": b"png",
            "assets/minecraft/sounds.json": b"excluded",
        }).items():
            archive.writestr(name, value)
    return path


def _settings(jar: Path) -> dict:
    return {"minecraft_dir": str(jar.parents[2]), "download": "false"}


@pytest.mark.parametrize("version", SUPPORTED_VERSIONS)
def test_extracts_only_viewer_assets_and_reuses_verified_cache(
    tmp_path: Path, version: str
) -> None:
    jar = _jar(tmp_path / f".minecraft/versions/{version}/{version}.jar")
    cache = tmp_path / "cache"
    result = ensure_assets(version, _settings(jar), cache_root=cache)

    assert sorted(
        path.relative_to(result).as_posix()
        for path in result.rglob("*")
        if path.name != "manifest.json"
    ) == [
        "assets",
        "assets/minecraft",
        "assets/minecraft/blockstates",
        "assets/minecraft/blockstates/stone.json",
        "assets/minecraft/models",
        "assets/minecraft/models/block",
        "assets/minecraft/models/block/stone.json",
        "assets/minecraft/textures",
        "assets/minecraft/textures/block",
        "assets/minecraft/textures/block/stone.png",
        "assets/minecraft/textures/block/subfolder",
        "assets/minecraft/textures/block/subfolder/stone.png",
        "assets/minecraft/textures/colormap",
        "assets/minecraft/textures/colormap/grass.png",
        "assets/minecraft/textures/entity",
        "assets/minecraft/textures/entity/chest",
        "assets/minecraft/textures/entity/chest/normal.png",
    ]
    manifest = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == str(jar.resolve())
    assert manifest["sha1"]
    assert manifest["extractedAt"]
    assert _cached_assets_valid(result)

    (result / "assets/minecraft/blockstates/stone.json").write_bytes(b"changed")
    assert not _cached_assets_valid(result)
    rebuilt = ensure_assets(version, _settings(jar), cache_root=cache)
    assert (rebuilt / "assets/minecraft/blockstates/stone.json").read_bytes() == b"{}"


def test_rejects_archive_traversal_without_replacing_previous_cache(tmp_path: Path) -> None:
    jar = _jar(tmp_path / ".minecraft/versions/26.3/26.3.jar")
    cache = tmp_path / "cache"
    assets = ensure_assets("26.3", _settings(jar), cache_root=cache)
    original_manifest = (assets / "manifest.json").read_bytes()
    _jar(jar, {"../escape": b"bad"})

    with pytest.raises(BlueprintError, match="不正な entry path"):
        ensure_assets("26.3", _settings(jar), cache_root=cache)

    assert (assets / "manifest.json").read_bytes() == original_manifest
    assert (tmp_path / "escape").exists() is False


def test_rejects_corrupt_zip_and_unknown_version(tmp_path: Path) -> None:
    jar = tmp_path / ".minecraft/versions/26.3/26.3.jar"
    jar.parent.mkdir(parents=True)
    jar.write_bytes(b"not a zip")
    with pytest.raises(BlueprintError, match="展開できません"):
        ensure_assets("26.3", _settings(jar), cache_root=tmp_path / "cache")
    with pytest.raises(BlueprintError, match="未対応"):
        ensure_assets("1.20", {}, cache_root=tmp_path / "cache")


def test_download_policy_false_and_noninteractive_ask_do_not_access_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("unexpected network request")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(BlueprintError, match="download=true"):
        ensure_assets(
            "26.3",
            {"minecraft_dir": str(tmp_path / "missing"), "download": "false"},
            cache_root=tmp_path / "cache",
        )


def test_official_download_is_sha1_verified_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("assets/minecraft/blockstates/stone.json", b"{}")
    jar_bytes = payload.getvalue()
    sha1 = hashlib.sha1(jar_bytes).hexdigest()

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    monkeypatch.setattr(
        "mcblueprint.assets._official_client", lambda version: ("https://example.test/jar", sha1)
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response(jar_bytes))
    cache = tmp_path / "cache"
    settings = {"minecraft_dir": str(tmp_path / "missing"), "download": "true"}

    assets = ensure_assets("26.3", settings, cache_root=cache)
    assert (assets / "assets/minecraft/blockstates/stone.json").read_bytes() == b"{}"
    cached_jar = cache / "jars/26.3.jar"
    assert cached_jar.read_bytes() == jar_bytes
    assert json.loads(cached_jar.with_suffix(".json").read_text(encoding="utf-8")) == {
        "source": "https://example.test/jar",
        "sha1": sha1,
    }

    def network_forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("verified cache should not use network")

    monkeypatch.setattr("urllib.request.urlopen", network_forbidden)
    (cache / "assets/26.3/manifest.json").unlink()
    assert ensure_assets("26.3", settings, cache_root=cache) == assets


def test_official_sha1_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "mcblueprint.assets._official_client",
        lambda version: ("https://example.test/jar", "0" * 40),
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: io.BytesIO(b"bad jar"))
    cache = tmp_path / "cache"
    with pytest.raises(BlueprintError, match="SHA-1 が一致しません"):
        ensure_assets(
            "26.3",
            {"minecraft_dir": str(tmp_path / "missing"), "download": "true"},
            cache_root=cache,
        )
    assert not (cache / "jars/26.3.jar").exists()
    with pytest.raises(BlueprintError, match="非対話"):
        ensure_assets(
            "26.3",
            {"minecraft_dir": str(tmp_path / "missing"), "download": "ask"},
            cache_root=tmp_path / "cache",
            stdin=io.StringIO(""),
        )


def test_concurrent_requests_publish_one_complete_cache(tmp_path: Path) -> None:
    jar = _jar(tmp_path / ".minecraft/versions/26.3/26.3.jar")
    cache = tmp_path / "cache"
    results: list[Path] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(ensure_assets("26.3", _settings(jar), cache_root=cache))
        except BaseException as exc:
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert results == [cache / "assets/26.3"] * 2
    assert _cached_assets_valid(cache / "assets/26.3")
