import hashlib
import json
from pathlib import Path

from mcblueprint import components

ROOT = Path(__file__).resolve().parent.parent
PATTERNS_DIR = ROOT / "src" / "mcblueprint" / "data" / "patterns"


def test_patterns_lock_matches_bundled_files() -> None:
    lock = json.loads(
        (ROOT / "src" / "mcblueprint" / "data" / "lock.json").read_text(encoding="utf-8")
    )
    entry = lock["patterns"]
    assert entry["repository"] == "https://github.com/mc-blueprint/mc-blueprint-patterns"
    assert entry["commit"] == "0aa54b7424bc4e3fca0907353e6edbc00ba507f8"

    digest = hashlib.sha256()
    for path in sorted(path for path in PATTERNS_DIR.rglob("*") if path.is_file()):
        digest.update(path.relative_to(PATTERNS_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    assert entry["sha256"] == digest.hexdigest()


def test_pattern_module_resolution_uses_bundled_data() -> None:
    assert components.resolve("medieval.window") == (
        components.PATTERNS_DIR / "medieval" / "window.json"
    )
