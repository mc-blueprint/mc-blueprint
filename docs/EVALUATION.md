# 建築品質評価

## 生成後のCritic指標

`mcblueprint.critic.evaluate` は型付き計画と `GeneratedRegion` の列を受け、各coreを一度だけ集計する。列・梁は全期待セルが同じownerの`structure`予約かつ`frame`役割を保つrunの割合で判定する。欠損座標はE101とし、消費したcoreに含まれなかった期待セルは`unobserved`理由を付けて失敗にする。外壁も計画した非開口投影セルのcore被覆を確認し、欠損・未観測をE203に分ける。開口は外壁面積の分母から除外する。

`flat_ratio` は各面の(u,y)固定4×4窓で測り、端は存在する投影セルだけを使う。round面は計画のsegmentごとに窓を分け、基準深度からの差を比較する。平坦な窓の比率が0.35を超えるとE201となる。`decoration_density` は計画で装飾対象とされたOperation由来の最終非airセルを外壁100投影面あたりに換算し、様式の0〜1範囲を100倍した区間と比較する。`facade_mix` は面・role内の最大ブロックID占有率で、固定ID roleは除外してE305を警告する。状態違いは同じブロックIDとして数える。

Criticの面要約は計画された投影セル数から常駐量を見積もり、`max_memory_mb` を超える計画を拒否する。全体集計は分子・分母を保持し、局所FAILを全体平均で打ち消さない。既存の`palette_relation`、`support`、`walkability`判定は従来の処理を維持する。

## 目的と範囲

この評価は Blueprint を出力したモデル、モデルの設定、seed、修正履歴を記録し、同じ課題の結果を追跡・比較するためのもの。課題は [fixed-cases.json](../evaluation/cases/fixed-cases.json)、このリポジトリで取った初期証跡は [baseline.json](../evaluation/records/baseline.json) に置く。既存 examples は構文・Operationの参照資料として使い、評価対象の正解や美観の正本とはみなさない。

`success` は記録された機械検証が成功したこと、`failure` は実行した検証または課題条件を満たさなかったこと、`unhandled` は未試行または対応可否の証拠がなく処理されなかったことを表す。未試行を failure と混同しない。実機へ配置していない記録からゲーム内の外観・向き・歩行可能性を結論づけない。

### 使用量記録

モデルやSDKに依存しない使用量比較は、建築品質記録と分離して `evaluation/records/usage.json` に `schemaVersion: 1`、`recordType: "usage"`、`attempts` 配列で記録する。項目例とCLI入力例は [usage-example.json](../evaluation/records/usage-example.json) を参照する。この例は形式を示す合成データであり、実測値ではない。各試行は一意な `attemptId`、比較単位 `comparisonGroupId`、課題 `taskId`、成果物ID（まだ成果物がなければ `null`）、成果物参照（`kind` と `reference`）、model、effort、日時、取得元、`success` / `failure` / `interrupted` の結果、修正元attempt、制作モード、品質判定と根拠、使用量を持つ。modelやeffortの未取得値は理由を添えた `unknown` とする。品質判定は `accepted` / `rejected` / `unknown`。`accepted` は機械検証の成功を意味せず、人間または別途明記した品質判定の結果である。未判定は `unknown` とする。

制作モードは `single-use`、`reused-production`、`library-preparation` のいずれか。比較時に償却率を仮定しないため、モードごとに集計を分ける。使用量項目は `value`、`unit`、`status` を持ち、取得済み値の状態は `measured` または `estimated`、取得できなかった値は `value: null`、`status: "not-collected"` と `reason`、対象外は `not-applicable` と理由で記録する。取得元はattempt単位の `source.kind` と `source.reference` に残す。時間、呼出回数、人間の手直し、入力/出力token、費用は混ぜずに集計する。費用は通貨単位ごとに分ける。各通貨の `complete` はその通貨に割り当てられた行だけの充足状態を表し、cost項目の最上位 `complete` と `notCollectedCount` は通貨不明の行を含む比較群全体を表す。単位成果あたりcostにも群全体の状態を出す。`correctionCount` は修正元attemptを持つ試行数で示す。

