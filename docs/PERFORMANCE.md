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

## D・F追加処理の初回切り分け（Issue #210、3261ea9）

新しい証跡は [make-performance-210.json](../evaluation/records/make-performance-210.json)
に分離する。Cのbaselineと#180の性能記録は上書きしない。修正前の起点は
`3261ea9`（#209適用済み）で、#180は#209前なので、今回の修正前後の比較とは区別する。
修正後は未commitの差分で測定したため、provenanceのcommit欄は起点を示す。
修正前後の実装は各phaseの `engine_sha256` で区別し、lock・既定設定・依存版も保存する。
計測後の公開準備で、編集した既存ファイルのCRLFを指定どおりLFへ統一した。
Pythonファイルは構文木の一致を確認し、`lf_normalization` に変換前後のhash、
`publication_provenance` にLF統一後の実装hashを保存する。計測時のprovenanceは保持する。

`scripts/evaluate_make_performance.py` は#180の `performance_spec` と
`benchmark._isolated` を再利用する。1.21.11・schem・seed 42、住宅／塔のmedium／largeを
独立プロセスで直列実行する。修正前は各1回、修正後は各3回とし、全試行と中央値を残す。
終了コード3も成果物出力まで測り、測定完了と品質合格を区別する。
時間境界は子プロセス起動からworker JSON出力までで、hash検査は親側で計時後に行う。
段階値は既存stage_hookの累積経過時間と瞬間RSSであり、隣接する時点の差を段時間とする。
最後のExport区間はscene作成と成果物公開までを含む。

塔2条件は別プロセスでcProfileも取得し、累積時間・自己時間・呼出回数の上位を記録する。
プロファイルを有効にした時間を通常の時間比較へ混ぜない。
性能計測は全実行を直列にするが、別担当の読み取りreviewのCPU負荷は制御していない。
過去記録とは依存版や時間境界も完全には一致せず、C・Dとの差を今回の変更だけの効果とは扱わない。
NFR2の達成判定はHで行う。

### 特定した重複計算

cProfile有効時の塔largeでは、`_spec_sources` が親要素の編集可能パラメータを補うために
`plan_buildspec` を再実行しており、この呼び出しが累積89.4秒だった。
make全体で計画は2回、`_plan_contract` は6回呼ばれていた。
makeが既に保持する `context["massing"]` を渡し、sceneのための再計画を省いた。
Blueprintからの再開時も保存済みcontextを使う。

Materializeでは同じ役割・形状・状態が出るたびに、既に出力Palette名が決まっていても
代替材を距離順に並べ直していた。塔largeでは `_distance` が4,130,541回、累積46.6秒だった。
既存のPalette名表を先に参照し、必要状態の重複検査もまとめる。
表は1回のmaterialize内だけで使い、候補順、最初に付けるPalette名、混色比、乱数系列を変えない。

計画とDetailも、外壁・階・部品を走査するたびに同じ開口Operationの構築とbounds変換を
繰り返していた。変換済みboundsを各関数の呼び出し内で保持して再利用する。
グローバルキャッシュや新しい依存ライブラリは追加していない。

一方、塔largeの `CriticAccumulator.consume` は4回、累積12.0秒だった。
`capture_sources` の累積386.5秒は下流の生成・Weather・Criticを進めるgeneratorの時間を含み、
関数の自己時間は0.7秒である。累積値だけをscene退避の単独コストとは解釈しない。
通常実行のGenerate段も生成・Weather・Critic集計・source退避・spool書込を含む。
Critic段は集計の確定とsupport・walkabilityなどの評価で、生成中の面集計の時間とは分かれる。
これらのプロファイル値を足し合わせたり、通常実行時間へそのまま換算したりしない。

#210へ追記する目標値は、同端末・同入力で塔medium **65秒以下**、large **350秒以下**を推奨する。
D−Cの増加分をそれぞれ約65%・78%以上解消する基準で、NFR2の目標を置き換えるものではない。
#209で既に改善した分もDとの比較には含むため、今回だけの効果は修正前後の値で確認する。

### 再測定結果

同端末・Python 3.12.15での秒数。C・D・修正後は各3回の中央値、今回の修正前は各1回。
入力byteはDと一致し、LFをCRLFに戻すとCの入力hashとも一致する。

| 対象 | C | D（#180） | 今回修正前（#209後） | 今回修正後 |
| --- | ---: | ---: | ---: | ---: |
| 住宅 medium | 29.682 | 68.904 | 61.511 | 46.939 |
| 塔 medium | 21.374 | 144.405 | 70.512 | 58.264 |
| 住宅 large | 183.595 | 290.553 | 229.453 | 207.142 |
| 塔 large | 155.736 | 1019.377 | 399.665 | 338.329 |

