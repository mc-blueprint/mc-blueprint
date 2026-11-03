"""同梱viewerを専用のヘッドレスChromiumで撮影する。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from mcblueprint.errors import BlueprintError
from mcblueprint.viewer_input import ViewerInput, atomic_write
from mcblueprint.viewer_server import ViewerServer

VIEWS = ("iso", "front", "top")
VIEWPORT = {"width": 1280, "height": 720}
SWIFTSHADER_ARGS = [
    "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
]
INSTALL = 'python -m pip install "mc-blueprint[render]"'
SETUP = "mcblueprint render --setup"
PROBE = """() => {
    const gl = document.createElement('canvas').getContext('webgl2');
    if (!gl) return null;
    const debug = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER);
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return renderer;
}"""
READY = """() => window.viewer?.ready === true ||
    document.querySelector('[role="alert"]') !== null"""


def _playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BlueprintError(f"render extraが必要です。{INSTALL} を実行してください") from exc
    return async_playwright


def setup() -> None:
    """現在のPythonのPlaywrightに対応するChromiumのみ取得する。OS依存は導入しない。"""
    _playwright()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"],
            check=False,
        )
    except OSError as exc:
        raise BlueprintError(f"Chromiumの取得を開始できません。{INSTALL}: {exc}") from exc
    if result.returncode:
        raise BlueprintError(
            f"Chromiumの取得に失敗しました。通信環境を確認し {SETUP} を再実行してください"
        )


class _WebGLUnavailable(BlueprintError):
    pass


async def _capture(playwright, server, views, preset, software, out):
    browser = await playwright.chromium.launch(
        channel="chromium", headless=True, chromium_sandbox=True,
        args=SWIFTSHADER_ARGS if software else [],
    )
    try:
        context = await browser.new_context(
            viewport=VIEWPORT, device_scale_factor=1, service_workers="block",
        )
        try:
            async def guard(route):
                url = urlsplit(route.request.url)
                if f"{url.scheme}://{url.netloc}" == server.origin:
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", guard)
            page = await context.new_page()
            renderer = await page.evaluate(PROBE)
            if renderer is None:
                raise _WebGLUnavailable("WebGL2 contextを作成できません")
            print(f"Chromium {browser.version}; WebGL renderer: {renderer}", file=out)
            if software and "swiftshader" not in renderer.lower():
                raise BlueprintError(f"SwiftShaderが使われていません: {renderer}")
            images = {}
            for view in views:
                query = urlencode({"still": view, "preset": preset})
                await page.goto(f"{server.origin}/?{query}#token={server.token}",
                                wait_until="domcontentloaded", timeout=0)
                await page.wait_for_function(READY, timeout=0)
                alert = await page.locator('[role="alert"]').all_text_contents()
                if alert:
                    raise BlueprintError(f"viewer: {'; '.join(alert)}")
                images[view] = await page.locator("#viewport canvas").screenshot(
                    type="png", timeout=0,
                )
            return images
        finally:
            await context.close()
    finally:
        await browser.close()


async def _render(playwright_factory, server, views, config, preset, out):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + config["timeout_seconds"]
    try:
        # ドライバーの起動途中はキャンセルせず、入場後に期限を検査して確実に閉じる。
        async with playwright_factory() as playwright:
            async with asyncio.timeout_at(deadline):
                if loop.time() >= deadline:
                    raise TimeoutError
                software = config["gpu"] == "swiftshader"
                try:
                    return await _capture(playwright, server, views, preset, software, out)
                except _WebGLUnavailable:
                    if software:
                        raise
                    print("WebGL2が利用できないためSwiftShaderで一度再起動します", file=out)
                    return await _capture(playwright, server, views, preset, True, out)
    except TimeoutError as exc:
        raise BlueprintError(
            f"{config['timeout_seconds']}秒の共通上限を超えました。"
            "入力・アセットと render.timeout_seconds を確認してください"
        ) from exc


def render(prepared: ViewerInput, assets: Path, output: Path, *,
           views: tuple[str, ...] = VIEWS, out=None) -> list[Path]:
    """準備済み入力を撮影し、全視点成功後に各PNGを原子的に公開する。

    viewportは1280×720、倍率1。設定の共通期限内に全視点を撮影する。
    失敗はBlueprintErrorとし、ブラウザとサーバーを閉じる。撮影失敗時は既存PNGを保つ。
    公開中のI/O失敗で複数PNG全体のロールバックは行わない。
    """
    if not views or len(set(views)) != len(views) or any(v not in VIEWS for v in views):
        raise BlueprintError("--viewsはiso,front,topから重複なしで指定してください")
    factory = _playwright()
    server = ViewerServer(prepared, assets)
    try:
        server.start()
        images = asyncio.run(_render(factory, server, views, prepared.config["render"],
                                     prepared.config["preview"]["shader"], out))
        stem = prepared.input.name.removesuffix(".scene.json.gz")
        if stem == prepared.input.name:
            stem = prepared.input.stem
        paths = []
        for view, image in images.items():
            path = Path(output) / f"{stem}-{view}.png"
            atomic_write(path, image)
            paths.append(path)
        return paths
    except Exception as exc:
        message = str(exc).replace(server.token, "<token>")
        raise BlueprintError(
            f"render: {message}\nブラウザ未導入なら {SETUP} を実行してください。"
            "LinuxのOS依存不足はPlaywright公式のシステム要件を確認してください。"
            "WebGL2不可なら --set render.gpu=swiftshader を指定してください"
        ) from exc
    finally:
        server.close()
