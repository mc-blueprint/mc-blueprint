"""#210の比較基準fc8fbf9を展開し、全ファイルをGit blobと照合する。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "fc8fbf9fea3bf05506ec42b841bf678fb0dd001e"
FOLDER = ROOT / "output/issue-210/develop-fc8fbf9"


def prepare() -> None:
    """完成済み参照は書き換えず再照合する。不一致時は停止する。"""
    archive_path = FOLDER.with_suffix(".tar")
    if not FOLDER.exists():
        FOLDER.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "git", "-C", str(ROOT), "-c", "core.autocrlf=false", "archive", "--format=tar",
            f"--output={archive_path}", COMMIT,
        ], check=True)
        with tarfile.open(archive_path) as archive:
            archive.extractall(FOLDER, filter="data")
    entries = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-rz", COMMIT])
    paths = set()
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        info, encoded = entry.split(b"\t", 1)
        mode, kind, expected = info.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("参照コードに通常ファイル以外が含まれています")
        name = encoded.decode("utf-8")
        raw = (FOLDER / name).read_bytes()
        actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest().encode()
        if actual != expected:
            raise ValueError(f"参照コードがGit blobと一致しません: {name}")
        paths.add(name)
    actual_paths = {
        p.relative_to(FOLDER).as_posix() for p in FOLDER.rglob("*")
        if p.is_file() and not (p.suffix == ".pyc" and "__pycache__" in p.parts)
    }
    if actual_paths != paths:
        raise ValueError("参照コードに追加または欠落したファイルがあります")
    metadata = {"commit": COMMIT,
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "verified_blobs": len(paths), "root": str(FOLDER)}
    FOLDER.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n",
    )
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    prepare()
