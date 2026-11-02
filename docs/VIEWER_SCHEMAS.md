# viewer のデータスキーマ

viewer v1 の JSON Schema は `schema/scene.schema.json` と `schema/edits.schema.json` が正本で、同じ内容をパッケージの `src/mcblueprint/schema/` に同梱する。コピーの一致と Draft 2020-12 準拠は `tests/test_viewer_schema.py` で確認する。

## scene v1

scene は gzip 展開後の UTF-8 JSON。トップレベルの `source` は入力種別・パス・SHA-256 を持ち、`palette` は各 block state の描画属性を持つ。`palette[0]` は `minecraft:air`（`render: invisible`）で、範囲外や省略したチャンクのブロックは添字 0 として扱う。`tint: fixed` の要素は blockdata の `tints.fixed` から解決した `tintColor: [L, a, b]`（OKLab）を持ち、それ以外の tint では `tintColor` を持たない。`environment.tints` は grass・foliage・water の OKLab 乗算色を保持する。

`chunks` は各軸 16 ブロック単位で分割し、scene の端では範囲内の実寸に切り詰める。`at` は各チャンクの最小コーナーの scene ローカル座標。全空気のチャンクは含めない。`blocks` は palette 添字を `(y × チャンク奥行 + z) × チャンク幅 + x`（x が最速）で並べた列を base64 化したもの。端のチャンクでは切り詰め後の幅・奥行を使う。`indexWidth` は添字あたり 16 bit、palette が 65,536 要素を超える場合は 32 bit で、整数バイト順はリトルエンディアン。

各チャンクの `sources` は `blocks` と同じ順で `sources[].id` を並べ、各要素は符号なし uint32 のリトルエンディアン。0 は生成元不明を表す。両方の base64 データはチャンクの実寸に対応する要素数を持つ。

## edits v1

`scene` は gzip を展開した後の scene JSON のバイト列の SHA-256。現在の生成結果と一致しない場合は競合として適用しない。`edits` は `replace`（領域と置換ブロック）、`delete`（`at` の単一点または `region` の範囲のどちらか一方）、`param`（要素・パラメーター・値）、`note`（要素・文章）のいずれかで、座標は scene ローカル座標。`region` は `[[x1,y1,z1],[x2,y2,z2]]` 形式で両端を含む。