`imageTokens` と `cachedInputTokens` は入力tokenの内訳として `includedIn: "inputTokens"` を指定する。これらの内訳を入力合計に加算しない。提供者の合計tokenは `providerTotalTokens` に別記し、入力/出力との合算に使わない。集計器は項目ごとに実測小計・推定小計・未取得数を分け、実測と推定を足した確定値や未取得をゼロとして出力しない。欠けた値がある単位成果あたり指標は `partial` として小計を示す。対象群に受理成果がなければ `no-accepted-artifacts` とし、割り算しない。

プロンプト文字数、Blueprintの文字数、差分量から実トークン数を導かない。実トークン値を取得できない場合は `not-collected` と取得できない理由を記録し、ゼロや根拠のない推定値で埋めない。集計器は実測値がないデータから削減率や効率改善を結論づけない。

集計群は `comparisonGroupId`、model、effort、制作モードで識別する。主指標の分子は同群に選んだ全attemptの使用量で、失敗・中断・修正前も含む。分母は品質判定が明示的に `accepted` となった重複しない成果物数であり、機械的なrun成功や試行数を使わない。`acceptedArtifactsOnly` は受理成果物に紐づくattemptだけの補助内訳である。各成果物の品質状態には最新日時の明示判定を用いる。`python scripts/aggregate_usage.py evaluation/records/usage.json` は検証済みJSON集計を標準出力へ返す。失敗入力では終了コード2を返す。同じ入力から同じJSONを生成する。

## 固定課題

`evaluation/cases/fixed-cases.json` は4課題を持つ。小規模住宅、複数階塔、共有壁を持つ二棟接合、斜面上の建物配置を同じ要求文・必須条件で実行する。各課題は参照資料のパス、用途、ライセンス、SHA-256を明記する。既存 examples はリポジトリの MIT License の範囲で参照する。課題文、必須条件、参照資料を変更したらケース定義の変更として残し、過去記録と混ぜない。

## 実行手順

1. ケースIDとケース定義ファイルの SHA-256 を記録する。ケースの課題文と必須条件はモデルへ同じ内容で渡す。
2. 実行日時、リポジトリcommit、OS、Python、mc-blueprint版、Minecraft版、利用可能な依存、モデル名・版・effort、seedを記録する。未確認・未取得は `unknown` と理由を記す。既存作例の統制実行ではモデル・effortを `not-applicable` と理由付きで記録する。
3. Blueprintの相対パスと SHA-256 を記録し、`mcblueprint validate`、次に `mcblueprint build` を実行する。検証失敗後はbuildしない。出力形式、相対パス、SHA-256、境界、ブロック数と終了状態を記録する。
4. 課題の固定視点一覧を期待値として記録し、プレビューまたはゲーム内画像を取得する。取得した視点名・画像パス・画像 SHA-256 と未取得視点を分けて記録する。CLIプレビューで表せる内部断面は `--layer` を使い、全階ではなく指定した階だけを出力する。基準塔では1階をy=0、中間階をy=10としている。ゲーム内の画像はMinecraft版、座標、視点位置・向き、描画距離、リソースパックも記録する。内部視点・断面などCLIの標準view名で表せない視点はケース定義にある名前を使い、実際の撮影位置を記す。
5. 修正のたびに履歴へ順序、変更理由、入力Blueprint hash、出力Blueprint hash、実行状態を追記する。最終成果のみで履歴を置き換えない。
6. 人間評価と実機比較を行っていない場合は明記する。未実施を成功扱いにしない。

## 評価方法

