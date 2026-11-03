import multiprocessing
from pathlib import Path

import pytest

from mcblueprint.output_lock import lock_paths


def _hold(path: str, ready, release) -> None:
    with lock_paths(Path(path)):
        ready.set()
        release.wait(10)


def test_process_cannot_lock_same_output(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_hold, args=(str(tmp_path / "out"), ready, release))
    process.start()
    try:
        assert ready.wait(10)
        with pytest.raises(BlockingIOError):
            with lock_paths(tmp_path / "out"):
                pass
    finally:
        release.set()
        process.join(10)
    assert process.exitcode == 0


def test_lock_released_after_exception(tmp_path):
    path = tmp_path / "out"
    with pytest.raises(RuntimeError):
        with lock_paths(path):
            raise RuntimeError
    with lock_paths(path):
        pass


@pytest.mark.parametrize("suffix", [
    ".litematic", ".scene.json.gz", ".scene.json.gz.meta.json", ".schem.run.json",
    ".litematic.run.json",
])
def test_output_stem_shares_lock(tmp_path, suffix):
    with lock_paths(tmp_path / "same.schem"):
        with pytest.raises(BlockingIOError):
            with lock_paths(tmp_path / ("same" + suffix)):
                pass


def test_input_paths_keep_extensions(tmp_path):
    with lock_paths(tmp_path / "spec.yaml"):
        with lock_paths(tmp_path / "spec.yml", tmp_path / "spec.json"):
            pass
