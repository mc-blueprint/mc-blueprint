# Operation 早見表

正本は `docs/OPERATIONS.md`。配置系 Operation の材質指定は、各表に記載したキーでブロックまたは Palette を指定する。基本 Operation は `block` / `palette` のどちらか一方と、任意の `comment` を取る。
`mode` は `"hollow"`（中空、既定）か `"solid"`（充填）。

## 基本

| type | 必須 | 任意 | 生成されるもの |
|---|---|---|---|
| `set` | `position` | | 1 ブロック |
| `fill` | `from`, `to` | | 直方体（充填） |
| `box` | `from`, `to` | `mode` | 直方体。`hollow` は 6 面 1 ブロック厚の殻 |
| `wall` | `from`, `to`（同じ y）, `height` | `thickness`(1) | `from`/`to` が X または Z を共有すれば直線の壁、そうでなければ矩形の外周壁。y から上へ `height` 段 |
| `floor` | `from`, `to`（同じ y） | | 1 段の平面。天井にも使う |
| `line` | `from`, `to` | | 2 点を結ぶ線（斜めも可） |
| `landform` | `shape`, `from`, `to`, `height`, `surface`, `subsurface` | plateau は `edgeWidth` | 楕円形の丘または縁を持つ矩形台地。詳細は [OPERATIONS.md](../../../../docs/OPERATIONS.md#landform) |

## 図形

| type | 必須 | 任意 | 生成されるもの |
|---|---|---|---|
| `circle` | `center`, `radius` | `axis`(`y`), `mode` | `axis` に垂直な円。`hollow` はリング、`solid` は円盤 |
| `cylinder` | `center`（底面中心）, `radius`, `height` | `axis`(`y`), `mode` | 円柱。`hollow` は側面のみ（底・天井なし） |
| `sphere` | `center`, `radius` | `mode` | 球。`hollow` は球殻 |

- `hollow` の円・球は常に 1 ブロック厚で隙間がない。
- 直径 D の塔は `radius = (D - 1) / 2`（奇数直径）。偶数直径は作れないので 1 大きい奇数にする。

## 構造（ネスト）

`block` / `palette` は持たず、`operations` に任意の Operation を入れる（深さ 8 まで）。

| type | 必須 | 任意 | 動作 |
|---|---|---|---|
| `mirror` | `axis`, `at`, `operations` | `keepOriginal`(true) | `operations` を実行し、`axis = at` の平面で鏡像化したものも実行。`at` は `.5` 可（例 `4.5` で 4 と 5 の間）。`facing` / 接続 / `shape` / `hinge` は自動で反転 |
| `repeat` | `count`(1〜512), `offset`, `operations` | | `operations` を `count` 回、`offset × i` ずつずらして実行 |
| `translate` | `offset`, `operations` | `rotation`(0/90/180/270、既定0) | 原点まわりの回転後に `offset` だけ平行移動し、1段の入れ子で両変換を実行 |
| `rotate` | `angle`(90/180/270), `center`, `operations` | | `operations` を `center` を通る鉛直軸まわりに時計回りで回転して実行。`facing` / `axis` / 接続も回る |

```json
{
  "type": "repeat",
  "count": 4,
  "offset": [0, 5, 0],
  "operations": [
    { "type": "floor", "from": [-4, 1, -4], "to": [4, 1, 4], "block": "oak_planks" },
    { "type": "set", "position": [5, 3, 0], "block": "glass_pane[north=true,south=true]" }
  ]
}
```

```json
{
  "type": "mirror",
  "axis": "z",
  "at": 4,
  "operations": [
    { "type": "repeat", "count": 4, "offset": [0, 1, 1], "operations": [
      { "type": "fill", "from": [-1, 5, -1], "to": [11, 5, -1], "block": "dark_oak_stairs[facing=south]" }
    ] }
  ]
}
```

（北側の斜面を 4 段作り、`z = 4` で南側へ鏡像 → 切妻屋根）

## 編集（配置済みの結果を読んで動作。順序が重要）

| type | 必須 | 動作 |
|---|---|---|
| `replace` | `from`, `to`, `match`（ブロック文字列または配列）, `block` / `palette` | 範囲内で `match` に一致するブロックを置き換える。`match` にプロパティを書かなければ ID だけで一致。未設定セルは `air` として扱う |
| `copy` | `from`, `to`, `offset` | 範囲の現在の内容を `offset` だけずらした位置へ複製（配置済みセルのみ、向きはそのまま） |

```json
{ "type": "replace", "from": [0, 1, 0], "to": [10, 5, 8], "match": "white_terracotta", "palette": "plaster" }
```

## 建築部品（内部で基本 Operation に展開。階段系は頭上空間と到着口を自動で空ける）

| type | 必須 | 任意 | 動作 |
|---|---|---|---|
| `stairs` | `start`（1 段目 = 下の階の床の 1 つ上）, `direction`, `height`（段数 = 登る高さ）, `block` / `palette` | `width`(1), `headroom`(3), `base` | 直進階段。各段の上 `headroom` ブロックと到着口を空気にする。最後の段は上の階の床と同じ高さ |
| `spiral_stairs` | `center`, `radius`(1〜8), `height`, `block` / `palette` | `turn`(clockwise), `headroom`(3), `column` | 螺旋階段。1 周で半径 1: 8 段、半径 2: 12 段、半径 3: 16 段 |
| `roof` | `from`, `to`（軒の高さの矩形 = 壁の外周）, `block` / `palette` | `style`(gable / hip), `ridge`(x / z), `overhang`(1), `gable`（妻壁のブロック）, `ridgeBlock` | 階段ブロックで屋根。`*_stairs` なら向きと寄棟の角の `shape` は自動 |
| `pillar` | `position`, `height`, `block` / `palette` | `base`, `cap` | 柱 |
| `doorway` | `position`（開口の左下）, `facing`（ドアの facing。北の壁なら `south`） | `width`(1), `height`(2), `door`（`oak_door` など）, `arch`（`{ "style", "block", "trim" }`） | 開口を空けてドアを 2 段置く。幅 2 で両開き。`arch` で頭上をアーチにする。`depth` で厚い壁を貫通 |
| `window` | `position`, `axis`（壁の向き x / z） | `width`(1), `height`(1), `block`(glass_pane), `arch` | 接続プロパティ付きのガラス板を置く。`arch` でアーチ窓 |
| `arch` | `position`（開口の左下）, `width`, `height`（開口の高さ。頂点まで）, `block` / `palette` | `axis`(x), `style`(round / pointed / flat), `depth`(1), `thickness`(1。2 で外側も滑らか), `trim`（段差に置く階段。縁と同じ素材の `*_stairs`）, `fill`（開口を埋めるブロック）, `hollow`(true) | 開口の周りに縁を作り開口を空ける。`trim` で縁の段差が階段になる（開口側は逆さ階段、`thickness: 2` なら外側も通常の階段）。`round` は幅 3 で高さ 2 以上、幅 5 で 3 以上、幅 7 で 4 以上。`pointed` は幅 3 で 3 以上、幅 5 で 5 以上 |
| `room` | `from`, `to`（外寸。`from.y` = 床の層、`to.y` = 天井の層）, `wall`（ブロックか `{ "palette" }`） | `floor`, `ceiling`, `corners`（四隅の柱）, `thickness`(1), `interior`(true), `doors`（`[{ side, offset, width, height, door, arch }]`）, `windows`（`[{ side, offset, width, height, sill(2), count, spacing(2), block, arch }]`） | 1 階分の床・壁・天井とドア・窓をまとめて作る。`offset` 省略で中央。ドア下段は自動で床の 1 つ上 |
| `tower` | `position`（1 階の床の中心）, `radius`（円形）または `size`（角形・奇数）, `height`（壁の高さ）, `wall` | `shape`(round / square), `floor`(= wall), `floors`（階の間隔）, `stairs`（`{ radius, block, turn, column }` / false）, `battlement`（`{ block, spacing(1) }`）, `windows`（`{ sides, sill(2), width, height, block, arch }`）, `door`（`{ side, block, arch }`） | 塔を一括で作る。屋上は `position.y + height + 1`（外壁より 1 張り出す）。螺旋階段が各階の床を抜く。屋根は別途載せる |
| `bridge` | `from`, `to`（路面の中心線の両端。同じ y、x か z が一致）, `deck` | `width`(3), `style`(flat / arch), `rise`（arch の盛り上がり）, `stairs`（斜面の階段）, `railing`（欄干。`*_wall` / `*_fence` は接続自動）, `railingHeight`(1), `piers`（`{ spacing, bottom, block }`） | 橋。路面の上 2 ブロックを空けるので必ず通れる。欄干は路面の両端の列（通路は `width − 2`） |
| `gate` | `position`（通路の床の中心、正面）, `width`, `height`（通路の直線部分）, `block` / `palette` | `axis`(x), `depth`(3), `jamb`(2), `top`(1), `arch`（`{ style, block, trim, thickness }`）, `battlement`, `portcullis`（`{ block, height }`）, `door`, `towers`（`{ size(7), height, windows, ... }`） | 城門。歩廊の床は `y + height + rise + 1 + top`（城壁の高さをこれに合わせる）。塔は歩廊の高さに床を張り出入口を空ける |
| `garden` | `from`, `to`（地面の層の矩形） | `style`(farm / flowers), `crop`(wheat[age=7]), `water`(4), `rows`, `ground`(grass_block), `plants`（flowers で必須。Palette に air を混ぜる）, `fence`, `gate`（`{ side, offset, block }`）, `lanterns`（`{ spacing, block }`） | 畑（湿った耕地 + 作物 + 水路）か花壇。柵は最外周の 1 段上で、内側が畑になる |
| `path` | `points`（軸平行の経由点。路面の層）, `block` / `palette` | `width`(2), `edge`（縁石）, `stairs`（段差の階段）, `clearance`(2), `lights`（`{ spacing, component }` / `{ spacing, block }`） | 道。y の差は区間内に均等な段差で結ぶ（区間の長さ以下）。段差の下は埋めないので地形に段を足す |

```json
{ "type": "stairs", "start": [2, 1, 1], "direction": "south", "height": 4, "block": "oak_stairs", "base": "oak_planks" }
```

```json
{ "type": "roof", "from": [0, 5, 0], "to": [10, 5, 8], "block": "dark_oak_stairs", "gable": "spruce_planks" }
```

```json
{ "type": "doorway", "position": [5, 1, 0], "facing": "south", "width": 3, "height": 3, "arch": { "style": "round", "block": "stone_bricks", "trim": "stone_brick_stairs" } }
```

```json
{ "type": "room", "from": [0, 0, 0], "to": [10, 5, 8], "wall": { "palette": "plaster" }, "floor": "stone_bricks", "corners": "oak_log",
  "doors": [ { "side": "north", "door": "oak_door" } ],
  "windows": [ { "side": "north", "count": 2, "spacing": 5, "height": 2 }, { "side": "south", "count": 3, "height": 2 } ] }
```

```json
{ "type": "tower", "position": [0, 0, 0], "radius": 6, "height": 20, "floors": 5, "wall": { "palette": "stone_wall" }, "floor": "spruce_planks",
  "battlement": { "spacing": 1 }, "windows": { "height": 2 }, "door": { "side": "north", "block": "spruce_door" } }
```

## 部品（`components/<name>.json`）

| type | 必須 | 任意 | 動作 |
|---|---|---|---|
| `component` | `name`（利用者部品または同梱モジュールの参照名）, `position` | `rotation`(0 / 90 / 180 / 270) | 部品の原点を `position` に置いて配置。向きは自動で回る |

単純名は利用者の `components/<name>.json`、`medieval.window` のようなドット名は同梱モジュールから解決する。利用者部品のサブディレクトリやドット付きファイルで同梱品を上書きできない。部品データ内の `name` は表示名で、日本語も使える。詳細は `docs/FORMAT.md` §11・`docs/PATTERNS.md` を参照。

同梱部品は `components/README.md` を見る（`lantern_post`, `medieval_window`, `arched_gate` など）。同じ部品を何度も使うときや、依頼者が部品を用意しているときに使う。新しい部品を作るときは `components/<name>.json` に Blueprint と同じ書式で書く（`minecraftVersion` / `origin` / `size` / `seed` は不要、座標は `[0,0,0]` 基準）。

```json
{ "type": "component", "name": "medieval_window", "position": [1, 2, 0] }
```

## よくある組み合わせ

| 作りたいもの | 書き方 |
|---|---|
| 部屋（外壁 + 床 + 天井 + ドア・窓） | `room` 1 つ（`doors` / `windows` で開口）。装飾のない箱なら `box`（hollow） |
| 家 | `room` + `roof`（`roof` の `from` / `to` は `room` の `to.y` の高さ）。`examples/cottage.json` |
| 上の階へ行く階段 | `stairs`（直進）または `spiral_stairs`。頭上と到着口は自動 |
| 切妻屋根 | `roof`（`gable` に妻壁のブロック） |
| 中身が空の建物 | `fill` で全体 → `fill` で内側を `air` |
| 窓を等間隔に並べる | `repeat` の中に `set`（`air` またはガラス） |
| 左右対称の建物 | 片側を書いて `mirror` |
| 四方に同じ部品（塔の 4 隅など） | 部品を 1 つ書き、`rotate` 90 / 180 / 270 で残り 3 つ |
| 同じ階を積む | 1 階を作って `copy` で `offset: [0, 階高, 0]` |
| 壁に質感を後付け | 壁を単色で作って `replace` で Palette に置き換え |
| 塔 | `tower`（外壁・床・螺旋階段・胸壁・窓・入口）。屋根は `sphere`（hollow の上半分）や `cylinder` を屋上に載せる。`examples/tower.json` |
| ドーム | `sphere`（hollow）→ 下半分を `fill` で `air` |
| 柱 | `wall` で `from == to`、または `fill` の 1 列 |
| 橋 | `bridge`（`style: arch` + `stairs` で太鼓橋、`piers` で川床まで橋脚）。`examples/bridge.json` |
| 城門 | `gate`（`towers` で両脇の塔、`portcullis` で落とし格子）。城壁は `fill` で歩廊の高さまで + 欄干。`examples/castle_gate.json` |
| 畑・花壇 | `garden`（`fence` + `gate` で囲む。花壇は `plants` に花と `air` の Palette）。`examples/farm.json` |
| 道・街灯 | `path`（`edge` に `cobblestone`、`lights` に `lantern_post` 部品）。`examples/village_path.json` |
| 出入口 | `doorway`（`door` を指定すればドア付き） |
| 窓 | `window`（接続は自動） |
| 門・アーチ窓 | `doorway` / `window` に `arch`。単独の縁だけなら `arch` |
| 回廊・橋脚 | `arch` を `repeat` で並べる（`depth` で奥行き） |

## building（固定配置の一〜二階建て）

`position`, `width`(≥5), `depth`(≥5), `wall`, `floor`, `roof`(`*_stairs`), `window`, `door`(`*_door`)を指定。任意は `floors`(1/2、既定1), `floorHeight`(≥4、既定4), `angle`(0/90/180/270、既定0)。二階では `stairs`(`*_stairs`)が必須で、奥行きは `floorHeight + 4` 以上。一階でstairsは省略可能。材質は単一ブロックのみ。roof/stairsにfacing/half/shapeを書かない。

床Yを基準に入口下段はY+1。北中央入口、東・南の各階窓、二階北窓、西内側の直進階段、棟軸z・軒1の切妻屋根を固定配置する。positionが回転原点。幅/奥行は回転前の外寸で、偶数幅の中央は小さい座標側を選ぶ。階段はheadroom3で、半段への昇降途中に身体が残る低い列の頭上も空ける。必要空間に収まらない寸法は拒否する。詳しい配置・材質の前提・Pythonのlayout参照は正本 `docs/OPERATIONS.md` のbuilding節を参照。


## join_buildings（二棟の共有壁・内部開口）

同床高・同階高・同階数（1+1/2+2）・壁厚1で接する二棟は `join_buildings` の `buildings` 配列に既存building記述を二つ入れ、配置後の外向き `faces` 二つと共有壁 `wall`、`openings`（1件以上、offset必須）を指定する。二棟を個別生成して後から壁をairで消す方法にしない。

offsetは実共有範囲の接線軸最小座標から数え、角を各1セル残す。floorは0始まり。主要入口・階段/到着予約域への干渉と異階数は拒否される。共有floor行は先頭棟のfloor材を使う。`joinRoof:true` は同幅・同棟軸・同軒高/段勾配/屋根材の全幅妻面接触だけを一続き屋根にする。二棟目180度回転などで主入口を外周に残す。T/L字・直交棟・異条件・三棟以上・床高差・ID参照接続は未対応。完全な入力例と配置/接続面APIの正本は `docs/OPERATIONS.md` のjoin_buildings節。
