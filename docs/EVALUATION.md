# 建築品質評価

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
