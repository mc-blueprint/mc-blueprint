# 3D静止画

`render` は同梱viewerを専用のヘッドレスChromiumで開き、PNGを出力する。
利用者によるNode.js・JDK・Edgeの導入は不要。既存のPillowによる2D `preview` は別機能。

```bash
python -m pip install "mc-blueprint[render]"
mcblueprint render --setup
mcblueprint render blueprints/house.json --views iso,front,top --out preview/
mcblueprint render tower.yaml --set render.gpu=swiftshader
```

初回setupは現在のPythonからPlaywrightのCLIを実行し、対応するChromiumを公式配布元から
Playwright標準cacheへ取得する。繰り返し実行できる。通常renderはブラウザを取得しない。
Playwright更新後はsetupを再実行する。LinuxのOS依存ライブラリは自動導入せず、
不足時は[公式のシステム要件・ブラウザ手順](https://playwright.dev/python/docs/browsers)を確認する。

Playwrightはrender extraだけに1.64.1を固定した。
[PyPIの配布情報](https://pypi.org/project/playwright/1.64.1/#files)でPython >=3.10の
platform wheelを確認済みで、Python 3.11/3.12に対応する。
標準機能やPillowではWebGL viewerのready待機とcanvas撮影ができないため採用する。
ソースからのPlaywright buildは利用者のNode不要という確認の対象外。

保存先を変える場合はPlaywright公式の `PLAYWRIGHT_BROWSERS_PATH` 環境変数を
setupとrenderの両方で同じ絶対パスに設定する。

入力は `view` と同じBuildSpec YAML・Blueprint JSON・`.schem`・`.scene.json.gz`。
`--minecraft-version` と `--set KEY=VALUE` を使える。入力準備によるsceneなどの成果は
通常の `output.directory` に保存される。画像の保存先は `--out`（既定 `preview/`）。
アセットの探索・取得可否とBearer認証付きローカルAPIは [VIEWER.md](VIEWER.md) と共通。
jarやアセットは利用者の端末に置き、配布物には含めない。

画像名は `<入力名>-iso.png`・`-front.png`・`-top.png`。
`--views` はこの3視点から重複なしで指定する。寸法は1280×720、倍率1。
isoは東・上・北、frontは北、topは上から見る。sceneの外接球が画角へ収まる距離を
viewerが決める。プリセットは `preview.shader`（既定 `vanilla`）に従う。

`render.gpu=auto` はWebGL2 contextを試し、生成できない場合だけSwiftShaderで一度再起動する。
`swiftshader` は最初からSwiftShaderを明示する。実際のChromium版とWebGLレンダラー名を
標準出力へ記録し、明示経路でSwiftShader以外が選ばれた場合は失敗する。
ブラウザは空の専用contextを使い、外部originへの通信を拒否する。

全視点と再試行で `render.timeout_seconds`（既定120秒）の共通上限を使い、viewerの
readyとalertを同時に監視する。アセット・モデル・alertなどの失敗は再試行しない。
ブラウザなし・OS依存不足・WebGL2不可・timeoutなどは原因と対処を表示して終了2。
終了時はブラウザ・context・サーバーを閉じる。

全視点の撮影が成功してから、同じディレクトリの一時ファイルを使って各PNGを原子的に
置換する。撮影失敗では既存PNGを保つ。公開中のI/O失敗について複数PNG全体の
トランザクションは保証しない。