塔は今回修正前に対してmedium約17%・large約15%短縮した。
D−Cの増加分は、#209による改善も含めてmedium約70%・large約79%を解消し、
上記の推奨目標を両方満たした。NFR2の達成は判定しない。
性能用4入力は修正前後とも終了コード0で、各3試行すべてで配置・指標・構造化診断・sceneの
4種のhashが修正前と一致した。固定16入力の不変性と合わせて20入力を確認した。

塔の段階別観測値は次のとおり。前は各1回、後は各区間の3回中央値。
Materialize区間には、次のGenerate hookまでのBlueprint検証も含む。
区間ごとの中央値の合計は、プロセス起動を含む全体時間の中央値とは一致しない。

| 区間 | medium前 | medium後 | large前 | large後 |
| --- | ---: | ---: | ---: | ---: |
| Resolve | 0.222 | 0.047 | 0.045 | 0.043 |
| Site/Massing/Detail | 3.220 | 1.498 | 36.194 | 12.464 |
| Materialize・検証 | 15.123 | 8.936 | 72.810 | 40.226 |
| Generate・生成中の集計 | 37.879 | 38.726 | 189.208 | 216.679 |
| Critic・support・walkability | 6.809 | 6.820 | 65.018 | 85.443 |
| Export・scene・公開 | 6.336 | 1.346 | 35.497 | 10.010 |

塔largeの全体時間は333.1〜381.9秒と変動した。今回変更していない生成・評価の区間にも
時間変動があり、原因は未確定である。修正前は1回で、端末全体の負荷・クロックも統制していないため、
短縮量はこの測定の観測差として扱い、統計的な保証や他端末の性能とは扱わない。
残る生成・Weather・検証・到達判定などの高速化とNFR2の達成はHの範囲とする。

### 再実行

この節は最新develop取り込み前の初回記録で、対応するスクリプトは
`756ca3d:scripts/evaluate_make_performance.py` にある。
現行スクリプトによるfc8fbf9単体との再比較は、次節の手順を使う。

再測定時はPython 3.12のタスク専用venvから、環境指定のコマンド記録を介して次の順に実行する。
`before`・`profile`・`fixed-before` は実装修正前に実行する。
同じ記録の完了済みケースは再実行しないので、新しい比較では別worktreeを使うか、
既存の証跡と一時成果物を保全してから新しい記録を作る。

```text
python scripts/evaluate_make_performance.py --phase before
python scripts/evaluate_make_performance.py --phase profile
python scripts/evaluate_make_performance.py --phase fixed-before
# 対象の性能修正を適用
python scripts/evaluate_make_performance.py --phase after
python scripts/evaluate_make_performance.py --phase fixed-after
```

一時出力・生の `.prof` は `benchmarks/.tmp/issue-210/` に残す。
sceneは入力絶対パスを含むため、修正前後で同じ入力パスとbyteを使用する。
別端末のscene hashそのものとの一致は要求しない。

## 最新develop統合後の再検証（Issue #210、fc8fbf9）

#214（PR #216）・#215（PR #218）を含む `fc8fbf9` を取り込み、
[make-performance-210-develop-fc8fbf9.json](../evaluation/records/make-performance-210-develop-fc8fbf9.json)
へ新しい比較を分離する。初回の3261ea9基準の記録は保持する。
WIPは `756ca3d`、mergeは `a9f601f` で、再計測結果はmerge後の作業差分として保存する。

比較用コードは `git archive` で担当領域内へ展開し、全461 blobがfc8fbf9と一致することを照合した。
archive時だけ `core.autocrlf=false` を指定してGit blobのbyteを維持し、Git設定は永続変更しない。
beforeの各workerではこの参照コードを先にimportし、afterは担当worktreeのコードをimportする。
各試行にpipelineの実パスとengine・lock・dataのhashを保存し、対象外の実装を読んだ場合は停止する。

入力パスは両実装で共有し、固定16件と性能用4件について、配置・全指標・構造化診断・
展開後scene JSONのSHA-256を比較する。前の3261ea9の出力は比較基準に使わない。
性能用のbeforeは各1回、afterは住宅・塔のmedium／largeを各3回、独立プロセスで直列実行する。
計時境界と段階値の定義は初回と同じで、workerの実装識別情報のJSON出力も全体時間に含む。
hash検査は計時外。端末全体の負荷は統制せず、NFR2の達成判定も行わない。

### 再測定結果

秒数はdevelop単体が各1回、統合実装が各3回の中央値。全試行の終了コードは0で、
全12試行の配置・指標・構造化診断・scene hashがそれぞれdevelop単体と一致した。

