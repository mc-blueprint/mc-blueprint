"""導入済みwheelの実API・同梱画面と公式アセットをヘッドレスで検証する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from importlib.resources import files
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

from mcblueprint.assets import (
    SUPPORTED_VERSIONS,
    _cache_root,
    _digest,
    _download_jar,
    _official_client,
    ensure_assets,
)
from mcblueprint.render import SWIFTSHADER_ARGS, render, setup
from mcblueprint.viewer_input import prepare_input
from mcblueprint.viewer_server import ViewerServer

READY = "() => window.viewer?.ready || document.querySelector('[role=alert]')"


def ready(page):
    page.wait_for_function(READY, timeout=120_000)
    assert page.locator('[role="alert"]').count() == 0, page.locator("#status").inner_text()
    assert page.evaluate("window.viewer.ready")


def select_block(page):
    canvas = page.locator("#viewport canvas")
    box = canvas.bounding_box()
    for y in (0.5, 0.6, 0.4, 0.7, 0.3):
        for x in (0.5, 0.6, 0.4, 0.7, 0.3):
            point = {"x": box["width"] * x, "y": box["height"] * y}
            canvas.click(position=point)
            if "minecraft:" in page.locator("#block-info").inner_text():
                page.locator("#pick").click()
                canvas.click(position=point)
                canvas.click(position=point)
                assert page.locator("#add").is_enabled()
                return
    raise AssertionError("3D画面上でブロックを選択できません")


def browser_edits(browser, prepared, assets, output):
    server = ViewerServer(prepared, assets)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("requestfailed", lambda req: failures.append({"url": req.url.split("#")[0],
                                                          "error": req.failure}))
    try:
        server.start()
        page.goto(server.url)
        ready(page)
        hashes = [prepared.scene_hash]
        for kind in ("replace", "delete"):
            select_block(page)
            page.locator("#kind").select_option(kind)
            if kind == "replace":
                page.locator("#value").fill("minecraft:gold_block")
            page.locator("#add").click()
            with page.expect_response(
                lambda response: response.url.endswith("/api/edits"),
            ) as saved:
                page.locator("#save").click()
            assert saved.value.status == 200, saved.value.text()
            page.locator("#edit-status").filter(has_text="edits を保存しました").wait_for()
            document = json.loads(prepared.edits_path.read_bytes())
            assert document["scene"] == hashes[-1]
            assert document["edits"][0]["kind"] == kind
            assert document["edits"][0]["id"] == "e1"
            with page.expect_navigation(wait_until="domcontentloaded", timeout=120_000):
                page.locator("#regenerate").click()
            ready(page)
            prepared.check_generation()
            hashes.append(prepared.scene_hash)
            assert hashes[-1] != hashes[-2]
        if prepared.spec:
            overrides = yaml.safe_load(prepared.input.read_bytes())["overrides"]
            assert [entry["id"] for entry in overrides] == [
                f"edit-{generation[:8]}-e1" for generation in hashes[:-1]
            ]
            select_block(page)
            page.locator("#kind").select_option("note")
            page.locator("#value").fill("入口を確認する")
            page.locator("#add").click()
            with page.expect_response(
                lambda response: response.url.endswith("/api/edits"),
            ) as noted:
                page.locator("#save").click()
            assert noted.value.status == 200, noted.value.text()
            assert noted.value.json()["notes"][0]["text"] == "入口を確認する"
            page.locator("#edit-status").filter(has_text="edits を保存しました").wait_for()
            with page.expect_navigation(wait_until="domcontentloaded", timeout=120_000):
                page.locator("#regenerate").click()
            ready(page)
            root = next(s for s in prepared.scene["sources"] if s.get("params"))
            param, value = (("height", 14) if prepared.input.stem == "tower" else ("roof", "hip"))
            assert param in root["params"]
            edits = [
                {"id": "param", "kind": "param", "element": root["element"],
                 "param": param, "value": value},
            ]
            headers = {"Authorization": f"Bearer {server.token}"}
            body = {"formatVersion": 1, "scene": prepared.scene_hash, "edits": edits}
            saved = page.request.post(server.origin + "/api/edits", headers=headers, data=body)
            assert saved.status == 200, saved.text()
            with page.expect_navigation(wait_until="domcontentloaded", timeout=120_000):
                regenerated = page.request.post(server.origin + "/api/regenerate",
                                                headers=headers, data={})
                assert regenerated.status == 200, regenerated.text()
            ready(page)
            prepared.check_generation()
            assert yaml.safe_load(prepared.input.read_bytes())["elements"][root["element"]][
                param] == value
            body["scene"] = prepared.scene_hash
            body["edits"][0]["param"] = "unknown"
            assert page.request.post(server.origin + "/api/edits", headers=headers,
                                     data=body).status == 422
        else:
            for kind in ("param", "note"):
                assert page.locator(f'#kind option[value="{kind}"]').evaluate(
                    "option => option.hidden || option.disabled",
                )
            headers = {"Authorization": f"Bearer {server.token}"}
            for kind in ("param", "note"):
                entry = ({"param": "height", "value": 14} if kind == "param"
                         else {"text": "拒否される指示"})
                body = {"formatVersion": 1, "scene": prepared.scene_hash,
                        "edits": [{"id": "bad", "kind": kind, "element": "house#1", **entry}]}
                assert page.request.post(server.origin + "/api/edits", headers=headers,
                                         data=body).status == 422
        page.screenshot(path=str(output / f"{prepared.input.stem}-edited.png"))
        assert not errors, errors
        assert not [f for f in failures if "/api/assets/" in f["url"]], failures
        return {"input": prepared.input.name, "hashes": hashes, "ready": True,
                "sseReload": True, "pageErrors": errors, "requestFailures": failures,
                "uiEdits": ["replace", "delete", "note"] if prepared.spec
                else ["replace", "delete"],
                "apiParam": param if prepared.spec else None}
    finally:
        page.close()
        server.close()
        assert not server.thread.is_alive()
        assert not server.listeners


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="リポジトリ外の証跡保存先")
    parser.add_argument("--setup", action="store_true", help="対応Chromiumを取得する")
    parser.add_argument("--download-assets", action="store_true", help="公式jarの取得を許可する")
    parser.add_argument("--browser-cache", type=Path, help="標準cacheが起動できない環境の検証用")
    parser.add_argument("--skip-render", action="store_true", help="成功済みPNGでUI検証を再開する")
    args = parser.parse_args()
    if args.browser_cache is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(args.browser_cache.resolve())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.setup:
        setup()
    report = {"python": sys.version, "browserPathOverride": os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
              "assets": [], "renders": [], "edits": []}
    bundle = files("mcblueprint").joinpath("data/viewer")
    manifest = json.loads(bundle.joinpath("manifest.json").read_bytes())
    for entry in manifest["files"]:
        assert hashlib.sha256(bundle.joinpath(entry["path"]).read_bytes()).hexdigest() == entry[
            "sha256"]
    licenses = bundle.joinpath("THIRD_PARTY_LICENSES.txt").read_text(encoding="utf-8")
    for dependency in ("three", "deepslate", "ajv"):
        assert dependency in licenses
    report["bundle"] = {"path": str(bundle), "commit": manifest["commit"],
                        "files": len(manifest["files"]), "licenses": True}
    assets_by_version = {}
    for version in SUPPORTED_VERSIONS:
        if args.download_assets:
            url, expected = _official_client(version)
            jar = _cache_root() / "jars" / f"{version}.jar"
            if not jar.is_file() or _digest(jar) != expected:
                jar = _download_jar(url, expected, _cache_root(), version)
            assert _digest(jar) == expected
            report["assets"].append({"version": version, "officialSha1": expected})
        assets = ensure_assets(version, {"minecraft_dir": str(output / "missing-minecraft"),
                                         "download": "false"})
        assets_by_version[version] = assets
        source = output / f"blocks-{version}.json"
        source.write_text(json.dumps({
            "formatVersion": 1, "name": "配布受入", "minecraftVersion": version,
            "operations": [{"type": "fill", "from": [0, 0, 0], "to": [3, 0, 3],
                            "block": "minecraft:stone"},
                           {"type": "set", "position": [1, 1, 1],
                            "block": "minecraft:oak_stairs[facing=east]"},
                           {"type": "set", "position": [2, 1, 2],
                            "block": "minecraft:glass"}],
        }, ensure_ascii=False), encoding="utf-8", newline="\n")
        prepared = prepare_input(source, output=output / version, overrides=[
            "preview.quality=low", "render.gpu=swiftshader", "assets.download=false",
        ])
        paths = ([output / "renders" / f"blocks-{version}-{view}.png"
                  for view in ("iso", "front", "top")] if args.skip_render else
                 render(prepared, assets, output / "renders", out=sys.stdout))
        for path in paths:
            assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            report["renders"].append({"file": path.name,
                                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        print(f"render {version}: OK", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chromium", headless=True,
                                              chromium_sandbox=True, args=SWIFTSHADER_ARGS)
        try:
            for archetype in ("house", "tower", "blueprint"):
                if archetype == "blueprint":
                    source = output / "blocks-1.21.11.json"
                else:
                    source = output / f"{archetype}.yaml"
                    source.write_text(yaml.safe_dump({
                        "minecraftVersion": "1.21.11", "archetype": archetype,
                        "scale": "small", "seed": 42,
                        "config": {"weathering": {"age": 0}},
                    }), encoding="utf-8", newline="\n")
                prepared = prepare_input(source, output=output / archetype,
                                         overrides=["preview.quality=low"])
                report["edits"].append(browser_edits(browser, prepared,
                                                     assets_by_version["1.21.11"], output))
                print(f"UI {archetype}: OK", flush=True)
            report["browser"] = browser.version
        finally:
            browser.close()
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
    print("配布受入: OK")


if __name__ == "__main__":
    main()