外観、内部、要求充足を独立して記録する。自動美観スコアは正本にしない。一般参加者と熟練評価者の結果を混ぜず、各群の評価数、個別値または集計方法を残す。外観と内部は1〜5点（1: 大きな問題があり意図を把握しにくい、3: 要求を概ね満たす、5: 一貫して仕上がり、細部まで明瞭）で記録する。要求充足はケースの各必須条件について `pass` / `fail` / `unknown` と根拠を記録する。該当画像と根拠を評価資料として保存し、評価者に参照用作例を見せる場合はその事実も記す。

参照資料と評価資料は別のパスに保存する。参照資料は利用許諾が明確なものだけを使い、出典・権利・用途を記録する。評価資料は生成物の固定視点画像、採点票、根拠で構成する。参照用の作例画像を評価用画像として再利用しない。

## 記録形式

`evaluation/records/baseline.json` を最小の記録例とする。新しい比較では `schemaVersion` を保ち、`recordType` を `run` として同じ項目を記録する。必須項目は次のとおり。

- 課題ID、ケース定義hash、実行状態、実行日時、commitと環境
- 要求文、参照資料と出典、Blueprint相対パス/hash、出力形式・相対パス/hash
- Minecraft版、model/effort、seed、検証・build状態
- 期待視点、取得済み視点、未取得視点、画像hash、一般/熟練別の外観・内部評価、条件ごとの充足判定
- 修正履歴、実機比較状態、未実施理由

`null` の値は状態と理由を併記する。値が対象外なら `not-applicable`、実行前や未実施なら `not-run`、評価していない採点なら `not-evaluated` と理由を記録する。値の取得を試みて確認できなかった場合は `unknown` と理由を記録する。取得済み値だけを空のまま残さない。ハッシュは小文字16進の SHA-256 とする。`evaluation/controls/unknown-operation.json` は検証器の負例対照であり、建築品質課題の失敗例やモデル評価には数えない。

## 現在の基準状態

基準commitで小規模住宅と塔の既存例はvalidate/buildに成功した。斜面上の建物配置と二棟接合は、現時点では実行記録がなく `unhandled`。未知Operationの負例対照は検証で拒否された。人間の一般/熟練比較、Minecraft実機への配置比較は行っていないため未完了である。実機確認はMinecraft版と配置先Worldの許可を得てから記録を追加する。

## ハッシュ対象fixtureの改行

SHA-256は改行を含む実ファイルのbyteから計算し、任意のユーザー入力を正規化しない。固定評価が参照する `examples/house.json`、`examples/tower.json`、`examples/village_path.json`、`evaluation/cases/fixed-cases.json` と負例対照だけは `.gitattributes` でLFに固定する。WindowsのCRLFとGit checkout後のLFで参照hashが変わることを防ぐためである。

改行契約や参照hashを修正した場合は、参照ファイルの最終byteから参照SHAを更新し、その後で最終ケース定義のSHAを計算して記録へ反映する。課題文・入力JSONの意味が不変でもbyte識別子は変わる。既存出力・画像・実行日時・評価結果は再生成せず、過去の別成果物や配布済み評価資料のhashを暗黙に更新しない。


## 後続fixtureのユーザー確認（2026-10-20）

Minecraft Java Edition 1.21.11の確認用ワールドで、配布した `manual-review-20261020` の家・塔・接合二棟・斜面建築について、要求充足と外観・内部はすべて正常とのユーザー報告を受けた。これは後続fixtureの確認であり、上記の当初baselineにある `unhandled` や機械実行結果を更新するものではない。

今回の比較画像ファイルは未提出で、AIが新たに撮影・画像照合した記録ではない。一般／熟練の評価者区分と外観・内部の1〜5採点は未回答、provider token等も未測定のまま保持する。正常報告から数値採点、人間同等以上の品質や使用量削減を推定しない。

## makeの実測品質診断