| 対象 | fc8fbf9単体 | fc8fbf9＋#210 | 目標 | 判定 |
| --- | ---: | ---: | ---: | --- |
| 塔 medium | 66.216 | 56.908 | 65以下 | 達成 |
| 塔 large | 471.330 | 283.235 | 350以下 | 達成 |
| 住宅 medium | 44.483 | 57.325 | 規定なし | 観測上増加 |
| 住宅 large | 282.698 | 207.027 | 規定なし | 観測上短縮 |

塔の段階別値。前は1回、後は区間ごとの3回中央値で、全体時間の中央値とは集計境界が異なる。

| 区間 | medium前 | medium後 | large前 | large後 |
| --- | ---: | ---: | ---: | ---: |
| Resolve | 0.043 | 0.042 | 0.043 | 0.042 |
| Site/Massing/Detail | 3.531 | 1.455 | 28.670 | 12.363 |
| Materialize・検証 | 15.978 | 8.921 | 72.194 | 36.833 |
| Generate・生成中の集計 | 37.180 | 37.547 | 238.028 | 170.030 |
| Critic・support・walkability | 4.422 | 6.802 | 84.719 | 56.245 |
| Export・scene・公開 | 4.199 | 1.316 | 46.763 | 6.964 |

住宅mediumの増加は主にGenerate区間の38.721→52.501秒として観測した。
この経路のコードは#210で変更しておらず、原因は未特定である。
塔largeも268.6〜371.4秒、住宅largeも205.0〜272.3秒と試行間の変動がある。
測定中のCPU全体利用率はCIMの瞬間値で22%だったが、負荷・クロックは統制しておらず、
これだけで変動原因を特定したとは扱わない。beforeは1回のため、差を変更単独の因果効果とせず、
住宅mediumを短縮したとも記載しない。中央値による塔の目標達成とNFR2全体の達成は区別する。

参照コードは `output/issue-210/develop-fc8fbf9/`、一時出力は
`benchmarks/.tmp/issue-210-fc8fbf9/` に分離する。参照コードを同じcommitから展開・blob照合した後、
Python 3.12のvenvからコマンド記録を介して実行する。日本語警告の文字コードを揃えるため、
親・子の両プロセスをUTF-8モードで起動する。

```text
python -X utf8 scripts/prepare_make_reference.py
python -X utf8 scripts/evaluate_make_performance.py --phase fixed-before --baseline-root output/issue-210/develop-fc8fbf9
python -X utf8 scripts/evaluate_make_performance.py --phase fixed-after
python -X utf8 scripts/evaluate_make_performance.py --phase before --baseline-root output/issue-210/develop-fc8fbf9
python -X utf8 scripts/evaluate_make_performance.py --phase after
```

## H01 地形・大規模の固定基準

Hの回帰入力と各入力のSHA-256は [large-scale.json](../evaluation/cases/large-scale.json) に固定する。中世城・街並み・洞窟・地下都市に加え、山岳/島嶼地形は起伏なし/あり×侵食なし/ありの8条件を定義する。12条件それぞれにmedium `64×48×64` / huge `256×128×256` とseed `42/73/101` を組み合わせた72入力がある。ハッシュはBuildSpecへ規模・寸法・seedを追加し、キー順を整えたUTF-8 compact JSONのSHA-256。現起点で未実装の入力は期待仕様として保持し、生成できた実測結果を作らない。

住宅・塔の現行対照測定は次で実行する。既存#210と同じ4入力（medium/large × 住宅/塔）を独立子プロセスで各3回測り、段別stage_hook、全体時間、OSピークRSS、品質/終了コード、出力hashを [h-baseline.json](../evaluation/records/h-baseline.json) に保存する。既存住宅archetypeは256×128×256のhuge寸法に外形が収まらないため、hugeはH固定課題の期待入力に限り、この対照測定へ含めない。

```powershell
python scripts/evaluate_make_performance.py --h-baseline
```

時間は子プロセス起動から `make` の出力・scene・run確定まで。段の値は累積経過時間と段開始時のWorking Setで、隣接値の差を段時間として読む。ピークRSSは独立したプロセス全体のOS計測値を使う。失敗試行は中央値から除外せず全値を残し、品質不合格と計測完了を区別する。LLM・jar取得・viewer表示・profilerは含めない。この端末はCPU 8コア/16論理CPU、物理メモリ64GiB、RTX 4070 SUPERのためNFR1端末に該当せず、結果からNFR1〜3の達成を判定しない。測定区間に別worktreeのPythonプロセスも稼働しており、CPU負荷を完全には隔離できなかった。

