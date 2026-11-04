# 矩形敷地と基礎の造成

任意のトップレベル項目 `foundation` は、元地形の明示位置を平坦に造成する。
`terrain` が必須で、候補地探索や地表に沿う建物の変形は行わない。
検査と計画は元snapshotを読み、通常Operationの前に一度だけ適用する。

```json
"foundation": {
  "from": [-3, 2, -4], "to": [3, 2, 3],
  "scan": {"minY": -4, "maxY": 15},
  "maxCut": 3, "maxFill": 3, "maxSupportDepth": 3,
  "ground": ["stone", "dirt", "grass_block"],
  "block": "cobblestone"
}
```

すべての項目が必須。`from` / `to` は同じYの整数Vec3で、X/Zは両端を含む矩形
footprint（順不同）、Yは床ブロック層Fを示す。入口下段・立つ足元はF+1。
座標はterrainと同じ元ファイルの貼付基準に対するBlueprint座標。
`scan.minY` / `maxY` は地表と支持を検査する上下の境界で、両端を含みF-1..F+1を含める。
探索帯全体がterrainの施工・取得範囲内にあり、施工矩形全体が保護矩形と交差しない必要がある。

## 対応する地盤と探索範囲

全footprint列を、探索下端から上端まで検査する。下端は明示された `ground`、
上端は通常の `minecraft:air` が必要。地盤が下から連続し、その上がairだけの単純な列を扱う。
最高の地盤セルYを地表Sとする。探索帯内の空洞、途中の別の浮いた地盤、水、溶岩、
砂・砂利、植生、指定していない材質、未取得セルは座標と原因付きで拒否する。
`cave_air` / `void_air` も通常airとして推測しない。
上端がairでも、探索上端より上まで無障害であるとは保証しない。下端より下の空洞や支持も検査しない。
建物の高さや頭上空間が必要なら、その領域を別途取得・確認する。

`ground` は対象地盤材IDを明示した配列。対応するのは、形状・落下・支持を推測せず
扱える次の通常完全ブロックだけ（`minecraft:` 省略可、プロパティ指定不可）：

`stone`, `granite`, `diorite`, `andesite`, `deepslate`, `cobbled_deepslate`,
`tuff`, `calcite`, `dirt`, `coarse_dirt`, `rooted_dirt`, `grass_block`,
`bedrock`, `cobblestone`, `stone_bricks`, `sandstone`, `red_sandstone`。

基礎材 `block` もこの一覧の単一IDとする。Paletteや不安定材の自己申告は扱わない。
元セルの明示プロパティはterrainの版別検証を受けた上で、同じIDなら地盤と認める。

## 深さと変更するセル

列ごとに次を算出し、等しい値は許容、上限超過を拒否する。

| 値 | 定義 |
| --- | --- |
| 必要切土深さ | `max(0, S - F)`。`maxCut` は0以上 |
| 必要盛土深さ | `max(0, F - S)`。床層も含む。`maxFill` は0以上 |
| 必要支持深さ | `max(1, F - S)`。床から最初の元地盤アンカーまでの距離。`maxSupportDepth` は1以上 |

F=10、S=8なら盛土深さ2・支持深さ2で、Y=9を支持層、Y=10を床にする。
S=12なら切土深さ2でY=11,12をairにし、Y=10を床に置換する。
SがF以上なら支持アンカーはF-1（距離1）、SがF未満ならアンカーはS。
アンカーから下端までの地盤も連続していることを探索帯内で確認する。

すべての列が通るまでsnapshotや既存volumeへ書き込まない。一列でも失敗すれば計画を返さず、
CLIは検証エラーで終了し出力ファイルを更新しない。成功時は切土をair、盛土と床を `block` にする。
切土しない下部の元地盤は保持する。後続Operationは造成後volumeへ通常どおり適用し、
床や建物を凹凸へ合わせて変形しない。

## 集計とAPI

`validate` / `build` は指定時だけ `Foundation: {...}` を表示する。
床Y、入口足元Y、必要深さの最大値、造成時の実変更セル数を報告する。
拒否時は全footprint列を調べ、各不適合列の最初の拒否座標・原因と、
算出できた必要深さを返す。入力・参照・造成条件の失敗は検証エラー（終了コード1）。

- `FoundationSpec.from_dict(raw)` はSchema検証済み項目を変換する。対応外指定は `BlueprintError`。
- `plan_foundation(spec, terrain) -> FoundationPlan` は元snapshotだけを読む。
  失敗は `FoundationError.issues`（座標 `position`、原因 `reason`、算出可能なら
  `required_cut` / `required_fill` / `required_support_depth`）。途中変更は残さない。
- `FoundationPlan.writes` は重複のない `(Vec3, BlockState)` の不変tuple。
  座標はBlueprint座標、状態は指定の基礎材または通常air。元と同じ状態のセルは含まない。
  make用snapshotの場合は同じセル列を反復できる内部の列区間計画を使い、全writesを
  ボクセルtupleとして常駐させない。領域窓と交差する区間だけを展開する。
  z→x→y順で、全列の検査後に一括適用する。
- `FoundationPlan.result` は不変の `FoundationResult`。`footprint`、`floor_y`、`entrance_y` と
  深さの最大値を持つ。`to_json()` でCLIと同じ辞書を返す。
- `cut_cells` は床より上をairへ変更した数、`fill_cells` は床より下のairを基礎材へ変更した数、
  `floor_cells` は床層を変更した数（元地盤から基礎材への置換も含む）。互いに重複せず、
  `changed_cells` は3つの和で `len(writes)` と一致する。
- `validate_and_load()` は計画を一度だけ作り、`Blueprint.foundation` に保持する。
  `generate_with_report(blueprint, seed=...)` は通常生成と同じvolumeに造成resultを添えて返す。
  指定なしのresultは `None`。既存 `generate()` の戻り値はvolumeのまま。

集計と平坦性は造成段階だけを表し、後続Operationによる変更は含まない。
接続路などの後続処理は、元snapshotへ `writes` を適用した状態と `entrance_y` を使う。
sourceの再読込や旧成果物への重ね掛けは行わない。実機適用・任意mask・部分再生成は対象外。
