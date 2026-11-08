# #261 NFR2の時間残差と回帰

比較元は`4e9ce5e`。Python 3.12のタスク専用venvで測定した。
同じ端末では#262・#263が並行し、負荷を固定できていない。
プロファイル・固定課題照合・通常計時の一部は自分の全件pytest等とも並行し、負荷は固定できていない。
基準端末での最終NFR判定は#244が担当する。ここで未達を達成扱いにしない。

生値・入力hash・実装hash・段開始時刻・RSS・4種hashは次の記録に保存した。

- [通常計時（各独立3反復）](region-performance-261.json)
- [固定56ケース（前後各1反復）](region-performance-261-matrix.json)
- [城郭medium/hugeのcProfile（通常計時と別実行）](region-performance-261-profile-final.json)

## 改善対象の選定

- 小バッチではMT19937の配列初期化の固定費が支配的だった。8セルの3回平均はNumPy 9.494ms、
  標準Random 0.044ms。一方、32,768セルではNumPy 121.322ms、標準Random 149.872ms。
  2,048セル以下だけ標準Randomへ切り替え、大きいバッチは既存の一括処理を維持した。
- scene出典は行ごとに同じ属性を再計算していた。領域内の属性添字ごとに一度だけ計算し、
  出典IDと衝突検出を維持した。キャッシュは領域ごとに破棄する。
- 標準ライブラリと既存NumPyだけを使い、追加依存・公開API・保存形式の変更はない。

## E2EとピークRSS

時間は独立子プロセス起動からworker JSON出力までの3試行中央値。hash検査は計時外。
RSSは各3試行の最大値。largeは128×96×128の参考系列であり、medium/hugeの閾値を当てない。

| ケース | 前→後（秒） | 削減率 | 最大RSS 前→後（MiB） | この端末の時間閾値との比較 |
|---|---:|---:|---:|---|
| tower-medium | 28.503 → 22.274 | 21.9% | 116.2 → 116.5 | 10秒超過（未達） |
| tower-large | 200.819 → 159.619 | 20.5% | 240.7 → 241.3 | 参考系列 |
| castle-medium-1.21.11-schem | 60.754 → 19.324 | 68.2% | 123.5 → 124.2 | 10秒超過（未達） |
| castle-huge-1.21.11-schem | 274.129 → 99.311 | 63.8% | 153.2 → 153.2 | 120秒以下 |
| town-medium-1.21.11-schem | 49.545 → 22.422 | 54.7% | 120.2 → 120.6 | 10秒超過（未達） |
| town-huge-1.21.11-schem | 249.136 → 104.493 | 58.1% | 168.4 → 167.7 | 120秒以下 |
| underground_city-huge-1.21.11-schem | 103.136 → 62.786 | 39.1% | 119.9 → 120.0 | 120秒以下 |

## 段別時間（中央値、秒）

GenerateにはWeatherとCriticの逐次consumeを含む。Critic段は最終集計・支持／通行検査を含む。
cProfileの累積時間は重複するため足し合わせない。

| ケース | Materialize 前→後 | Generate 前→後 | Critic 前→後 | Export 前→後 |
|---|---:|---:|---:|---:|
| tower-medium | 10.297 → 8.797 | 8.150 → 4.164 | 5.498 → 4.895 | 0.307 → 0.286 |
| tower-large | 38.258 → 38.746 | 83.311 → 41.576 | 63.977 → 63.871 | 1.634 → 1.570 |
| castle-medium-1.21.11-schem | 10.140 → 8.958 | 42.935 → 3.424 | 1.002 → 1.027 | 0.287 → 0.289 |
| castle-huge-1.21.11-schem | 13.361 → 13.021 | 221.609 → 47.683 | 22.810 → 23.859 | 7.285 → 7.297 |
| town-medium-1.21.11-schem | 12.305 → 13.308 | 31.234 → 2.784 | 1.102 → 1.119 | 0.296 → 0.303 |
| town-huge-1.21.11-schem | 23.612 → 23.161 | 188.978 → 46.272 | 22.685 → 21.391 | 6.280 → 6.394 |
| underground_city-huge-1.21.11-schem | 3.466 → 3.641 | 71.745 → 31.594 | 19.371 → 18.775 | 5.385 → 5.258 |

## cProfileと残る支配段

