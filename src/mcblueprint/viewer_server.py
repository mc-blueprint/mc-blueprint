"""Bearer認証付きloopback専用viewerサーバー。任意ファイルやコマンドは扱わない。"""

from __future__ import annotations

import hmac
import json
import mimetypes
import queue
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from mcblueprint.assets import _selected
from mcblueprint.errors import BlueprintError
from mcblueprint.pipeline import PipelineError
from mcblueprint.viewer_input import EditConflict, ViewerInput

MAX_BODY = 1024 * 1024


class ViewerServer(ThreadingHTTPServer):
    """127.0.0.1の空きポートで配信する。closeでSSEと要求スレッドを終了する。

    startはバックグラウンド、serve_foreverは前景用。起動した所有者が必ずcloseする。
    assetsはensure_assetsが返す版ディレクトリで、要求から取得・変更しない。
    """

    daemon_threads = False

    def __init__(self, prepared: ViewerInput, assets: Path):
        self.prepared = prepared
        self.assets = assets.resolve()
        self.version = prepared.scene["minecraftVersion"]
        self.token = secrets.token_urlsafe(32)
        self.gate = threading.Lock()
        self.stopping = threading.Event()
        self.listeners: set[queue.Queue] = set()
        self.listeners_lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.bundle = files("mcblueprint").joinpath("data/viewer")
        manifest = json.loads(self.bundle.joinpath("manifest.json").read_text(encoding="utf-8"))
        self.static = {entry["path"] for entry in manifest["files"]}
        super().__init__(("127.0.0.1", 0), _Handler)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    @property
    def url(self) -> str:
        mode = "" if self.prepared.spec else "?editMode=blocks"
        return f"{self.origin}/{mode}#token={self.token}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.serve_forever, name="viewer-server")
        self.thread.start()

    def close(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.shutdown()
            self.thread.join()
        self.server_close()

    def notify(self, result: dict) -> None:
        with self.listeners_lock:
            for listener in self.listeners:
                listener.put(result)


class _Handler(BaseHTTPRequestHandler):
    server: ViewerServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        pass

    def _reply(self, status: int, raw: bytes = b"", content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _error(self, status: int, message: str):
        self._reply(status, json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))

    def _guard(self, path: str) -> bool:
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        host = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if len(host) != 1 or host[0] not in hosts:
            self._error(403, "Hostが許可されていません")
            return False
        if origins and origins != [f"http://{host[0]}"]:
            self._error(403, "Originが許可されていません")
            return False
        if path.startswith("/api/"):
            auth = self.headers.get_all("Authorization", [])
            expected = "Bearer " + self.server.token
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected.encode()):
                self._error(401, "Bearer認証が必要です")
                return False
        return True

    def _path(self) -> str | None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path, errors="strict")
        except (ValueError, UnicodeError):
            self._error(404, "パスが許可されていません")
            return None
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or not path.startswith("/") or path.startswith("//")
                or any(c in path for c in ("\\", ":", "%", "\x00"))
                or any(part in (".", "..", "") for part in path[1:].split("/"))):
            if (path == "/" and not parsed.scheme and not parsed.netloc
                    and not parsed.fragment):
                return path
            self._error(404, "パスが許可されていません")
            return None
        return path

    def do_GET(self):
        try:
            path = self._path()
            if path is None or not self._guard(path):
                return
            # viewerの表示パラメータは静的入口だけで解釈する。
            if path == "/":
                raw = self.server.bundle.joinpath("index.html").read_bytes()
                self._reply(200, raw, "text/html")
                return
            if path == "/api/scene":
                with self.server.gate:
                    raw = self.server.prepared.raw_scene
                import gzip

                self._reply(200, gzip.compress(raw, mtime=0), "application/gzip")
            elif path == "/api/events":
                self._events()
            elif path.startswith("/api/assets/"):
                prefix = f"/api/assets/{self.server.version}/"
                if not path.startswith(prefix):
                    self._error(404, "版が一致しません")
                    return
                relative = path[len(prefix):]
                target = (self.server.assets / relative).resolve()
                if not target.is_relative_to(self.server.assets) or not _selected(
                        PurePosixPath(relative)):
                    self._error(403, "アセットの範囲外です")
                elif not target.is_file():
                    self._error(404, "アセットがありません")
                else:
                    self._reply(200, target.read_bytes(), self._mime(relative))
            elif path[1:] in self.server.static:
                raw = self.server.bundle.joinpath(path[1:]).read_bytes()
                self._reply(200, raw, self._mime(path))
            else:
                self._error(404, "要求先がありません")
        except (OSError, ValueError, UnicodeError) as exc:
            self._error(500 if isinstance(exc, OSError) else 400, "要求を読み取れません")

    @staticmethod
    def _mime(path: str) -> str:
        if path.endswith(".js"):
            return "text/javascript"
        return mimetypes.guess_type(path)[0] or "application/octet-stream"

    def _events(self):
        listener = queue.Queue()
        with self.server.listeners_lock:
            self.server.listeners.add(listener)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while not self.server.stopping.is_set():
                try:
                    result = listener.get(timeout=0.25)
                    raw = b"event: scene-updated\ndata: " + json.dumps(result).encode() + b"\n\n"
                except queue.Empty:
                    if self.server.stopping.is_set():
                        break
                    raw = b": keep-alive\n\n"
                self.wfile.write(raw)
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass
        finally:
            with self.server.listeners_lock:
                self.server.listeners.discard(listener)
            self.close_connection = True

    def _body(self):
        if self.headers.get_all("Transfer-Encoding"):
            self._error(400, "Transfer-Encodingは使用できません")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not lengths[0].isascii()) or (
                lengths and not lengths[0].isdigit()):
            self._error(400, "Content-Lengthが不正です")
            return None
        length = int(lengths[0]) if lengths else 0
        if length > MAX_BODY:
            self._error(413, "本文は1 MiB以下にしてください")
            return None
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._error(415, "application/jsonが必要です")
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._error(400, "本文が途中で終了しています")
            return None
        body = json.loads(raw) if raw else {}
        if body is None:
            raise ValueError("JSON nullは使用できません")
        return body

    def do_POST(self):
        try:
            path = self._path()
            if path is None or not self._guard(path):
                return
            if path not in ("/api/edits", "/api/regenerate"):
                self._error(404, "要求先がありません")
                return
            body = self._body()
            if body is None:
                return
            if path == "/api/regenerate" and body != {}:
                self._error(400, "再生成は空本文または空JSON objectだけを受け付けます")
                return
            if not self.server.gate.acquire(blocking=False):
                self._error(409, "対象は処理中です")
                return
            try:
                if path == "/api/edits":
                    result = self.server.prepared.save_edits(body)
                else:
                    result = self.server.prepared.regenerate()
                    self.server.notify(result)
            finally:
                self.server.gate.release()
            self._reply(200, json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except (EditConflict, BlockingIOError) as exc:
            self._error(409, str(exc))
        except (ValueError, UnicodeError, TimeoutError):
            self._error(400, "JSON本文が不正です")
        except PipelineError as exc:
            self._error(500 if exc.code == 2 else 422, str(exc))
        except BlueprintError as exc:
            self._error(422, str(exc))
        except OSError:
            self._error(500, "入出力に失敗しました")

    def do_OPTIONS(self):
        path = self._path()
        if path is not None and self._guard(path):
            self._error(405, "OPTIONSは使用できません")


def serve(prepared: ViewerInput, assets: Path, *, open_browser: bool = True, out=None) -> None:
    """安全なURLをoutへ表示し、Ctrl+Cまで前景を保持する。

    token付きURLはブラウザへ渡し、自動起動しない場合だけ標準エラーへ表示する。
    終了時はSSEとサーバーを閉じる。
    """
    server = ViewerServer(prepared, assets)
    try:
        print(server.url.partition("#")[0], file=out, flush=True)
        print("token付きURLは自動起動したブラウザ、または自動起動無効時の標準エラーで"
              "確認してください。", file=out, flush=True)
        server.start()
        if open_browser:
            webbrowser.open(server.url)
        else:
            print(server.url, file=sys.stderr, flush=True)
        while server.thread.is_alive():
            server.thread.join(timeout=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