`make` はWeather後の各coreを一度だけ集計し、計画した柱・梁・外壁・開口と照合する。
支持・歩行は完成spoolから検査する。haloを最終状態とみなしたり、全体の属性付きvolumeを復元したりしない。
出力隣の `<出力>.run.json` に既存の文字列 `diagnostics` と、`metrics`・`structuredDiagnostics` を保存する。
構造化診断は `code`・`metric`・`severity`・`element`・`source`・`actual`・`expected`・`repairPaths` を持つ。
修正先は利用可能なBuildSpec入力項目だけを示す。既存の `validate --json` の契約は変えない。

| 指標／コード | 測定と判定 | 修正・停止 |
| --- | --- | --- |
| column_continuity / E101 | 期待柱と梁の非air・frame・structure予約・ownerを照合。柱の合格割合は1.0 | 構造エラー1。通常生成の欠損はエンジン不具合として停止 |
| flat_ratio / E201 | 開口を除いた固定4×4投影窓（端は部分窓）の深度一定率。0.35以下 | FAIL。`config.decoration.depth`・`config.decoration.density` |
| decoration_density / E202 | 開口以外の外壁100投影面あたり、明示装飾Operationの最終非air数。様式範囲の100倍と比較 | FAIL。`config.decoration.density` |
| facade_surface_missing / E203 | 計画開口に含まれない外壁の欠損 | FAIL。該当override・`config.weathering.missing_blocks` |
| facade_mix / E305 | 建物・面・wall_main/wall_secondary役割ごとの最多ブロックID占有率。0.85以下 | WARN。`config.palette.mix`。固定ID役割は除外 |
| opening_recess / opening_recess_blocked | 後退域が構造・通路の予約と重なるため開口後退量を0にした。`actual`は0、`expected`は要求された後退量 | WARN。`config.decoration.depth`。この診断だけでは終了コードを変えない |
| palette_relation | 既存の配色関係診断 | FAIL。役割材質・`config.palette.contrast`・`config.palette.saturation` |
| support / walkability | 既存の支持・到達判定。`blocked` と `unsupported` を区別 | FAIL。該当箇所・床材など根拠のある項目だけ修正 |

開口後退の診断はDetail計画に保存し、通常実行とDetail・Blueprintからのresumeで
同じ文字列診断・構造化診断・CLI表示へ渡す。Pythonの警告としては出力しない。

外壁の被覆は開口以外の基準セルの非airと期待面に対応する属性を確認し、手前の装飾では欠損を補わない。
欠損セルのsource・operationを診断へ残し、override由来なら `overrides[<id>]` を修正先に含める。
外壁の最外深度は被覆とは別にcore間で合成し、roundの輪郭の段差は区間ごとの基準深度として差し引く。
roundの面区間は柱間の円弧全体を含め、同じ投影rayに並ぶ輪郭セルも被覆対象とする。
4×4窓の原点は計画のorigin、横軸はuAxisに沿う符号付きブロック距離、縦軸はoriginからの高さとし、
世界座標の最小値で区切り直さない。対角法線の面選択も建物のローカル軸で確定してから回す。
面単位のFAILを全体平均で相殺しない。計画の `decorations` は
`{"operation": "最終セルと同じOperation ID", "element": "facades[].id", "face": "north"}` の列である。
Operation IDの文字列から装飾を推定せず、この表に含まれるセルだけを数える。
外壁が存在すれば装飾指定が空でも密度0として様式の下限と比較する。depth=0も同じ扱いとし、
装飾密度をn/aにできるのは計画上の外壁がない場合だけである。
固定IDだけの外壁混合も対象外であり、測定失敗・計画や属性の欠落はn/aへ変えない。
固定課題におけるn/aの受入判定は別途行い、実行成功から品質合格を推定しない。

終了コードはFAILなし（WARNのみを含む）0、検証・構造エラー1、引数・I/O・資源不足2、
品質FAIL 3。3では成果物と `status: "failed"` のrun記録を保存する。
1・2では完成成果物を公開せず、既存成果物・run記録を保全する。
これらは内部閾値であり、美観の受入基準や人間評価を代替しない。

