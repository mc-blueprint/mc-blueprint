# 部品（component）

繰り返し使う建築部品を 1 ファイル 1 部品で置く。Blueprint からは `component` Operation で名前を指定して配置する。

```json
{ "type": "component", "name": "lantern_post", "position": [10, 1, -2], "rotation": 90 }
```

## 部品ファイルの書き方

`components/<name>.json`（名前は英小文字・数字・アンダースコア）。Blueprint と同じ書式で、`minecraftVersion` / `origin` / `size` / `seed` は書かない。座標は部品の原点 `[0, 0, 0]` からの相対で、`position` で指定した位置が原点になる。

```json
{
  "formatVersion": 1,
  "name": "lantern_post",
  "description": "石の台座に樫のフェンス 2 段、上にランタン",
  "palettes": {},
  "operations": [
    { "type": "set", "position": [0, 0, 0], "block": "minecraft:stone_bricks" },
    { "type": "fill", "from": [0, 1, 0], "to": [0, 2, 0], "block": "minecraft:oak_fence" },
    { "type": "set", "position": [0, 3, 0], "block": "minecraft:lantern" }
  ]
}
```

- 部品の `palettes` は部品内で優先され、書かれていない名前は呼び出し元の Blueprint の Palette を参照する（呼び出し元の Palette に合わせたい部分は部品側に Palette を書かない）。
- 部品の中で別の部品を使ってもよい（循環参照とネスト深さ 8 超はエラー）。
- `rotation` は上から見て時計回り。原点を軸に回転してから `position` へ移動する。`facing` / `axis` / 接続プロパティは自動で回る。
- 部品は Blueprint ファイルと同じ階層の `components/`、次にカレントディレクトリの `components/` から探す。
- ゲーム内で作ったパーツは `mcblueprint import <file.schem> --component --name <name>` で部品ファイルに変換できる（座標は自動で原点基準になる）。
- `mcblueprint components` で探索パス上の部品（名前・大きさ・説明）を一覧できる。`mcblueprint check components/` は各部品を原点に置いて検証し、ランタンなど支持が必要なブロックの警告も出す（`--strict` で警告をエラー扱い）。

## 部品の正本

このディレクトリは利用者が追加する部品（`mcblueprint design` の出力を含む）用です。共通の様式と部品の正本は [mc-blueprint-patterns](https://github.com/mc-blueprint/mc-blueprint-patterns) です。同梱モジュールは `component` Operation から `medieval.window` のような名前で参照できます。
