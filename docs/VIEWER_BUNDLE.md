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

## Minecraft アセット

Minecraft の jar と viewer 用テクスチャ・モデルは wheel に含めない。`assets.minecraft_dir` で
指定した `.minecraft` を先に探し、見つからない場合は SHA-1 を検証済みの公式 jar cache
を使う。必要な jar がどちらにもない場合、`assets.download: true` だけが無確認で公式配布元
から取得する。`ask` は対話端末で確認し、非対話端末では `true` の設定方法を案内して終了する。
`false` は通信しない。

Windows の cache は `%LOCALAPPDATA%\mc-blueprint\cache\` に置く。jar は `jars/`、版ごとに
選別して展開したファイルと `manifest.json` は `assets/<minecraftVersion>/` に保存する。
manifest には取得元、jar SHA-1、展開日時、展開ファイル一覧と各ファイルの SHA-256 を記録し、
欠損・破損した展開 cache は再作成する。cache lock と staging directory により、同じ版の
同時展開や途中失敗で未完成 cache を公開しない。

取得・展開処理は `mcblueprint.assets.ensure_assets(version, settings)` が担う。`settings` は
`assets` 設定の辞書で、戻り値は版別 cache directory（`assets/minecraft/` を含む）である。