## 生成コア固定課題（Issue #153）

[generation-core.json](../evaluation/records/generation-core.json) に固定住宅・塔を全4版×両出力形式で記録した。各組でBuildSpecから `make` し、emitしたBlueprintを従来の `build` 経路でも出力して、ブロック配置の一致と各必須条件の機械判定を保存する。条件判定は到達可能な経路・ドア、床ブロックと壁面の被覆、屋根の居住域上の配置、塔の階別到達・床・窓ガラスを出力ブロックから調べる。これらは課題文の構造条件を機械判定するもので、美観やMinecraft実画面の確認ではない。

同じ記録の `regression_checks` はseed再現とseed差、core 16/32/64の配置一致、各中間段からの再開、既存examplesのvalidation・生成セル数を保持する。個々の固定課題結果、計測環境と入力ケースのhashは同JSON内を参照する。旧Blueprint fixtureの固定hash・検証試験は `tests/test_benchmark.py` のfixture回帰試験も参照する。

この記録はCの住宅・塔を対象とする。街並み・地形・地下の計画品質はH、ブラインド人間比較・小型モデル・トークン総量比較はIの評価範囲であり、今回実施していない。Minecraftの実画面確認も未実施である。

性能目標との照合は [PERFORMANCE.md](PERFORMANCE.md) と [make-baseline.json](../benchmarks/make-baseline.json) を参照する。NFR2のmedium 10秒目標に対し住宅29.682秒・塔21.374秒、large 120秒目標に対し住宅183.595秒・塔155.736秒で、いずれも未達だった。NFR3の巨大入力ピークRSS 2GB目標に対し、最大観測値はmake住宅largeの193.3MiBで目標内。NFR1の内蔵GPU・8GB想定端末では未測定であり、この端末の結果から性能を推定しない。

## 構造と装飾の固定課題（Issue #180）

D用の[事前固定条件](../evaluation/cases/structure-detail.json)と
[実測記録](../evaluation/records/structure-detail.json)を追加した。Cの記録とbaselineは更新しない。
初回測定の起点は `b416651`、設計参照は `.github-private` の `b2f7ab6`（D35〜D41）。
#209適用後の品質記録と境界接続記録は、#207・#180を統合した実装で再生成した。測定した実装版は各記録のprovenanceを参照する。
その後、#214の外壁輪郭・被覆・回転の評価修正でも同じ固定入力と閾値で品質・境界接続記録を再生成した。
16組と追加9ケースの終了コード0、平坦率0.0、柱連続率1.0を維持した。固定round塔では
非開口セル数が273から295へ増え、同じ計画を利用する装飾の予算・配置も変わり、
ブロック数が686から690へ増えた。入力や密度範囲、4×4窓・0.35・1.0の算式と閾値は変更していない。
`tests/test_planning.py` の固定記録監査では16入力すべてについて、形状から求めた外壁の被覆、
実際の装飾背面の壁を消す負例、同一の壁・装飾・開口計画を4方向へ回した指標の同値性を検証する。
実行前に住宅・塔のBuildSpecを全4版×schem/litematicの16入力として保存し、
seed 42、柱連続率1.0、平坦率0.35以下、面ごとの局所FAILなし、構造エラーなし、
Cの床・入口・階到達・窓の必須条件維持を固定した。入力や閾値を結果に合わせて変更していない。

16組すべてで `column_continuity=1.0`、`beam_continuity=1.0`、`flat_ratio=0.0`、
構造・外壁欠損なし、Cの必須条件維持を確認した。makeの終了コードも全組0で、
警告E305は残す。emitしたBlueprintをCLIのbuildで同形式へ出力し、世界座標と
ブロックの全状態（向き・上下などを含む）の一致を確認した。圧縮ファイルのbyte一致は要求しない。

