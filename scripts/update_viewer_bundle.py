"""固定した viewer の build 済みファイルをパッケージへ取り込む。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

VIEWER_COMMIT = "5d9c62e006c167356a88f8147f2f5d7ee47051a6"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("viewer", type=Path, help="固定 viewer の build 用 worktree")
    args = parser.parse_args()
    viewer = args.viewer.resolve()
    commit = subprocess.run(
        ["git", "-C", str(viewer), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if commit != VIEWER_COMMIT:
        raise SystemExit(f"viewer commit が固定値と異なります: {commit}")

    dist = viewer / "dist"
    if not dist.is_dir():
        raise SystemExit("viewer/dist がありません。先に固定 viewer を build してください")
    source_files = sorted(path for path in dist.rglob("*") if path.is_file())
    if not source_files or any(path.is_symlink() for path in source_files):
        raise SystemExit("viewer/dist が空か、シンボリックリンクを含みます")
    invalid = [
        path.relative_to(dist).as_posix()
        for path in source_files
        if path.relative_to(dist).parts[0]
        not in {"index.html", "THIRD_PARTY_LICENSES.txt", "assets"}
        or (
            path.relative_to(dist).parts[0] == "assets"
            and len(path.relative_to(dist).parts) != 2
        )
    ]
    if invalid:
        raise SystemExit(f"配布対象外の dist ファイルがあります: {', '.join(invalid)}")
    if not (dist / "THIRD_PARTY_LICENSES.txt").is_file():
        raise SystemExit("THIRD_PARTY_LICENSES.txt がありません")
    if not (dist / "index.html").is_file():
        raise SystemExit("index.html がありません")

    package_root = Path(__file__).resolve().parents[1] / "src" / "mcblueprint" / "data" / "viewer"
    package_root.mkdir(parents=True, exist_ok=True)
    old_manifest = package_root / "manifest.json"
    if old_manifest.is_file():
        for entry in json.loads(old_manifest.read_text(encoding="utf-8"))["files"]:
            parts = Path(entry["path"]).parts
            if parts and all(part not in {".", ".."} for part in parts):
                stale = package_root.joinpath(*parts)
                if stale.is_file():
                    stale.unlink()
    entries = []
    for source in source_files:
        relative = source.relative_to(dist)
        target = package_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        if source.suffix.lower() in {".css", ".html", ".js", ".json", ".txt"}:
            content = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            target.write_text(content, encoding="utf-8", newline="\n")
        else:
            target.write_bytes(content)
        entries.append({"path": relative.as_posix(), "sha256": sha256(target)})

    manifest = {
        "source": "mc-blueprint/viewer",
        "commit": commit,
        "files": entries,
    }
    (package_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
