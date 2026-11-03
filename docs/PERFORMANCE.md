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

## 生成コア初期基準（Issue #144）

`scripts/generate_benchmark_profiles.py` は既存Operationだけで五負荷の合成Blueprintを作る。`profile-*` fixtureは既存の段階別baselineに混ぜず、完全生成の独立系列として測る。

```powershell
python scripts/benchmark.py --generate-profiles
python scripts/benchmark.py --profile-benchmark
```

`benchmarks/generation-profiles.json` にfixtureごとの寸法、seed、定義版、SHA-256、viewerの対応sceneと入力差を記録する。5種のmediumは64×48×64、城のhugeは256×128×256。固定seedと同じ定義版から同一JSONを再生成する。

各試行は新しいPython子プロセスを起動し、JSON読込、検証、モデル構築、生成、support検査、`.schem` 出力までの壁時計時間を親側で測る。ピークRSSは子プロセス全体の最大Working Set（Windows）またはOSの最大RSSで、`tracemalloc`とは別に取得する。`benchmarks/generation-baseline.json` は3回分の生値と中央値、実セル数、出力サイズ、OS・CPU・物理メモリ・GPU・Python版を保存する。失敗・取得不能の値は理由付きで記録する。基準測定は閾値による合否判定ではなく、この端末での実測証跡である。

| 負荷 | 寸法 | 生成時間中央値 | プロセスピークRSS中央値 |
| --- | ---: | ---: | ---: |
| 城 medium | 64×48×64 | 1.277秒 | 74.0 MiB |
| 街並み medium | 64×48×64 | 1.121秒 | 69.8 MiB |
| 地形 medium | 64×48×64 | 2.139秒 | 75.8 MiB |
| 地下 medium | 64×48×64 | 1.533秒 | 75.8 MiB |
| 植生 medium | 64×48×64 | 1.022秒 | 69.0 MiB |
| 城 huge | 256×128×256 | 17.651秒 | 278.0 MiB |

時間は新規Pythonプロセスの起動を含む3回の中央値。各試行の生値はJSON記録を参照する。測定端末はWindows 11（10.0.26200）、AMD Ryzen 7 5700X 8-Core Processor、物理メモリ68,640,256,000 bytes、NVIDIA GeForce RTX 4070 SUPER、Python 3.12.15。

viewer側の参照は `mc-blueprint-viewer` の `7b2e0f4:docs/performance.md`。負荷対応は城→castle、街並み→town、地形→terrain、地下→underground、植生→vegetation。viewerはfixture-v1のscene JSONを読み込み・描画するのに対し、ここでは既存OperationのBlueprintを検証・生成してschemへ出力するため入力データも処理範囲も異なる。viewerの初回表示・fpsと本測定のend-to-end生成時間、またviewerのプロセスRSS集計と本測定のPython子プロセスピークRSSは直接比較しない。

## 生成コア測定（Issue #153）

2026-11-03に同じ6個のBlueprint fixtureを変更せず、新旧エンジンで3回ずつ測定した。新系列はBlueprint入力で利用可能な `generate_regions` → 既定Weather → `export_generated_regions` を対象とする。旧系列はread・parse・validation・model load・generate・support・schem exportを含む。処理範囲が異なるため表は各系列の観測値を並べたもので、速度差の因果比較ではない。全生値、入力hash、環境、設定・lock・engine・commit識別子は [generation-core-baseline.json](../benchmarks/generation-core-baseline.json) と [generation-baseline.json](../benchmarks/generation-baseline.json) を参照。

| 負荷 | 寸法 | 旧時間中央値 | 旧ピークRSS中央値 | 新コア時間中央値 | 新ピークRSS中央値 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 城 medium | 64×48×64 | 1.277秒 | 74.0 MiB | 6.749秒 | 73.5 MiB |
| 街並み medium | 64×48×64 | 1.121秒 | 69.8 MiB | 4.998秒 | 71.0 MiB |
| 地形 medium | 64×48×64 | 2.139秒 | 75.8 MiB | 6.605秒 | 71.8 MiB |
| 地下 medium | 64×48×64 | 1.533秒 | 75.8 MiB | 6.272秒 | 70.5 MiB |
| 植生 medium | 64×48×64 | 1.022秒 | 69.0 MiB | 4.628秒 | 70.7 MiB |
| 城 huge | 256×128×256 | 17.651秒 | 278.0 MiB | 195.677秒 | 91.9 MiB |

新系列のhugeはNFR2の2分目標を超過した。RSSはこの端末ではNFR3の2GB以内だった。medium値は10秒以内だが、make全体の測定は別系列であり、NFR2の達成判定にはmakeの値を用いる。

BuildSpecのmake全体を固定課題住宅・塔およびmedium・largeで測定した値は [make-baseline.json](../benchmarks/make-baseline.json) に記録する。mediumは64×48×64以内、largeは128×96×128以内に収まるよう計画寸法を指定し、実出力寸法も各試行に保存する。段階値は `stage_hook` 呼出時点の累積経過時間とその時点のWorking Setであり、段階単独の時間・ピークRSSではない。end-to-endピークRSSは独立子プロセス全体の値として別に記録する。

| make入力 | 要求上限 | 実出力寸法 | 時間中央値 | ピークRSS中央値 |
| --- | ---: | ---: | ---: | ---: |
| 住宅 medium | 64×48×64 | 63×40×63 | 29.682秒 | 182.6 MiB |
| 塔 medium | 64×48×64 | 61×43×61 | 21.374秒 | 161.8 MiB |
| 住宅 large | 128×96×128 | 127×72×127 | 183.595秒 | 193.3 MiB |
| 塔 large | 128×96×128 | 125×88×125 | 155.736秒 | 177.5 MiB |

mediumの住宅・塔はNFR2の10秒目標を超え、large住宅・塔も2分目標を超える。巨大負荷はNFR3の2GB以内だった。住宅・塔の生値、各stage_hook時点の累積経過時間と段開始時RSS、実寸法はJSONを参照する。

測定端末は上記のWindows 11 / Ryzen 7 5700X / RTX 4070 SUPER機であり、NFR1の内蔵GPU・8GB端末では未測定である。街並み・地形・地下・植生の計画生成はHの範囲で、今回のBlueprint区間計測は計画品質を評価しない。人間比較・3D viewer・初回表示とfps・モデル使用量比較はIまたはFの範囲であり未測定である。