記録には入力・ケース定義・engine・lock・データ・測定スクリプトの版とhash、seed、
指標の分子・分母、面・役割ごとの密度と材質占有率、各面の平坦率と投影窓の深度範囲を残した。
runの全体指標に加え、既存CriticAccumulatorへ同じBlueprintのWeather後coreを流し直して
面別の投影窓を保存し、全体指標との一致を記録テストで確認する。
所要時間とOS最大RSSは各組の専用子プロセスでmake直後に測り、build比較と面別の再集計を含めない。
段開始時のRSSは瞬間値であり、段単独のピークではない。

住宅と塔の両方でseed 42の再現・73との差、core 16/32/64と逆順、
Resolve/Site/Massing/Detail/Blueprintからのresumeについて、配置・指標・構造化診断の一致を確認した。
90度回転も品質合格し、期待柱の先頭セルをairにした負例ではE101と連続率低下を検出した。
旧examples 9件は検証・生成に成功し、Cの生成セル数を維持している。

固定課題は住宅9×13×11、塔11×15×11で計画範囲もcore 16に収まり、16/32/64では境界を跨がない。
同じ固定入力（1.21.11／schem）をcore 7で複数領域へ分ける追加確認を
[境界接続記録](../evaluation/records/structure-detail-partition.json)へ分離し、正順・逆順の
core一覧と配置・指標・構造化診断の一致を確認する。
住宅は8core、塔は12coreを実際に消費した。逆順の塔ではWARN診断の列挙順が変わるが、
内容・合否は一致する。元の診断配列も保存し、順序一致と内容一致を別項目にしている。

### 追加様式・塔形状の改善（Issue #209）

1.21.11／schemの3様式×住宅／round塔／square塔の9ケースを#209適用後に再測定した。
以前は終了コード3だった6ケースを含め、全9ケースで終了コード0となり、E201・E202・palette_relationは出なくなった。
結果は実測記録の `regression_checks.styles_and_shapes` に保存している。
固定入力からstyleと塔のshapeだけを切り替え、住宅の明示的なgable屋根、seed、入力と閾値は維持した。

| 対象 | medieval | japanese | modern |
| --- | --- | --- | --- |
| 住宅 | 合格 | 合格 | 合格 |
| round塔 | 合格 | 合格 | 合格 |
| square塔 | 合格 | 合格 | 合格 |

いずれも柱連続率は1.0。#209で必須帯を含む装飾予算と局所的な凹凸配置、角形塔の面計画、配色候補の探索を修正した。
patternsの様式データとCriticの算式・閾値は変更していない。
固定課題16組の合格と、core 7の正順・逆順での配置・指標・構造化診断の一致も維持している。
ここでの合格は測定した9ケースの結果であり、未測定の入力まで合格とみなさない。

### Cと同条件の性能比較

以下のD性能記録は#209適用前の測定であり、今回再測定せず値を保持している。

[D性能記録](../evaluation/records/structure-detail-performance.json)は、Cの
`benchmarks/make-baseline.json` と同じ住宅・塔のtiny/medium/large、1.21.11、schem、seed 42を
各3回、独立子プロセスで測ったもの。size・要素寸法の作り方、OS最大RSSと端末情報の取得は
既存 `scripts/benchmark.py` の手順を再利用する。CPU・メモリ総量・Python版はCと同じ。
C入力はWindowsのCRLF、D入力はLFなので `input_identical_to_c`（byte一致）はfalseである。
保存したD入力をCRLFで直列化すると全6条件のCの入力hashに一致することをテストで照合する。

時間は秒、RSSはMiB。いずれも3回の中央値で、差はD−C。個々の試行と段開始時の瞬間値はJSONに残す。

