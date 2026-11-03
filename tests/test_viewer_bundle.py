import hashlib
import json
from importlib.resources import files


def test_viewer_bundle_manifest_and_hashes() -> None:
    bundle = files("mcblueprint").joinpath("data", "viewer")
    manifest = json.loads(bundle.joinpath("manifest.json").read_text(encoding="utf-8"))

    assert manifest["source"] == "mc-blueprint/viewer"
    assert manifest["commit"] == "7b2e0f44bc53780499fb500d1fd6c1d5b52039c1"
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
        "assets/index-u8M9rc8l.js",
        "assets/worker-Yfb6Qb8X.js",
        "index.html",
    }
    assert bundle.joinpath("THIRD_PARTY_LICENSES.txt").is_file()

