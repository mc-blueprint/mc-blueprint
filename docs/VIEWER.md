# ローカル3D viewer

`mcblueprint view` は同梱のviewerを `127.0.0.1` の空きポートで配信する。
Node.jsは利用時には不要。Minecraftのアセットは利用者の端末で展開し、配布物には含めない。

ヘッドレスChromiumによる3視点PNGと初回setupは [RENDER.md](RENDER.md) を参照。

```bash
mcblueprint view tower.yaml
mcblueprint view blueprints/house.json -o output/view
mcblueprint view existing.schem --minecraft-version 1.21.11
mcblueprint view output/tower.scene.json.gz --set preview.open_browser=false
```

BuildSpec（YAML）は生成・評価してsceneを作る。Blueprint JSONは従来の生成を使い、
make由来の生成metadataがあるときは同じ混色・Weather経路を使う。
`.schem` は既存importerで読む。既知のDataVersionと版の上書きが矛盾すると拒否し、
未知のDataVersionを近い版へ置き換えない。DataVersionがない場合だけ版の指定が必要。
`.scene.json.gz` は検証して読み、内部に記録された由来ファイルを自動で開かない。

`--set KEY=VALUE` は通常の設定合成に従う。
`assets.minecraft_dir`、`assets.download` による資産解決は
[VIEWER_BUNDLE.md](VIEWER_BUNDLE.md) を参照。
`preview.open_browser=false` ならブラウザを開かず接続情報を表示する。表示中は前景を保持し、Ctrl+Cで
サーバーとSSE接続を閉じる。表示しているだけでは出力を占有しない。
CLIの `make` も既定で生成済みsceneを表示する。生成を二度実行せず、評価不合格
（終了コード3）の成果も表示する。`preview.enabled=false` なら表示を省略し、
`preview.mode=2d` なら出力先の `preview/` に4視点PNGを保存する。
Pythonの `make` とviewerからの再生成は別サーバーを起動しない。

## 保存と再生成

viewerの編集保存先は出力stemの `<名前>.edits.json`。
再生成は現在のeditsを保存してから要求する。保存失敗時は再生成せず、処理中は
二重送信を防ぐ。保存ボタンだけでeditsを保存することもできる。
BuildSpec入力ではspecを更新し、元の要素の出所と編集を維持して再生成する。
BuildSpecへ保存するoverride IDは `edit-<元scene hashの先頭8桁>-<edit id>` とする。
新しいsceneでviewerが同じedit IDを採番しても以前の編集と区別し、同じ名前空間付きIDの
同内容は再追加せず、別内容は競合として拒否する。scene・editsはv1のまま扱う。
それ以外の入力は置換・削除だけを許し、`<名前>.edited.json` に派生Blueprintを保存する。
元のBlueprint・schematic・入力sceneは変更しない。

CLIで編集だけを適用する場合は次のようにする。再生成は開始しない。

```bash
mcblueprint apply-edits output/tower.edits.json --spec tower.yaml --scene output/tower.scene.json.gz
mcblueprint apply-edits output/house.edits.json --input blueprints/house.json --scene output/house.scene.json.gz
```

生成時にCLI指定を使った場合は、同じ指定を再現する。

```bash
mcblueprint make tower.yaml --seed 42 --set weathering.age=0
mcblueprint apply-edits output/tower.edits.json --spec tower.yaml --scene output/tower.scene.json.gz --seed 42 --set weathering.age=0
```

`--set KEY=VALUE` は複数指定でき、`--seed` はmakeと同様に `--set seed=...` より優先する。
`make --format litematic` の指定は `--set output.format=litematic` で再現する。
指定の順序やYAML表記ではなく、設定合成後のキーと値をsidecarの `cliOverrides` と照合する。
省略・変更・追加は相違キーを示して終了コード2で拒否し、sidecarの値を自動採用しない。
CLI指定の記録がない古いsidecarはsceneの再生成が必要。`--set`・`--seed` は `--spec` 専用。

`--spec` は `-o updated.yaml` を省略すると原本を原子的に更新する。
`--input` は `-o` を省略すると入力の隣の `<名前>.edited.json` に保存する。
派生Blueprintでは編集を末尾の明示Operationへ変換する。paramとnoteは受け付けない。
noteは対象付きメモとしてCLIの出力とAPI応答の `notes` に返す。
BuildSpecには書き込まず、元のeditsファイルに残す。editsファイルは削除しない。

