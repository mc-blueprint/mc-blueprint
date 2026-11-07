# #243 反証指摘への対応

詳細の生値・指紋・入力hash・テスト上位30件は[JSON](region-performance-243-review.json)。前回の生成全体の主測定は[元記録](region-performance-243.md)として保全する。

## 変更

- make/buildからNBT出力予算を渡し、保持する出典表・spoolパレット・行キャッシュも差し引く。
- Litematicの全セルuint32添字・packed配列・NBT複製・一時領域と、SchemのVarInt列等を確保前に検査する。
- 400×250×400・128MiBは、160,000,000 byteの添字確保に進まずBlueprintError。既存出力も保持する。
- 測定結果は各entryの実装・測定器・基準commit・Python指紋と入力hashを照合し、相違や指紋欠落は測定前に拒否する。

## 変更した出力段の独立3反復

8,388,608セルの完成spoolを入力にし、生成処理は計時へ含めない。後側は出力用128MiB上限を指定。各workerは独立プロセスで、ロード元をassert検査する。時刻固定の展開NBT hashは全試行・前後で一致。

| 形式 | 出力中央値 前→後（秒） | 全試行最大RSS 前→後（MiB） |
|---|---:|---:|
| schem | 0.4630 → 0.3976 | 87.7 → 86.5 |
| litematic | 0.5236 → 0.4972 | 106.7 → 106.9 |

## 検証

- 住宅・塔・接合・城郭・街並み・洞窟・地下街の4版×両形式56ケースで4種hashと入力hashが一致。
- 全件pytest: 3194 passed、1 skipped、6653.49秒。
- ruff check .、mcblueprint check --strict、wheel作成、git diff --checkに成功。
- skipは任意のtiktoken未導入による既存のskillトークン数テスト。

## 測定記録の扱い

旧entryに指紋がない場合も再利用しない。今回の結果は旧記録と分離し、旧beforeは比較基準としてのみ参照する。NBT出力内容を変える最適化は追加していない。