## 完成spoolの再利用（#234）

makeとemit Blueprintのbuildは、完成spoolの行を既存の `export_rows` へ直接渡す。
出力用の小volume復元と二つ目のblock spoolをなくし、既存の行単位のpalette変換と
原子的出力を再利用する。別実行のspoolを保持するキャッシュは追加しない。
core重複検査にはタイル候補索引を使い、範囲検査・体積の累積による被覆確認と合わせて
任意順の完成core列を検査する。

```powershell
python -X utf8 scripts/evaluate_spool_reuse.py
```

起点 `85306dd` の参照コードを `output/issue-234/reference` へ展開してGit blobと照合し、
#232の4入力を同じ入力パス・出力パスで変更前後それぞれ独立プロセスで3回ずつ実行する。
記録済みの試行は再実行せず、続きから測定する。再測定には既存の
`evaluation/records/spool-reuse-234.json` を別名で保全してから実行する。
配置・指標・構造化診断・展開sceneの4種hashは計時外で比較する。
実装・配色identityを含むenvironment、段別時刻、全体時間、OSピークRSSと全生値は
[spool-reuse-234.json](../evaluation/records/spool-reuse-234.json) に保存する。

block spoolのread/write byte数と書込み回数はファイル操作を数えた論理I/Oであり、
OSキャッシュや物理ディスクI/Oの測定値ではない。core数別の比較は同寸法の非重複coreを
128・512・2048個並べた入力を各3回測る。全件pytestと他タスクが並行稼働したためCPUは
隔離されておらず、時間差だけで本変更の効果やNFR2/3の達成を判定しない。

次の数値は2026-11-04の反証修正前（先頭coreの寸法を索引粒度とした実装）の3回中央値。
反証後は64ブロック幅の固定索引へ変更した。性能は再測定しておらず、下表を修正後の性能値とは扱わない。
全24試行が終了コード0で、配置・指標・構造化診断・
展開sceneの4種hashが変更前後・反復間で一致した。全入力byteも#232と一致する。
各試行のblock spool書込みは2回から1回へ減った。

| 対象 | 変更前（秒） | 変更後（秒） | spool書込前（bytes） | spool書込後（bytes） |
| --- | ---: | ---: | ---: | ---: |
| 住宅 medium | 58.633 | 57.747 | 1,270,080 | 635,040 |
| 塔 medium | 57.863 | 56.443 | 1,309,792 | 654,896 |
| 住宅 large | 295.451 | 295.047 | 9,290,304 | 4,645,152 |
| 塔 large | 366.141 | 292.672 | 11,125,000 | 5,562,500 |

住宅largeのblock spool読込みは49,585,372から31,053,532 bytes、塔largeは
830,622,000から808,372,000 bytesへ減った。評価などで使う完成spoolの読込みは残る。
住宅largeの全体時間の差は小さく、塔largeの変更前は322.853〜400.218秒、変更後は
291.448〜369.501秒と大きく変動した。全体時間の差を本変更だけの短縮量とは扱わない。

core数別は1セルのcore作成・検査・spool書込みを含む3回中央値で、生成や評価の全体時間とは異なる。

| core数 | 変更前（秒） | 変更後（秒） |
| ---: | ---: | ---: |
| 128 | 0.008300 | 0.003181 |
| 512 | 0.094305 | 0.011049 |
| 2048 | 1.283499 | 0.043591 |

core数が128から2048へ16倍になると、観測時間は変更前の約154.6倍に対し変更後は約13.7倍だった。
索引の候補数はcore寸法・位置にも依存するため、この入力から任意のcore列の上限時間は保証しない。

### 反証修正とdevelop統合後の再検証

先頭の端数coreが1³の場合に索引がセル単位へ膨張する問題を修正し、粒度を64ブロック幅に固定した。
65³を8 coreで覆う通常順・逆順を、原点と負座標のタイル境界がずれる条件で検査する。
回帰テストは書込み中のtracemallocピークを8 MiB未満に制限し、配置hashの順序不変性も確認する。

#233・#236を含むdevelop `43a5b27` を取り込んだ後、既存 `scripts/evaluate_projection.py` の
`run` を使い、取り込み対象developと統合後の実装で同じ入力を生成した。
固定16件（4版×両形式×住宅・塔）、様式・回転36件、core 7の通常順・逆順4件の計56件で、
配置・指標・構造化診断・展開sceneの4種hashと終了コードが一致し、全件終了コード0だった。
性能は再測定していない。参照commit・統合commit・実装hash・入力hashと照合結果は
[spool-reuse-234-verification.json](../evaluation/records/spool-reuse-234-verification.json) に保存する。
