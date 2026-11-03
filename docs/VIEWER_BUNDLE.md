# viewer の静的バンドル

viewer の build 済みファイルを `mcblueprint` の wheel に同梱する。利用時に Node.js は必要ない。

## 更新

viewer の固定 commit `7b2e0f44bc53780499fb500d1fd6c1d5b52039c1` を専用 worktree で Node.js 24 / npm 11 により build し、次を実行する。

```powershell
python scripts/update_viewer_bundle.py H:\mc-blueprint\worktrees\mcb-154\mc-blueprint-viewer
```

スクリプトは source worktree の HEAD が固定 commit と一致することを確認し、`dist/` の全ファイルを取り込む。生成される `manifest.json` は source commit と各ファイルの SHA-256 を記録する。依存 lock・viewer source・fixture・asset はコピーしない。

## 配布境界

`src/mcblueprint/data/viewer/` に静的 dist、`THIRD_PARTY_LICENSES.txt`、版と hash の manifest を置く。Node.js、npm、Vite、開発 API、scene fixture、Mojang の jar・texture・model は含めない。wheel 内の資産は `importlib.resources.files("mcblueprint").joinpath("data", "viewer")` から読み取れる。

`tests/test_viewer_bundle.py` は package resource としての可読性、manifest の hash、静的配布物の範囲を確認する。