| ケース | 関数 | 累積時間 前→後（秒） | 呼出し回数 前→後 |
|---|---|---:|---:|
| castle-medium-1.21.11-schem | randomness.py:first_draws | 54.443 → 0.285 | 4568 → 4568 |
| castle-medium-1.21.11-schem | critic.py:consume | 1.310 → 1.485 | 1 → 1 |
| castle-medium-1.21.11-schem | support.py:check_support | 1.684 → 1.866 | 12 → 12 |
| castle-medium-1.21.11-schem | scene.py:_source_descriptor | 14.003 → 3.225 | 8325 → 1870 |
| castle-medium-1.21.11-schem | validator.py:validate_schema | 25.188 → 23.700 | 1 → 1 |
| castle-huge-1.21.11-schem | randomness.py:first_draws | 239.482 → 1.482 | 14527 → 14527 |
| castle-huge-1.21.11-schem | critic.py:consume | 100.016 → 90.211 | 16 → 16 |
| castle-huge-1.21.11-schem | support.py:check_support | 113.043 → 108.005 | 512 → 512 |
| castle-huge-1.21.11-schem | scene.py:_source_descriptor | 371.749 → 5.238 | 143036 → 2154 |
| castle-huge-1.21.11-schem | validator.py:validate_schema | 44.097 → 48.030 | 1 → 1 |

### 未達・参考ケースの残差

- tower-medium: Materialize 8.797秒、Critic 4.895秒、Generate 4.164秒。
- tower-large: Critic 63.871秒、Generate 41.576秒、Materialize 38.746秒。
- castle-medium-1.21.11-schem: Materialize 8.958秒、Generate 3.424秒、Resolve 2.035秒。
- town-medium-1.21.11-schem: Materialize 13.308秒、Generate 2.784秒、Resolve 1.468秒。

次の候補は、Operation型別のschema条件の探索範囲削減（エラー内容・順序を維持）、
支持検査でのパレット分類の再利用、Criticでのcore内の役割候補の一括抽出。
今回の変更には含めず、各候補を新たに計測してから採否を決める。

## 完成条件と検証

- 未達7ケース: 同条件・独立3反復、before→afterの交互実行を記録。
- 固定56ケース: 配置・指標・構造化診断・展開sceneの224 hashがすべて一致。終了コードも前後一致。
- 乱数: 空バッチ・先頭ゼロ・2,048/2,049セルの切替境界・32,768セルを標準Randomと完全一致で検証。
- scene: 行境界、出典のない属性、領域ごとに異なる属性添字を回帰検証。
- 全件pytest（指定どおり1回）: 3192 passed、
  1 skipped、failures=0、errors=12、6031.46秒。
- 12 errorsの原因は計測器への反復回数追加に対する共通テストfixtureの呼出し更新漏れ。
  呼出しを修正後、該当12件と不足反復再開の新規1件、計13件が成功。未解決のテストエラーはない。
  全件コマンドの初回終了結果を成功へ置き換えず、修正後は影響範囲だけを再検証した。
- ruff check .: 成功。mcblueprint check --strict: 25ファイル、エラー0・警告0。
- wheelビルド: 成功。git diff --check: 成功。
- 共有契約・保存形式・乱数列・診断は変更なし。#262/#263の担当ファイルは変更していない。
- 残作業: #244の基準端末判定と、公開担当によるcommit・push・通常PR作成。

- skip理由: tests.test_skill_sync.test_skill_token_limit: could not import 'tiktoken': No module named 'tiktoken'

## 測定時と最終ファイルの対応

測定時に変更ファイルへ残っていたCRLFを、測定完了後にLFへ統一した。
改行以外の内容とPython構文木は同一であり、全件pytestはこの同じ実装に対する結果。
測定時と最終時のraw hash、共通のLF正規化engine hash、各ファイルの対応を各JSONの`source_normalization`へ保存した。測定時のprovenanceは書き換えていない。
最終ファイルから再測定する場合は、通常のprovenance検査に従って新しい記録先を使う。

LF統一後、城郭medium・地下都市hugeを再実行し、測定時と同じ計8 hashを確認した。

測定完了後、afterの開始前にbeforeのprovenance・入力hashを照合する処理を追加した。
worker・計時境界・測定値と測定時の指紋は変更していない。更新後の測定器hashは各JSONの`post_measurement_script`に記録し、LF統一時点のhashとの対応を保持した。
