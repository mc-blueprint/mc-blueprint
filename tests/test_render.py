import asyncio
import builtins
import io
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mcblueprint import render as module
from mcblueprint.cli import main
from mcblueprint.errors import BlueprintError


@pytest.mark.parametrize("arguments", [
    [], ["--views", "iso,iso"], ["--views", "north"], ["--views", ""],
    ["--setup", "input.json"], ["--setup", "--set", "render.gpu=swiftshader"],
    ["--setup", "--views", "iso,front,top"], ["--setup", "--out", "preview"],
])
def test_cli_rejects_arguments_before_input_access(arguments, capsys):
    if arguments and arguments[0] == "--views":
        arguments = ["missing.json", *arguments]
    assert main(["render", *arguments]) == 2
    assert capsys.readouterr().err


def test_missing_extra(monkeypatch):
    original = builtins.__import__

    def without_playwright(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise ImportError("missing")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_playwright)
    with pytest.raises(BlueprintError, match=r"mc-blueprint\[render\]"):
        module.setup()


@pytest.mark.parametrize("code", [0, 1])
def test_setup_uses_current_python_and_no_os_install(monkeypatch, code):
    calls = []
    monkeypatch.setattr(module, "_playwright", lambda: None)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(module.subprocess, "run", run)
    if code:
        with pytest.raises(BlueprintError, match="通信環境"):
            module.setup()
    else:
        assert main(["render", "--setup"]) == 0
    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"]]