| 対象 | C時間 | D時間 | 時間差 | DピークRSS | RSS差 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 住宅 tiny | 1.247 | 2.036 | +0.789 | 79.6 | +4.7 |
| 塔 tiny | 1.248 | 1.971 | +0.723 | 80.8 | +4.1 |
| 住宅 medium | 29.682 | 68.904 | +39.222 | 193.9 | +11.2 |
| 塔 medium | 21.374 | 144.405 | +123.031 | 194.3 | +32.5 |
| 住宅 large | 183.595 | 290.553 | +106.958 | 203.0 | +9.8 |
| 塔 large | 155.736 | 1019.377 | +863.641 | 265.7 | +88.2 |

mediumの10秒・largeの120秒というNFR2の時間目標は4条件すべて未達。
性能用tinyは全試行の終了コード0、medium/largeは全試行3（品質FAIL）で、
後者も成果物を書き出すまで測定している。測定の完了と品質合格を区別する。

時間差とRSS差は各条件の中央値の差であり、負荷を統制した因果比較ではない。
C以後に追加されたscene出力・構造・装飾・Criticもmake時間に含み、workerのimport量も異なる。
依存版の同一性は未確認である。
並行作業のCPU・メモリ競合は制御していない。CのNFR2未達をD固定品質の合格で上書きしない。
今回の建物のRSSから、未測定の巨大地形を含むNFR3全体やNFR1想定端末の達成を推定しない。
Minecraft実画面、人間評価、jar取得、モデル呼出し・トークン比較は実施していない。

### 再実行

Python 3.12のタスク専用venvへ `pip install -e ".[dev]" hatchling` を行った上で実行する。
コマンドは環境の `run_logged.py` などを介して記録する。

```text
python scripts/evaluate_structure.py --matrix --performance
python scripts/evaluate_structure_partition.py
ruff check .
pytest
mcblueprint check --strict
python -m pip wheel --no-build-isolation --no-deps --wheel-dir output/issue-180/wheel .
git diff --check
```

`--freeze` は入力と閾値を最初に固定する専用操作で、既存計画があれば上書きを拒否する。
通常の再実行では指定しない。16入力のhashが一致しなければmatrixは停止する。
品質FAIL（終了コード3）は成果物・指標を保存し、構造・I/Oエラーを品質合格へ読み替えない。
#209では `--matrix` と `evaluate_structure_partition.py` だけを実行して品質記録を更新し、`--performance` は実行していない。
作成wheelは別のクリーンvenvへ導入し、ソースの探索パスに頼らず4版blockdataとスキーマのlock hash、
3様式とBlueprintスキーマの存在を確認し、wheel内patternsの絶対パスを指定してcheck --strictを実行する。
クリーンな作業ディレクトリでパスなしのcheckは探索対象がなく失敗するため、同梱先を明示する。

初回（#180）記録時のローカル検証はruff成功、pytestは2371成功・1skip（任意のtiktoken未導入）。
性能記録の1テストを測定完了後に分離し、残る全件と合わせて実行した。
リポジトリのstrict検証は22ファイル、クリーンwheelのpatterns指定は19ファイルで、どちらもエラー・警告0。
wheelビルド、4版blockdataとスキーマのlock hash照合、3様式・Blueprintスキーマの存在確認、
変更26ファイルのUTF-8 BOMなし／LF確認、測定方式・計画hashの照合、git diff --checkにも成功した。

## makeの性能修正の初回検証（Issue #210、3261ea9）

#209適用済みの `3261ea9` を修正前として、Dの固定16入力を変更せずに再実行した。
[make-performance-210.json](../evaluation/records/make-performance-210.json) の
`fixed-before` と `fixed-after` に入力hashと4種の結果hashを保存する。
世界座標と完全なブロック状態を整列した配置、全指標、構造化診断、展開後scene JSONが
16組すべてで一致した。構造化診断は配列の順序も含めて比較し、除外や正規化で差を隠さない。
各組の終了コードは修正前後とも0だった。

