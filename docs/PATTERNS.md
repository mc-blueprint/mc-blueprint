# パターンライブラリ v1

`src/mcblueprint/data/patterns/` は mc-blueprint-patterns の確定 commit を同梱する。同梱元・commit・SHA-256 の正本は [lock.json の `patterns`](../src/mcblueprint/data/lock.json) とする。更新時は正本の develop に取り込まれた commit を決め、その commit から `styles/`・`modules/`・`sources.yaml` を置き換え、lock の commit と SHA-256 を更新する。SHA-256 は対象ファイルを相対パスの UTF-8 バイト列順に並べ、各ファイルについて `相対パス + NUL + ファイル内容 + NUL` を連結したバイト列の SHA-256。

`component` のドット区切り名（例 `medieval.window`）は同梱モジュールを参照する。利用者の `components/<name>.json` は従来どおり Blueprint と作業ディレクトリから探索する。

配置・`mcblueprint components` の一覧・参照の検査では同じ名前解決を使います。単純名は利用者部品、ドット名は同梱モジュールの名前空間です。利用者の `components/medieval/window.json` と `components/medieval.window.json` は `medieval.window` を上書きしません。参照名の各要素は `^[a-z0-9_]+$` です。利用者部品のデータ内の `name` は表示名なので、日本語を含む任意の文字列を使えます。

パターンデータの形式の正本は `schema/pattern-style.schema.json`、`pattern-module.schema.json`、`pattern-sources.schema.json` です。各スキーマは実行時にも使えるよう `src/mcblueprint/schema/` に同一内容で同梱します。

`styles/<style>.yaml` は様式名、グリッド、部位ごとのモジュール参照と役割を記述します。`modules/<style>/<name>.json` は Blueprint の部品形式を拡張します。`formatVersion`・`name`・`operations` に加え、`palettes`・`description`・`author`・`metadata` を部品と同じ形式で使えます。モジュール名は `<style>.<name>` でファイル位置と一致させます。`parameters`（型・既定値・数値範囲など）と `anchors` は任意です。`sources.yaml` は出典とライセンスを記録します。`layouts/` はこの版の対象外です。

役割参照はブロック値として記述します。

```json
{"type": "set", "position": [0, 0, 0], "block": {"role": "frame", "shape": "stairs", "state": {"facing": "north"}}}
```

`block` または Palette の `block`、および [Materialize対象の材質スロット](OPERATIONS.md#materialize-前の役割入力) に書けます。
`mcblueprint check` は、モジュール名に対応する様式の既定材でMaterializeしてから、通常の意味検証・生成・支持検査を行います。
対応する様式名がないモジュールは検査対象の各様式で確認し、役割を定義する様式がなければ未知役割として拒否します。
版を省略すると全4版、`--minecraft-version` を指定するとその版だけ検証します。
未知の役割、必要形状の不足、無効な状態はエラーです。色の関係が解けない場合の診断は構造エラーとは区別します。
モジュール内Paletteの役割参照は、そのPaletteが持つ重みを維持して主ブロックへ解決します。
役割参照入りPaletteは全entryを同じ役割に揃えます。異なる役割、または役割参照と具体ブロックを混在させたPaletteはMaterializeで未対応として拒否します。
役割参照を持つ入れ子の `component` はMaterialize時に展開し、Paletteの親からの継承・子での上書きと、位置・回転・セル属性を保持します。
部品の展開は、役割参照の解決や改名対象Paletteの継承に必要なものに限定します。未参照の役割入りPaletteや子での同名上書きだけを理由に、無関係な具体部品を展開しません。必要な展開も平行移動と回転を1段にまとめ、部品1個分の入れ子の深さを保ちます。
現行Dでは同梱モジュールをDetailの固定ベイ・入口・屋根縁として使用します。更新時はpatterns正本の確定commitから `styles/`・`modules/`・`sources.yaml` を無加工で置き換え、lockのcommitとSHA-256を更新します。

patterns#11の役割参照を含む部品は、BuildSpecでは対象版・様式・指定材でMaterializeします。版情報を持たない従来の部品読み込みでは、同梱最古版と部品名の様式の既定材を使います。gatehouse・village_pathの部品材質はこの既定材へ変わりますが、原寸・配置・向きは維持します。

Detailは部品の外付けframe・window_frame・trim・屋根縁を役割ごとに配置し、必須壁・床・機能開口は基本施工を維持します。原寸で収まらない区間は基本施工のままとし、部品の非一様スケールは行いません。round塔は輪郭の同じ基準面を持つ区間だけに固定幅部品を置きます。

内部計画の `decorations` はMassing/Detail JSONと `metadata.planning` に保持します。各要素の `operation` は最終セルの `CellAttributes.operation` と同じID、`element` は `facades[].id`、`face` は回転後の外壁方向です。trim・window_frame・accentの配置だけを明示し、構造frame・床・屋根・機能ガラスを装飾密度へ含めません。

## 検証

```sh
mcblueprint check patterns/
```

成功は終了コード `0`、スキーマ・名前・参照エラーは `1`、パスなどの引数・入出力エラーは `2` です。

`styles/` のあるディレクトリは patterns として検査し、`modules/`・`sources.yaml` の欠落もエラーにします。`designs/` はプリセットとして検査するため、配下の `styles/` と混同しません。複数の検査パスもそれぞれ判定します。

外部 patterns の検査では、ドット名の探索先をその `modules/` だけに置き換え、同梱品では補完しません。JSON ファイルを直接指定した `mcblueprint check <file.json>` は名前から解決し直さず、指定ファイル自体を検査します。
