"""実アセットと同梱viewerをヘッドレスPlaywrightで検証する開発用スクリプト。"""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

from mcblueprint.assets import ensure_assets
from mcblueprint.viewer_input import prepare_input
from mcblueprint.viewer_server import ViewerServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--playwright-module", type=Path, required=True)
    parser.add_argument("--browser", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="viewer-browser-") as temporary:
        root = Path(temporary)
        source = root / "browser.json"
        source.write_text(json.dumps({
            "formatVersion": 1, "minecraftVersion": "1.21.11", "name": "接続試験",
            "operations": [{"type": "fill", "from": [0, 0, 0], "to": [2, 0, 2],
                            "block": "minecraft:stone"}],
        }), encoding="utf-8", newline="\n")
        prepared = prepare_input(source, output=root / "output")
        assets = ensure_assets("1.21.11", {"download": "false"})
        server = ViewerServer(prepared, assets)
        try:
            server.start()
            subprocess.run(["node", str(Path(__file__).with_suffix(".mjs")), server.url,
                            str(args.playwright_module.resolve()), str(args.browser.resolve())],
                           check=True, timeout=120)
        finally:
            server.close()
        assert not server.thread.is_alive()
        assert not server.listeners


if __name__ == "__main__":
    main()
