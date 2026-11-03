"""Minecraft jar から viewer 用アセットを安全に用意する。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO

from mcblueprint.errors import BlueprintError

VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
SUPPORTED_VERSIONS = ("1.21.11", "26.1.2", "26.2", "26.3")
_CHUNK_SIZE = 1024 * 1024
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def _default_minecraft_dir() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / ".minecraft"
    return Path.home() / ".minecraft"


def _cache_root() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "mc-blueprint" / "cache"
    return Path.home() / ".cache" / "mc-blueprint"


def _read_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            value = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise BlueprintError(f"Minecraft の公式メタデータを取得できません: {exc}") from exc
    if not isinstance(value, dict):
        raise BlueprintError("Minecraft の公式メタデータが不正です")
    return value


def _official_client(version: str) -> tuple[str, str]:
    manifest = _read_json(VERSION_MANIFEST_URL)
    entry = next((item for item in manifest.get("versions", []) if item.get("id") == version), None)
    if entry is None or not isinstance(entry.get("url"), str):
        raise BlueprintError(f"Minecraft {version} の公式版情報がありません")
    metadata = _read_json(entry["url"])
    client = metadata.get("downloads", {}).get("client", {})
    url, sha1 = client.get("url"), client.get("sha1")
    if not isinstance(url, str) or not isinstance(sha1, str) or len(sha1) != 40:
        raise BlueprintError(f"Minecraft {version} の公式 client 情報が不正です")
    return url, sha1.lower()


def _digest(path: Path) -> str:
    value = hashlib.sha1()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            value.update(chunk)
    return value.hexdigest()


def _local_jars(minecraft_dir: Path, version: str) -> Iterator[Path]:
    yield minecraft_dir / "versions" / version / f"{version}.jar"


def _verified_cached_jar(cache: Path, version: str) -> tuple[Path, str, str] | None:
    jar = cache / "jars" / f"{version}.jar"
    metadata = jar.with_suffix(".json")
    try:
        if metadata.is_file():
            data = json.loads(metadata.read_text(encoding="utf-8"))
        else:
            data = json.loads(
                (cache / "assets" / version / "manifest.json").read_text(encoding="utf-8")
            )
        expected = data["sha1"]
        source = data.get("source")
        if (
            not isinstance(source, str)
            or not source.startswith("https://")
            or not isinstance(expected, str)
        ):
            return None
        if jar.is_file() and _digest(jar) == expected:
            return jar, expected, source
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def _download_jar(url: str, expected_sha1: str, cache: Path, version: str) -> Path:
    directory = cache / "jars"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{version}.jar"
    fd, name = tempfile.mkstemp(prefix=f".{version}-", suffix=".part", dir=directory)
    temporary = Path(name)
    try:
        digest = hashlib.sha1()
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(url, timeout=60) as response:
            while chunk := response.read(_CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected_sha1:
            raise BlueprintError(
                f"Minecraft {version} jar の SHA-1 が一致しません: "
                f"expected {expected_sha1}, got {actual}"
            )
        os.replace(temporary, destination)
        metadata = destination.with_suffix(".json")
        temp_metadata = temporary.with_suffix(".json")
        temp_metadata.write_text(
            json.dumps({"source": url, "sha1": actual}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_metadata, metadata)
        return destination
    except (OSError, urllib.error.URLError) as exc:
        raise BlueprintError(f"Minecraft {version} jar を取得できません: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_suffix(".json").unlink(missing_ok=True)


def _selected(path: PurePosixPath) -> bool:
    parts = path.parts
    if len(parts) < 4 or parts[:2] != ("assets", "minecraft"):
        return False
    category = parts[2:]
    if category[0] == "blockstates":
        return len(category) == 2 and category[1].endswith(".json")
    if category[0] == "models":
        return len(category) >= 2 and category[-1].endswith(".json")
    if category[:2] == ("textures", "block"):
        return len(category) >= 3 and category[-1].endswith(".png")
    if category[:2] == ("textures", "colormap"):
        return len(category) == 3 and category[-1].endswith(".png")
    return (
        len(category) >= 4
        and category[:2] == ("textures", "entity")
        and category[2] in {"chest", "bed"}
        and category[-1].endswith(".png")
    )


def _safe_archive_path(name: str) -> PurePosixPath:
    if "\\" in name or name.startswith("/") or "\x00" in name:
        raise BlueprintError(f"jar に不正な entry path があります: {name!r}")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} or ":" in part for part in path.parts):
        raise BlueprintError(f"jar に不正な entry path があります: {name!r}")
    return path


def _extract(jar: Path, destination: Path) -> list[str]:
    extracted: list[str] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(jar) as archive:
            for entry in archive.infolist():
                path = _safe_archive_path(entry.filename)
                if entry.is_dir():
                    continue
                mode = entry.external_attr >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG) and file_type != stat.S_IFLNK:
                    raise BlueprintError(
                        f"jar に通常ファイルでない entry があります: {entry.filename}"
                    )
                if file_type == stat.S_IFLNK:
                    raise BlueprintError(
                        f"jar に symlink entry があります: {entry.filename}"
                    )
                if not _selected(path):
                    continue
                normalized = path.as_posix()
                if normalized in seen:
                    raise BlueprintError(f"jar に重複した asset path があります: {normalized}")
                seen.add(normalized)
                output = destination.joinpath(*path.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, output.open("xb") as target:
                    shutil.copyfileobj(source, target)
                extracted.append(normalized)
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise BlueprintError(f"Minecraft jar を展開できません: {exc}") from exc
    if not extracted:
        raise BlueprintError("Minecraft jar に対象アセットがありません")
    return sorted(extracted)


@contextmanager
def _version_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    with process_lock, path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _cached_assets_valid(directory: Path, sha1: str | None = None) -> bool:
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        hashes = manifest["fileSha256"]
        if (
            not isinstance(files, list)
            or not files
            or not isinstance(hashes, dict)
            or (sha1 is not None and manifest.get("sha1") != sha1)
        ):
            return False
        for name in files:
            path = _safe_archive_path(name)
            if not _selected(path) or (
                _file_sha256(directory / Path(*path.parts)) != hashes.get(name)
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, BlueprintError):
        return False


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_assets(
    jar: Path,
    destination: Path,
    *,
    version: str,
    source: str,
    sha1: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=destination.parent))
    backup = destination.with_name(f".{destination.name}.previous")
    try:
        files = _extract(jar, staging)
        file_hashes = {
            name: _file_sha256(staging / Path(*PurePosixPath(name).parts)) for name in files
        }
        manifest = {
            "source": source,
            "sha1": sha1,
            "extractedAt": datetime.now(UTC).isoformat(),
            "files": files,
            "fileSha256": file_hashes,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if backup.exists():
            shutil.rmtree(backup)
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except OSError:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def ensure_assets(
    version: str,
    settings: dict,
    *,
    cache_root: Path | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> Path:
    """viewer 用アセットを返す。必要な場合だけ設定に従い jar を取得・展開する。"""
    if version not in SUPPORTED_VERSIONS:
        raise BlueprintError(f"未対応の Minecraft 版です: {version}")
    cache = cache_root or _cache_root()
    destination = cache / "assets" / version
    with _version_lock(cache / "locks" / f"assets-{version}.lock"):
        minecraft_dir = Path(settings.get("minecraft_dir") or _default_minecraft_dir()).expanduser()
        jar = next((path for path in _local_jars(minecraft_dir, version) if path.is_file()), None)
        source = "local"
        expected_sha1: str | None = None
        if jar is not None:
            expected_sha1 = _digest(jar)
            source = str(jar.resolve())
            if _cached_assets_valid(destination, expected_sha1):
                return destination
        elif _cached_assets_valid(destination):
            return destination
        else:
            cached = _verified_cached_jar(cache, version)
            if cached:
                jar, expected_sha1, source = cached
            else:
                policy = settings.get("download", "ask")
                if policy not in ("ask", "true", "false", True, False):
                    raise BlueprintError(f"assets.download の値が不正です: {policy!r}")
                if policy == "false" or policy is False:
                    raise BlueprintError(
                        f"Minecraft {version} の jar がありません。assets.download=true "
                        "または assets.minecraft_dir を設定してください"
                    )
                if policy == "ask":
                    input_stream, output_stream = stdin or sys.stdin, stdout or sys.stdout
                    if not input_stream.isatty():
                        raise BlueprintError(
                            "Minecraft jar の取得確認が必要です。非対話環境では "
                            "assets.download=true を設定してください"
                        )
                    print(
                        f"Minecraft {version} の公式 jar を取得しますか? [y/N] ",
                        end="",
                        file=output_stream,
                        flush=True,
                    )
                    if input_stream.readline().strip().lower() not in {"y", "yes"}:
                        raise BlueprintError("Minecraft jar の取得は許可されませんでした")
                url, expected_sha1 = _official_client(version)
                jar = _download_jar(url, expected_sha1, cache, version)
                source = url

        assert jar is not None and expected_sha1 is not None
        if source.startswith("https://") and _digest(jar) != expected_sha1:
            raise BlueprintError(f"Minecraft {version} jar の SHA-1 が一致しません")
        _publish_assets(
            jar,
            destination,
            version=version,
            source=source,
            sha1=expected_sha1,
        )
        return destination
