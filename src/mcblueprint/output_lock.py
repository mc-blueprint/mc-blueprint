"""入力と出力を対象にしたプロセス間排他。"""

from __future__ import annotations

import os
import threading
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path

_guard = threading.Lock()
_held: set[str] = set()


def _key(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    for suffix in (".scene.json.gz.meta.json", ".scene.json.gz", ".schem.run.json",
                   ".litematic.run.json", ".schem", ".litematic"):
        if normalized.endswith(suffix):
            return normalized.removesuffix(suffix)
    return normalized


@contextmanager
def lock_paths(*paths: Path):
    """複数パスを一定順・非待機でロックし、with終了時に解放する。

    schem・litematic・scene・scene sidecar・runは同じ出力stemを共有する。
    その他の入力パスは拡張子を含めて正規化する。競合はBlockingIOError。
    """
    keys = sorted({_key(Path(path)) for path in paths})
    lock_dir = Path.home() / ".cache" / "mc-blueprint" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    acquired: list[str] = []
    with _guard:
        if any(key in _held for key in keys):
            raise BlockingIOError("対象はすでに処理中です")
        _held.update(keys)
        acquired.extend(keys)
    try:
        with ExitStack() as stack:
            for key in keys:
                name = sha256(key.encode("utf-8")).hexdigest() + ".lock"
                stream = stack.enter_context((lock_dir / name).open("a+b"))
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"0")
                        stream.flush()
                    stream.seek(0)
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as exc:
                        if exc.errno in (13, 36):
                            raise BlockingIOError("対象はすでに処理中です") from exc
                        raise
                    fd = stream.fileno()
                    stack.callback(_unlock_windows, fd)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
    finally:
        with _guard:
            for key in acquired:
                _held.discard(key)


def _unlock_windows(fd: int) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