sceneには入力パスが含まれるため、両実行で同じ入力パス・byteを使う。
run記録とscene sidecarに含むengine世代・出力パスは実装修正や保存先の違いで変わるので、
これらのファイル全体のhashは不変性の判定に使わない。
配置hashには `.schem`／`.litematic` の時刻情報や圧縮byteを含めない。
性能用のmedium／largeも同じ4種のhashを比較し、反復間の不一致も検出する。
住宅・塔のmedium／large 4入力を各3回測定し、12試行すべてで修正前の結果と一致した。
固定16入力と合わせて20入力で不変性を確認した。
LF統一後も固定16入力を再生成し、計測前と同じ4種のhashで一致した。
結果は `publication_fixed_cases` に分離し、計測時の記録を置き換えない。
手順と時間境界は [PERFORMANCE.md](PERFORMANCE.md) を参照する。

性能修正はMassingの再利用、開口boundsの再利用、既存Palette表の再利用に限る。
公開API、保存形式、乱数系列、Criticの算式・閾値は変更していない。
追加試験では通常生成／Blueprintからの再開でscene用の再計画を防ぎ、
状態の異なる階段とfullブロックが同一Paletteへ誤って統合されないことを確認する。
NFR2の達成、未測定入力の品質、人間の外観評価、実画面確認はこの一致確認から推定しない。

ローカル検証はPython 3.12.15でruff成功、pytest全件2474成功・1skip
（任意のtiktoken未導入）、check --strictは22ファイルでエラー・警告0、wheelビルド成功。
git diff --checkと変更ファイルのUTF-8 BOMなし／LFも確認した。

## 最新developに対する性能修正の不変性（Issue #210、fc8fbf9）

#214・#215のmerge後は、基準を `fc8fbf9` 単体へ切り替えた。
[最新記録](../evaluation/records/make-performance-210-develop-fc8fbf9.json) の
`fixed-before` は全Git blobを照合したdevelopコード、`fixed-after` は統合実装の実行である。
固定16件で配置・指標・構造化診断・sceneのhash一致を確認した。
性能用4件も各3回の全12試行でdevelop単体と一致し、計20入力の不変性を確認した。
性能用の全試行は終了コード0だった。
sceneの入力絶対パスも揃えており、実装の読み込み先は各workerの実パスで検証する。

develop側の外壁輪郭、被覆と最外深度の分離、回転に依存しない評価窓、開口後退、
収容不足の修正先を保持する。Criticの実装はfc8fbf9と同一で、#210は既存結果を再利用して
重複計算を省く。性能と再実行手順は [PERFORMANCE.md](PERFORMANCE.md) を参照する。

merge後のローカル検証はruff成功、pytest全件2550成功・1skip（任意のtiktoken未導入）・317警告、
strict検証22ファイルでエラー・警告0、wheelビルド成功。
pytestの警告には、開口後退が予約領域と重なる場合に後退量を0とする#215の診断が含まれる。

## H01 地形・大規模の固定入力と基準測定

[large-scale.json](../evaluation/cases/large-scale.json) はHの12条件（山岳/島嶼の起伏・侵食の4通りを含む）をmedium/huge、seed 42/73/101で凍結し、72個のBuildSpec入力hashを持つ。これは実装前の期待条件である。起点 `dabb0dc` では対象archetypeが未実装のため、生成結果や品質合格を記録しない。後続Issueでは全4版・両出力形式・BuildSpec/emit Blueprintの接続を評価し、core分割・逆順・resumeの一致と指定負例を確認する。

住宅・塔のmedium/large 4対照入力の現行時間・段別RSS・OSピークRSS・出力状態は [h-baseline.json](../evaluation/records/h-baseline.json) を参照する。既存住宅archetypeはhuge要求寸法に収まらないため、hugeは未実装H課題の期待入力としてのみ保持する。端末仕様と測定境界は [PERFORMANCE.md](PERFORMANCE.md) に記録する。対象端末がNFR1相当ではないため、この記録は性能の基準証跡であり、NFR2/3達成判定ではない。
