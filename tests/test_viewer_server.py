import gzip
import http.client
import json
import socket
import threading

import pytest

from mcblueprint.viewer_input import prepare_input
from mcblueprint.viewer_server import MAX_BODY, ViewerServer


@pytest.fixture
def server(tmp_path):
    source = tmp_path / "house.json"
    source.write_text(json.dumps({
        "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "試験",
        "operations": [{"type": "fill", "from": [-1, 0, 0], "to": [1, 0, 0],
                        "block": "minecraft:stone"}],
    }), encoding="utf-8")
    prepared = prepare_input(source, output=tmp_path / "output")
    assets = tmp_path / "assets"
    asset = assets / "assets/minecraft/models/block/stone.json"
    asset.parent.mkdir(parents=True)
    asset.write_text('{"test": true}', encoding="utf-8")
    instance = ViewerServer(prepared, assets)
    instance.start()
    try:
        yield instance
    finally:
        instance.close()
    assert not instance.thread.is_alive()
    assert not instance.listeners


def request(server, method="GET", path="/api/scene", body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    values = {"Authorization": "Bearer " + server.token, "Content-Type": "application/json"}
    values.update(headers or {})
    connection.request(method, path, body=body, headers=values)
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


def edit(server, **changes):
    data = {"formatVersion": 1, "scene": server.prepared.scene_hash,
            "edits": [{"id": "replace-1", "kind": "replace", "region": [[0, 0, 0], [0, 0, 0]],
                       "block": "minecraft:dirt"}]}
    return data | changes


@pytest.mark.parametrize(("headers", "status"), [
    ({"Authorization": ""}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Host": "example.com"}, 403),
    ({"Host": "127.0.0.1:80"}, 403),
    ({"Origin": "null"}, 403),
    ({"Origin": "https://evil.example"}, 403),
    ({}, 200),
])
def test_authentication_over_socket(server, headers, status):
    for path in ("/api/scene", "/api/assets/1.21.11/assets/minecraft/models/block/stone.json"):
        result, response_headers, _ = request(server, path=path, headers=headers)
        assert result == status
        assert not any(key.lower().startswith("access-control-") for key in response_headers)
    if status != 200:
        assert request(server, path="/api/events", headers=headers)[0] == status
        for path in ("/api/edits", "/api/regenerate"):
            assert request(server, "POST", path, "{}", headers)[0] == status


def test_same_origin_localhost_and_gzip(server):
    port = server.server_port
    status, headers, raw = request(server, headers={"Origin": server.origin})
    assert status == 200
    assert headers["Content-Type"] == "application/gzip"
    assert "Content-Encoding" not in headers
    assert json.loads(gzip.decompress(raw))["size"] == [3, 1, 1]
    assert request(server, headers={"Host": f"localhost:{port}",
                                    "Origin": f"http://localhost:{port}"})[0] == 200
    assert request(server, method="OPTIONS")[0] == 405
    assert request(server, path="/manifest.json")[0] == 404
    assert request(server, path="/assets/worker-Yfb6Qb8X.js")[0] == 200
    assert request(server, path="/api/assets/1.21.11/manifest.json")[0] == 403


@pytest.mark.parametrize("path", [
    "../secret.json", "%2e%2e/secret.json", "%252e%252e/secret.json",
    "C:/secret.json", "C%3a/secret.json", "%5c%5cserver%5cshare%5csecret.json",
    "/server/share/secret.json", "assets/minecraft/models/%2e%2e/secret.json",
    "assets/minecraft/models/%5csecret.json",
])
def test_path_confinement_over_socket(server, path):
    assert request(server, path="/api/assets/1.21.11/" + path)[0] == 403


def test_symlink_escape_over_socket(server, tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text("秘密", encoding="utf-8")
    link = server.assets / "assets/minecraft/models/block/link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        # Windowsの開発者モードが無効ならdirectory junctionでも実体境界を確認する。
        import subprocess

        directory = server.assets / "assets/minecraft/models/escape"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(directory), str(tmp_path)],
                                capture_output=True)
        assert result.returncode == 0, result.stderr
        path = "assets/minecraft/models/escape/secret.json"
    else:
        path = "assets/minecraft/models/block/link.json"
    assert request(server, path="/api/assets/1.21.11/" + path)[0] == 403


@pytest.mark.parametrize(("body", "headers", "status"), [
    ("x", {}, 400), ("null", {}, 400), ("[]", {}, 400),
    ('{"command":"whoami"}', {}, 400), ('{"input":"C:/x"}', {}, 400),
    ("{}", {"Content-Type": "text/plain"}, 415),
    ("", {"Content-Length": "-1"}, 400),
    ("", {"Content-Length": "wrong"}, 400),
    ("", {"Content-Length": str(MAX_BODY + 1)}, 413),
    ("", {"Transfer-Encoding": "chunked"}, 400),
    ("{}", {}, 409), ("", {}, 409),
])
def test_post_contract_over_socket(server, body, headers, status):
    assert request(server, "POST", "/api/regenerate", body, headers)[0] == status


def test_duplicate_length_and_auth_headers(server):
    for duplicate in ("Content-Length: 2", "Host: evil", "Authorization: Bearer wrong"):
        with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as stream:
            stream.sendall(("POST /api/regenerate HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{server.server_port}\r\n"
                            f"Authorization: Bearer {server.token}\r\n"
                            "Content-Type: application/json\r\n"
                            f"Content-Length: 2\r\n{duplicate}\r\n\r\n{{}}").encode())
            assert b"200 OK" not in stream.recv(4096)


def test_save_validation_preserves_previous_edits(server):
    valid = edit(server)
    assert request(server, "POST", "/api/edits", json.dumps(valid))[0] == 200
    previous = server.prepared.edits_path.read_bytes()
    invalid = edit(server, scene="0" * 64)
    assert request(server, "POST", "/api/edits", json.dumps(invalid))[0] == 409
    invalid = edit(server, edits=[{"id": "note", "kind": "note", "element": "x", "text": "x"}])
    assert request(server, "POST", "/api/edits", json.dumps(invalid))[0] == 422
    invalid = edit(server)
    invalid["edits"].append(invalid["edits"][0] | {"block": "minecraft:glass"})
    assert request(server, "POST", "/api/edits", json.dumps(invalid))[0] == 422
    assert server.prepared.edits_path.read_bytes() == previous


def events(server):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    connection.request("GET", "/api/events", headers={"Authorization": "Bearer " + server.token})
    response = connection.getresponse()
    assert response.status == 200
    assert response.readline() == b": connected\n"
    assert response.readline() == b"\n"
    return connection, response


def test_regenerate_sse_concurrent_and_shutdown(server, monkeypatch):
    connections = [events(server) for _ in range(2)]
    old = server.prepared.scene_hash
    original = server.prepared.input.read_bytes()
    assert request(server, "POST", "/api/edits", json.dumps(edit(server)))[0] == 200
    entered, release = threading.Event(), threading.Event()
    regenerate = server.prepared.regenerate

    def delayed():
        entered.set()
        assert release.wait(10)
        return regenerate()

    monkeypatch.setattr(server.prepared, "regenerate", delayed)
    results = []
    thread = threading.Thread(target=lambda: results.append(
        request(server, "POST", "/api/regenerate", "{}")))
    thread.start()
    try:
        assert entered.wait(5)
        assert request(server, "POST", "/api/regenerate", "{}")[0] == 409
        assert request(server, "POST", "/api/edits", json.dumps(edit(server)))[0] == 409
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert results[0][0] == 200, results
    assert server.prepared.scene_hash != old
    assert server.prepared.input.read_bytes() == original
    assert server.prepared.derived_path.is_file()
    for _connection, response in connections:
        line = response.readline()
        while line.startswith(b":") or line == b"\n":
            line = response.readline()
        assert line == b"event: scene-updated\n"
        assert b'"scene"' in response.readline()
    # 保存済みeditsは旧sceneに属するので再適用できない。
    assert request(server, "POST", "/api/regenerate", "{}")[0] == 409
    server.close()
    for connection, response in connections:
        response.read()
        connection.close()
    # fixtureによるcloseの再実行も安全。


def test_publish_failure_rolls_back_without_sse(server, monkeypatch):
    from mcblueprint import viewer_input

    assert request(server, "POST", "/api/edits", json.dumps(edit(server)))[0] == 200
    paths = (server.prepared.input, server.prepared.scene_path, server.prepared.output,
             server.prepared.derived_path)
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    old_hash = server.prepared.scene_hash
    notified = []
    monkeypatch.setattr(server, "notify", notified.append)
    write = viewer_input.atomic_write

    def fail(path, raw):
        if path == server.prepared.scene_path:
            raise OSError("公開失敗")
        write(path, raw)

    monkeypatch.setattr(viewer_input, "atomic_write", fail)
    assert request(server, "POST", "/api/regenerate", "{}")[0] == 500
    assert before == {path: path.read_bytes() if path.exists() else None for path in paths}
    assert server.prepared.scene_hash == old_hash
    assert notified == []


def test_ctrl_c_closes_server_and_sse(server, monkeypatch):
    from mcblueprint import viewer_server

    # 起動ownerのKeyboardInterruptで、実ソケットとSSEを終了する。
    connections = [events(server)]
    monkeypatch.setattr(viewer_server, "ViewerServer", lambda *_args: server)
    monkeypatch.setattr(server, "start", lambda: None)

    def interrupt(_url):
        raise KeyboardInterrupt

    monkeypatch.setattr(viewer_server.webbrowser, "open", interrupt)
    viewer_server.serve(server.prepared, server.assets)
    assert not server.thread.is_alive()
    assert not server.listeners
    for connection, response in connections:
        response.read()
        connection.close()


def test_external_input_change_before_publish_is_not_overwritten(server, monkeypatch):
    from mcblueprint import viewer_input

    assert request(server, "POST", "/api/edits", json.dumps(edit(server)))[0] == 200
    scene = server.prepared.scene_path.read_bytes()
    write = viewer_input._write_blueprint_scene
    external = server.prepared.input.read_bytes() + b"\n"

    def change_input(*args, **kwargs):
        write(*args, **kwargs)
        server.prepared.input.write_bytes(external)

    monkeypatch.setattr(viewer_input, "_write_blueprint_scene", change_input)
    assert request(server, "POST", "/api/regenerate", "{}")[0] == 409
    assert server.prepared.input.read_bytes() == external
    assert server.prepared.scene_path.read_bytes() == scene
    assert not server.prepared.output.exists()
    assert not server.prepared.derived_path.exists()


def test_spec_note_in_api_responses_and_edits_file_only(tmp_path):
    import yaml

    source = tmp_path / "tower.yaml"
    source.write_text('minecraftVersion: "1.21.11"\narchetype: tower\nscale: small\n',
                      encoding="utf-8")
    prepared = prepare_input(source, output=tmp_path / "output")
    server = ViewerServer(prepared, tmp_path / "assets")
    document = {"formatVersion": 1, "scene": prepared.scene_hash, "edits": [
        {"id": "note", "kind": "note", "element": "tower#1", "text": "入口を確認する"},
    ]}
    notes = [{"id": "note", "element": "tower#1", "text": "入口を確認する"}]
    try:
        server.start()
        status, _, raw = request(server, "POST", "/api/edits", json.dumps(document))
        assert status == 200
        assert json.loads(raw)["notes"] == notes
        previous = prepared.edits_path.read_bytes()
        status, _, raw = request(server, "POST", "/api/regenerate", "{}")
        assert status == 200
        assert json.loads(raw)["notes"] == notes
        assert "metadata" not in yaml.safe_load(source.read_bytes())
        assert prepared.edits_path.read_bytes() == previous
    finally:
        server.close()
    assert not server.thread.is_alive()
