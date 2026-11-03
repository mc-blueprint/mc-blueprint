import hashlib
import json
from importlib.resources import files


def test_viewer_bundle_manifest_and_hashes() -> None:
    bundle = files("mcblueprint").joinpath("data", "viewer")
    manifest = json.loads(bundle.joinpath("manifest.json").read_text(encoding="utf-8"))

    assert manifest["source"] == "mc-blueprint/viewer"
    assert manifest["commit"] == "5d9c62e006c167356a88f8147f2f5d7ee47051a6"
    assert manifest["files"]
    for entry in manifest["files"]:
        content = bundle.joinpath(*entry["path"].split("/")).read_bytes()
        assert b"\r" not in content
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]


def test_viewer_bundle_contains_only_static_distribution() -> None:
    bundle = files("mcblueprint").joinpath("data", "viewer")
    manifest = json.loads(bundle.joinpath("manifest.json").read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in manifest["files"]}
    assert paths == {
        "THIRD_PARTY_LICENSES.txt",
        "assets/index-BFhRTilM.css",
        "assets/index-DqfXENRo.js",
        "assets/worker-Yfb6Qb8X.js",
        "index.html",
    }
    assert bundle.joinpath("THIRD_PARTY_LICENSES.txt").is_file()

