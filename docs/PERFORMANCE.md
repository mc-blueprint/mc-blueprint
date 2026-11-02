# 性能測定

`benchmarks/fixtures/` に疎な配置、密な地形、大きな空気領域、反復Operationの4種類を3規模ずつ固定し、`scripts/benchmark.py` で段階ごとの基準を記録する。fixtureのSHA-256、入力サイズ、実セル数、Operation数、bounds寸法と体積、生成した `.schem` のサイズを各記録に残す。

## 実行

```powershell
$env:PYTHONPATH = "$PWD/src"
python scripts/benchmark.py --generate-fixtures
python scripts/benchmark.py --repetitions 3 --output benchmarks/baseline.json
```

`--generate-fixtures` は同じ負荷を再生成する。各負荷・段階は独立したPython workerで実行する。worker内では各反復ごとに入力と前処理を作り直し、ブロックデータ、描画色、Blueprint schemaのキャッシュを消してから計時する。メモリ計測は時間反復後に別実行し、`tracemalloc` が時間へ与える影響を避ける。時間は反復値と通常の統計的中央値を記録する。反復間でPython allocator状態は共有される。fixtureのハッシュが変わった測定記録は同じ基準として比較しない。CIでは時間の絶対値を合否条件にしない。

## 段階と計測範囲

| 段階 | 計時する処理 | 計時外の準備 |
| --- | --- | --- |
| `read_parse` | JSONファイルの読込とJSON解析 | なし |
| `validation` | `validate()` 全体 | JSON読込 |
| `model_load` | `load_blueprint_dict()` によるモデル・Operation構築 | JSON読込 |
| `bounds_expansion` | 新しいモデルの構築とOperationの解析bounds計算 | JSON読込 |
| `generation` | 新しいモデルからの `generate()` | JSON読込と別モデルの生成 |
| `support` | `check_support()` | 読込、モデル構築、生成 |
| `render` | top view の画像レンダリング | 読込、モデル構築、生成 |
| `output` | `SchemExporter.export()` による圧縮 `.schem` 出力 | 読込、モデル構築、生成 |
| `cli_validate` | CLI `validate` 全体 | interpreter起動 |
| `cli_preview` | top view 1面を出すCLI `preview` 全体 | interpreter起動 |
| `cli_build` | `.schem` を出すCLI `build` 全体 | interpreter起動 |
| `single_pass` | 検証、モデル構築、生成、支持検査、top view 1面の描画・PNG保存、`.schem` 出力 | interpreter起動 |

`validate()` はschema・意味検証に加えてモデル構築と解析boundsを含むため、これらの段階の時間を足し合わせてCLI全体時間とは扱わない。`bounds_expansion` は複合Operationの遅延展開が起きる `bounds()` の計算を独立して測る。`generation` は新しいモデルを使い、生成中に必要となる複合Operationの展開を含む。各段階はそれぞれ独立した測定であり、キャッシュ状態も共有しない。

`cli_validate`、`cli_preview`、`cli_build` はそれぞれ別コマンドで、各CLIの検証・生成の再実行を含む。`single_pass` は同じ処理群を一度ずつ同一プロセスで実行し、各コマンドを個別に呼ぶ場合とのコストを比べるための参考値である。previewはいずれもtop view 1面・scale 1・Blueprint origin表示に固定している。CLI比較の時間計測はinterpreter起動を除き、`single_pass` と個別のCLI関数群の処理時間を比較する。

メモリ値は各段階の実行中に `tracemalloc` が観測したPythonメモリ割当のピークで、準備処理を除く。OSプロセス全体のRSSやPillowなどのネイティブメモリは含まないため、プロセス全体の最大メモリや段階間の差分とは解釈しない。計測できない値は `null` とし、0に置き換えない。

## 基準記録

`benchmarks/baseline.json` は測定した環境、実行回数、fixtureのハッシュと段階ごとの結果を保存する。記録は測定したOSとPython版に限った基準であり、未実行OSの性能を示さない。時間は開発者が環境を揃えて比較するための証跡で、CIの合否閾値ではない。

Windows 11 / Python 3.14.4 / mc-blueprint 0.1.0 での代表値（3回の中央値、秒）は次のとおり。

| 負荷 | 実セル / bounds | 検証 | 生成 | 描画 | CLI 3呼出合計 | 単一パス |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 疎配置 `sparse-256` | 256 / 46×1×46 | 0.1323 | 0.0006 | 0.0025 | 0.4361 | 0.1747 |
| 密な地形 `terrain-24` | 4,608 / 24×8×24 | 0.0043 | 0.0195 | 0.0403 | 0.1761 | 0.0840 |
| 空気領域 `air-24` | 13,824 / 24×24×24 | 0.0041 | 0.0343 | 0.0837 | 0.4058 | 0.1842 |
| 反復 `repeat-64` | 512 / 191×2×2 | 0.0071 | 0.0023 | 0.0045 | 0.0826 | 0.0243 |

この測定では疎配置の検証がOperation数とともに増え、広い空気領域は実セル数が多いため生成・描画が主な負荷になった。3 CLIの合計には検証・生成の繰り返しが含まれ、単一パスはそれらを一度ずつ行う。表はこの実行環境での説明用の観測値であり、将来の変更を判定する固定性能基準ではない。