def test_setup_start_failure(monkeypatch):
    monkeypatch.setattr(module, "_playwright", lambda: None)

    def fail(*args, **kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(BlueprintError, match="取得を開始できません"):
        module.setup()


@asynccontextmanager
async def driver():
    yield object()


@pytest.mark.parametrize("gpu,failures,expected", [
    ("auto", [module._WebGLUnavailable("no context")], [False, True]),
    ("auto", [BlueprintError("asset 404")], [False]),
    ("auto", [module._WebGLUnavailable("no context")] * 2, [False, True]),
    ("swiftshader", [module._WebGLUnavailable("no context")], [True]),
    ("swiftshader", [], [True]),
])
def test_retry_only_context_failure(monkeypatch, gpu, failures, expected):
    calls = []

    async def capture(*args):
        calls.append(args[4])
        if failures:
            raise failures.pop(0)
        return {"iso": b"png"}

    monkeypatch.setattr(module, "_capture", capture)
    task = module._render(driver, object(), ("iso",),
                          {"gpu": gpu, "timeout_seconds": 1}, "vanilla", io.StringIO())
    if len(failures) >= len(expected):
        with pytest.raises(BlueprintError):
            asyncio.run(task)
    else:
        assert asyncio.run(task) == {"iso": b"png"}
    assert calls == expected


def test_retry_shares_deadline_and_cancels_capture(monkeypatch):
    calls, closed = [], []

    async def capture(*args):
        calls.append(args[4])
        try:
            if len(calls) == 1:
                await asyncio.sleep(0.2)
                raise module._WebGLUnavailable("no context")
            await asyncio.sleep(0.4)
            return {}
        finally:
            closed.append(True)

    monkeypatch.setattr(module, "_capture", capture)
    with pytest.raises(BlueprintError, match="共通上限"):
        asyncio.run(module._render(driver, object(), ("iso",),
                                   {"gpu": "auto", "timeout_seconds": 0.5},
                                   "vanilla", io.StringIO()))
    assert calls == [False, True]
    assert len(closed) == 2


def test_deadline_includes_driver_startup(monkeypatch):
    capture = AsyncMock()
    closed = []
    monkeypatch.setattr(module, "_capture", capture)

    @asynccontextmanager
    async def slow_driver():
        await asyncio.sleep(0.05)
        try:
            yield object()
        finally:
            closed.append(True)

    with pytest.raises(BlueprintError, match="共通上限"):
        asyncio.run(module._render(slow_driver, object(), ("iso",),
                                   {"gpu": "auto", "timeout_seconds": 0.01},
                                   "vanilla", io.StringIO()))
    capture.assert_not_awaited()
    assert closed == [True]


def browser_fixture(monkeypatch, *, alert=None, renderer="ANGLE (SwiftShader)", fail=None):
    canvas = SimpleNamespace(screenshot=AsyncMock(return_value=b"png"))
    alerts = SimpleNamespace(all_text_contents=AsyncMock(return_value=alert or []))
    page = SimpleNamespace(
        evaluate=AsyncMock(return_value=renderer), goto=AsyncMock(side_effect=fail),
        wait_for_function=AsyncMock(),
        locator=lambda selector: alerts if selector == '[role="alert"]' else canvas,
    )
    context = SimpleNamespace(route=AsyncMock(), new_page=AsyncMock(return_value=page),
                              close=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock(),
                              version="test-version")
    chromium = SimpleNamespace(launch=AsyncMock(return_value=browser))
    return SimpleNamespace(chromium=chromium), browser, context, page, canvas


@pytest.mark.parametrize("failure", ["alert", "navigation", "context", "renderer", None])
def test_capture_contract_and_cleanup(monkeypatch, failure):
    playwright, browser, context, page, canvas = browser_fixture(
        monkeypatch, alert=["asset missing"] if failure == "alert" else [],
        renderer=None if failure == "context" else (
            "hardware" if failure == "renderer" else "ANGLE (SwiftShader)"),
        fail=RuntimeError("navigation") if failure == "navigation" else None,
    )
    server = SimpleNamespace(origin="http://127.0.0.1:1234", token="secret")
    task = module._capture(playwright, server, module.VIEWS, "soft", True, io.StringIO())
    if failure:
        with pytest.raises((BlueprintError, RuntimeError)):
            asyncio.run(task)
        canvas.screenshot.assert_not_awaited()
    else:
        assert asyncio.run(task) == dict.fromkeys(module.VIEWS, b"png")
        assert page.goto.await_count == 3
        assert "?still=iso&preset=soft#token=secret" in page.goto.call_args_list[0].args[0]
        assert page.wait_for_function.call_args.args == (module.READY,)
        canvas.screenshot.assert_awaited_with(type="png", timeout=0)
    browser.close.assert_awaited_once()
    context.close.assert_awaited_once()
    playwright.chromium.launch.assert_awaited_once_with(
        channel="chromium", headless=True, chromium_sandbox=True, args=module.SWIFTSHADER_ARGS,
    )
    browser.new_context.assert_awaited_once_with(
        viewport={"width": 1280, "height": 720}, device_scale_factor=1, service_workers="block",
    )
    guard = context.route.call_args.args[1]
    for url, allowed in [(server.origin + "/api/scene", True), ("https://example.com", False),
                         (server.origin + ".evil.com/api/scene", False)]:
        route = SimpleNamespace(request=SimpleNamespace(url=url), continue_=AsyncMock(),
                                abort=AsyncMock())
        asyncio.run(guard(route))
        assert route.continue_.await_count == int(allowed)
        assert route.abort.await_count == int(not allowed)


def test_context_creation_failure_closes_browser(monkeypatch):
    playwright, browser, context, page, canvas = browser_fixture(monkeypatch)
    browser.new_context.side_effect = RuntimeError("OS dependency missing")
    with pytest.raises(RuntimeError, match="OS dependency missing"):
        asyncio.run(module._capture(playwright, object(), module.VIEWS,
                                    "vanilla", False, io.StringIO()))
    browser.close.assert_awaited_once()
    playwright.chromium.launch.assert_awaited_once_with(
        channel="chromium", headless=True, chromium_sandbox=True, args=[],
    )


@pytest.mark.parametrize("fail", [True, False])
def test_render_preserves_images_and_closes_server(monkeypatch, tmp_path, fail):
    from unittest.mock import Mock

    prepared = SimpleNamespace(input=Path("test.scene.json.gz"), config={
        "render": {"gpu": "auto", "timeout_seconds": 1}, "preview": {"shader": "vanilla"},
    })
    server = SimpleNamespace(start=Mock(), close=Mock(), token="sensitive")
    monkeypatch.setattr(module, "ViewerServer", lambda *args: server)
    monkeypatch.setattr(module, "_playwright", lambda: driver)
    monkeypatch.setattr(module, "_render", AsyncMock(
        side_effect=RuntimeError("failure sensitive") if fail else None,
        return_value={"iso": b"new png"},
    ))
    path = tmp_path / "test-iso.png"
    path.write_bytes(b"previous png")
    if fail:
        with pytest.raises(BlueprintError, match="failure <token>"):
            module.render(prepared, tmp_path, tmp_path)
        assert path.read_bytes() == b"previous png"
    else:
        assert module.render(prepared, tmp_path, tmp_path) == [path]
        assert path.read_bytes() == b"new png"
    server.close.assert_called_once()