sceneのSHA-256が一致しなければ適用しない。specではsidecarの入力・合成設定・
engine・dataの世代も照合する。CLIでspecを書き換えた後は古いsceneへの再適用が
拒否されるため、再生成して新しいsceneを使う。

再生成は入力・出力のOSロックを非待機で取得する。競合はHTTP 409、CLIは終了2。
候補の生成後、公開直前にも入力を検査する。失敗時は既存の入力と成果を保全または
ロールバックし、scene一式の公開成功後だけ `scene-updated` を通知する。
複数ファイルを電源断まで含めた単一atomic transactionとしては扱わない。
評価不合格でも生成成果がある場合はHTTP 200の `code: 3` とdiagnosticsで示す。

## 接続契約

| メソッド | パス | 内容 |
| --- | --- | --- |
| GET | `/api/scene` | gzip JSON bytes。`application/gzip`、Content-Encodingなし |
| GET | `/api/assets/<版>/<path>` | 起動時に準備した版の公開アセットだけ |
| GET | `/api/events` | SSE。公開成功後に `scene-updated` |
| POST | `/api/edits` | edits v1を検証して原子的に保存 |
| POST | `/api/regenerate` | 保存済みeditsで固定入力・出力を再生成 |

APIはすべて起動ごとのBearer tokenが必要。tokenは起動URLのフラグメントから渡す。
標準出力にはtokenを含まないURLと、token付きURLの取得方法を表示する。
自動起動時はtoken付きURLをブラウザへ直接渡し、標準出力・標準エラーには出さない。
`preview.open_browser=false` の場合だけ、手動で開くtoken付きURLを標準エラーへ表示する。
規定の `run_logged.py` は標準エラーも記録するため、この設定では監査ログにもtokenが残る。
監査ログにtokenを残さない場合は自動起動を使う。手動起動時のログは認証情報を含むものとして
アクセス・共有範囲を制限し、未加工のまま共有しない。標準エラーへの出力だけではマスクされない。
tokenはサーバー終了まで有効で、再起動時に変わる。
Hostは同じポートの `127.0.0.1` / `localhost` に限定し、OriginがあればHostに対応する
自身のoriginと完全一致するものだけを許す。Originなしでも認証は必要。
CORSは有効にしない。静的ファイルは同梱manifestの一覧に限定する。
要求パスを一度だけデコードし、認証・配信先の判定・アセットの範囲検査で共有する。
二重エンコード、ドット区間、バックスラッシュ、drive・UNCなど不正なパスは404で拒否する。

POSTは `Content-Type: application/json`、本文最大1 MiB。
regenerateは空本文または `{}` だけを受け、コマンド・パス・設定を指定できない。
形式不正400、認証なし401、Host/Origin/アセット範囲拒否403、不正パス・未定義パス404、
未保存・古い世代・競合409、
サイズ超過413、Content-Type不正415、意味検証失敗422、I/O失敗500を返す。

## 開発時のブラウザ検証

`scripts/verify_viewer.py --playwright-module <playwright/index.mjs> --browser <browser.exe>`
は開発用Nodeと既存Playwrightを使い、別プロファイルのヘッドレスブラウザで同梱bundleを
検証する。実アセットを端末内で解決し、Bearer、Worker、CSS、画像、保存・再生成、
SSEによる再読み込みを確認して全プロセスを閉じる。製品CLIからNodeは呼ばない。

配布wheelの受入確認は、別のvenvへwheelと `render` extraを導入してから
`scripts/verify_distribution.py --output <リポジトリ外の証跡保存先> --setup --download-assets`
を実行する。公式manifestのSHA-1、全4版のSwiftShader静止画、同梱bundleのhashと
ライセンス、住宅・塔・非spec入力の実APIと画面操作、保存・再生成・SSE更新を検証する。
公式jar取得とChromium取得は通信を伴う。Nodeとdev-apiは使用しない。
標準cacheからChromiumを起動できない環境では `--browser-cache <検証用cache>` を
明示し、標準cacheでの失敗と区別して記録する。PNG撮影成功後のUI検証再開には
`--skip-render` を使える。
