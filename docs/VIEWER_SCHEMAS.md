# viewer のデータスキーマ

viewer v1 の JSON Schema は `schema/scene.schema.json` と `schema/edits.schema.json` が正本で、同じ内容をパッケージの `src/mcblueprint/schema/` に同梱する。コピーの一致と Draft 2020-12 準拠は `tests/test_viewer_schema.py` で確認する。

## scene v1

scene は gzip 展開後の UTF-8 JSON。トップレベルの `source` は入力種別・パス・SHA-256 を持ち、`palette` は各 block state の描画属性を持つ。`palette[0]` は `minecraft:air`（`render: invisible`）で、範囲外や省略したチャンクのブロックは添字 0 として扱う。`tint: fixed` の要素は blockdata の `tints.fixed` から解決した `tintColor: [L, a, b]`（OKLab）を持ち、それ以外の tint では `tintColor` を持たない。`environment.tints` は grass・foliage・water の OKLab 乗算色を保持する。

`mcblueprint make` は `.schem` / `.litematic` と同じ完成coreから `<stem>.scene.json.gz` も出力し、隣に内部世代情報 `<stem>.scene.json.gz.meta.json` を置く。sidecar は入力・解決済み設定・環境・展開後sceneのSHA-256を記録する。生成元不明のセルは source ID 0 とし、Operation の属性があるセルは内部source表の安定IDを持つ。scene は gzip の mtime を0にして決定的に書き出す。

`chunks` は各軸 16 ブロック単位で分割し、scene の端では範囲内の実寸に切り詰める。`at` は各チャンクの最小コーナーの scene ローカル座標。全空気のチャンクは含めない。`blocks` は palette 添字を `(y × チャンク奥行 + z) × チャンク幅 + x`（x が最速）で並べた列を base64 化したもの。端のチャンクでは切り詰め後の幅・奥行を使う。`indexWidth` は添字あたり 16 bit、palette が 65,536 要素を超える場合は 32 bit で、整数バイト順はリトルエンディアン。

各チャンクの `sources` は `blocks` と同じ順で `sources[].id` を並べ、各要素は符号なし uint32 のリトルエンディアン。0 は生成元不明を表す。両方の base64 データはチャンクの実寸に対応する要素数を持つ。

BuildSpec の住宅・塔では、root とその子要素のセルの source は、`element` を実在する編集可能な root の ID とし、`params` を planning が定めるその root の許可パラメーター名とする。子の生成元は `spec`・`label`・`path` に残す。たとえば塔の body の選択でも `element: tower#1` の `height` を編集し、body 自身のパラメーターとは扱わない。root の配下でない要素や、BuildSpec の計画情報を持たない入力には params を付けない。schema v1 とセルごとの source ID の形式は変わらない。

`.scene.json.gz` を単独入力として開く場合も非 spec 入力として扱う。元の scene は変更せず、表示用コピーの sources から params を取り除く。由来の `source.kind: spec` や `source.path` は編集権限の根拠にしない。

## edits v1

`apply_edits_file(..., overrides=["seed=42", "weathering.age=0"])` はmakeと同じ設定合成を使い、
BuildSpecの生成時CLI指定と合成設定のhashを照合する。内部sidecarの `cliOverrides` は、
CLI由来の設定パスから合成後の値への辞書（指定なしは `{}`）。makeとviewer再生成で記録する。
適用時の指定が省略・変更・追加されていれば相違キーを示して拒否し、記録値は自動採用しない。
記録がないsidecarはsceneを再生成する。scene v1・edits v1の形式は変更しない。

`scene` は gzip を展開した後の scene JSON のバイト列の SHA-256。現在の生成結果と一致しない場合は競合として適用しない。`edits` は `replace`（領域と置換ブロック）、`delete`（`at` の単一点または `region` の範囲のどちらか一方）、`param`（要素・パラメーター・値）、`note`（要素・文章）のいずれかで、座標は scene ローカル座標。`region` は `[[x1,y1,z1],[x2,y2,z2]]` 形式で両端を含む。

Python API `mcblueprint.edits.apply_edits(scene, edits, source, *, scene_bytes=None, notes=None)` は入力を変更せず、適用候補を返す。`scene_bytes` を渡すと展開済みJSONのSHA-256も照合する。BuildSpecの編集は既存 `planning.plan_buildspec` で検証し、`replace` / `delete` が `structure` または `circulation` の予約Operationのboundsと重なる場合は要素ID・予約種別を示して拒否する。BuildSpec以外は `replace` / `delete` を派生Blueprintの `overrides` にし、元入力を上書きしない。`note` は入力へ書き込まず、成功時に呼出し元の `notes` リストへ `{id, element, text}` を追加する。`apply_edits_file(..., notes=...)` も保存成功後にメモを返す。CLIは対象付きメモを出力し、HTTPの保存・再生成応答は `notes` に含める。元のeditsファイルは残し、noteから生成処理を起動しない。

`mcblueprint.output_lock.lock_paths(*paths)` は入力・出力の正規化パスを一定順で非待機ロックするコンテキストマネージャー。ロック競合は `BlockingIOError`。Windowsは `msvcrt.locking`、Unixは `fcntl.flock` を使い、ロックファイルは削除しない。
