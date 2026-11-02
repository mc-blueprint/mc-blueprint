# パターンライブラリ v1

パターンデータの形式の正本は `schema/pattern-style.schema.json`、`pattern-module.schema.json`、`pattern-sources.schema.json` です。各スキーマは実行時にも使えるよう `src/mcblueprint/schema/` に同一内容で同梱します。

`styles/<style>.yaml` は様式名、グリッド、部位ごとのモジュール参照と役割を記述します。`modules/<style>/<name>.json` は Blueprint の部品形式を拡張します。`formatVersion`・`name`・`operations` に加え、`palettes`・`description`・`author`・`metadata` を部品と同じ形式で使えます。モジュール名は `<style>.<name>` でファイル位置と一致させます。`parameters`（型・既定値・数値範囲など）と `anchors` は任意です。`sources.yaml` は出典とライセンスを記録します。`layouts/` はこの版の対象外です。

役割参照はブロック値として記述します。

```json
{"type": "set", "position": [0, 0, 0], "block": {"role": "frame", "shape": "stairs", "state": {"facing": "north"}}}
```

`block` または Palette の `block` に書けます。役割参照は Materialize 前には実ブロックが決まらないため `mcblueprint check` でエラーになります。

## 検証

```sh
mcblueprint check patterns/
```

成功は終了コード `0`、スキーマ・名前・参照エラーは `1`、パスなどの引数・入出力エラーは `2` です。
