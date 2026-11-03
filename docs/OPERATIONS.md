# Operation 仕様（formatVersion 1）

Operation は Blueprint JSON の `operations` 配列に並べる建築の単位である。
共通項目（`type`, `block`, `palette`, `comment`）、座標系、適用順序、上限は [FORMAT.md](FORMAT.md) を参照。

本書が Operation の正本である。

## Materialize 前の役割入力

生成計画では、ブロック値を `{"role": "wall_main", "shape": "full", "state": {}}` として指定できる。
`mcblueprint.materialize.materialize()` が必要形状を先に収集し、具体ブロックと既存の重み付き Palette に変換する。
戻り値の `blueprint` を通常の検証器・loaderへ渡す。役割参照入りJSONを直接 `build` する形式ではない。

対象は基本配置の `block`、`room` の `wall/floor/ceiling/corners`、`building` の `wall/floor/roof/stairs`、
`roof` の `block/gable/ridgeBlock`、`tower` の `wall/floor/stairs.block/battlement.block`、
`stairs.base`、`spiral_stairs.column`、`arch` の `block/trim/fill`、`pillar` の `block/base/cap`。
開口の窓・ドアには従来の具体ブロック値を指定する。`building.roof` は `stairs` 形状を指定し、
棟用の `slab` も同一familyへ要求する。向き・上下・接続状態は各Operationが付与してから具体値へ解決する。
`building`・`roof` は基本配置へ展開するため、壁・屋根の混色は単一ブロックへ縮退しない。
展開時も位置・回転とセル属性を保持し、配置の役割参照をセルのrole属性へ渡す。

```python
from mcblueprint.materialize import materialize

result = materialize(
    plan,  # minecraftVersion・operationsなどを持つ計画JSON
    style="medieval", biome="plains", seed=42,
    palette=resolved.config["palette"],  # resolve_buildspec() の結果
)
concrete = result.blueprint
```

`palette.materials` は役割ごとに材質名、固定ブロックID、`{"material": "stone", "color": [0.6, 0, 0]}`、
または色のみのobjectを受け取る。色のみならその様式の当該役割の材質候補を使う。
材質名と同じ文字列（例 `stone`）は材質指定で、固定IDは `minecraft:stone` のように書く。
様式の役割・明示材質にない役割、必要形状の欠落、状態不整合は `BlueprintError`。
固定IDは選び直さず、他形状だけそのfamilyから引き、混色率は0にする。
固定指定の状態が不正なら、別形状しか要求されていなくても拒否する。
固定IDに比較面の色がない場合もそのIDを保持し、関係規則の判定に必要なら `color_unavailable` を診断する。
`floor/path/stairs` の役割と床・階段スロットは、既存traversalが扱える材質・状態に限定する。
塔はwall材で作る屋上床も判定対象に含め、`stairs.column` の柱は歩行材として制限しない。

候補色は用途別の面（床・道・屋根はtop、その他はside）の5分位点で比較する。
着色層だけにバイオーム色または固定色を線形sRGBで乗算し、未着色層があれば等分する。
`variation=0` は候補分布の中央に固定し、増加時は中央から分布全域へ目標色と再選択範囲を広げる。
同条件・seedで同じ結果になり、役割ごとの乱数はMaterialize段のseedから導出する。

Cの内部関係規則はwall_mainとroof/frameの明度差 `0.04 + 0.24 * contrast` 以上、
wall_mainとwall_secondaryの彩度差 `0.04 + 0.12 * saturation` 以下、両者が有彩色なら色相差60度以下。
`saturation` は目標色の彩度倍率 `0.5 + saturation` にも写し、`accent` はaccentの目標彩度と混色率、
plannerへ返す面積比上限 `0.08 * accent` に写す。これらはCの内部規則であり、受入評価の閾値ではない。
関係は選ばれた実ブロック色で最大20回評価し、違反規則に関わる非固定の役割だけを再選択する。
解けなければ違反数・違反量が最小の組合せと
`diagnostics`（規則・実測値・候補数・試行数）を返す。
`roles` は役割×必要形状の主ブロック表、`max_area_ratios` はplannerが守る面積比上限。
混色は主材と近色の副材の2材で、`mix` が副材の使用比率になる。
役割参照のない既存JSONは内容を変更しない。

## 目次

| 分類 | type | 概要 |
|---|---|---|
| 基本 | [`set`](#set) | 1 ブロックを配置 |
| 基本 | [`fill`](#fill) | 直方体を充填 |
| 基本 | [`box`](#box) | 直方体（中空 / 充填） |
| 基本 | [`wall`](#wall) | 壁（直線 / 矩形外周） |
| 基本 | [`floor`](#floor) | 1 段の床 |
| 基本 | [`line`](#line) | 2 点を結ぶ線 |
| 図形 | [`circle`](#circle) | 円（リング / 円盤） |
| 図形 | [`cylinder`](#cylinder) | 円柱（側面 / 充填） |
| 図形 | [`sphere`](#sphere) | 球（球殻 / 充填） |
| 構造 | [`mirror`](#mirror) | ネストした Operation を鏡像化 |
| 構造 | [`repeat`](#repeat) | ネストした Operation を繰り返し配置 |
| 構造 | [`translate`](#translate) | ネストした Operation を平行移動 |
| 構造 | [`rotate`](#rotate) | ネストした Operation を鉛直軸まわりに 90° 単位で回転 |
| 編集 | [`replace`](#replace) | 範囲内の一致するブロックを置き換え |
| 編集 | [`copy`](#copy) | 範囲の現在の内容を別の場所へ複製 |
| 建築 | [`stairs`](#stairs) | 直進階段（頭上空間と到着口を自動確保） |
| 建築 | [`spiral_stairs`](#spiral_stairs) | 螺旋階段 |
| 建築 | [`roof`](#roof) | 切妻・寄棟屋根 |
| 建築 | [`pillar`](#pillar) | 柱（台座・笠付き） |
| 建築 | [`doorway`](#doorway) | 出入口（開口 + ドア） |
| 建築 | [`window`](#window) | 窓（接続済みガラス板） |
| 建築 | [`arch`](#arch) | アーチ（半円 / 尖頭 / 平）と開口 |
| 建築 | [`building`](#building) | 矩形一〜二階建て（固定配置の入口・窓・直進階段・切妻屋根） |
| 二棟接続 | [`join_buildings`](#join_buildings) | 同床高・同階高・同階数の二棟に一重の共有壁と内部開口 |
| 建築 | [`room`](#room) | 1 階分の床・壁・天井とドア・窓の開口 |
| 建築 | [`tower`](#tower) | 塔（外壁・各階の床・螺旋階段・胸壁・窓・入口） |
| 建築 | [`bridge`](#bridge) | 橋（平橋 / 太鼓橋、欄干、橋脚） |
| 建築 | [`gate`](#gate) | 城門（アーチの通路、歩廊と胸壁、落とし格子、両脇の塔） |
| 建築 | [`garden`](#garden) | 畑（耕地・作物・水路）と花壇、柵と門 |
| 建築 | [`path`](#path) | 道（経由点をつなぐ路面、段差の階段、縁石、街灯） |
| 部品 | [`component`](#component) | 利用者部品または同梱パターンモジュールを配置 |

## 共通の記法

- `Pos` は `[x, y, z]` の整数配列。
- `from` / `to` は両端を含み、順序は問わない。以下では `min = 要素ごとの min(from, to)`, `max = 要素ごとの max(from, to)` とする。
- `mode` を持つ Operation は `"hollow"`（中空）と `"solid"`（充填）のいずれかで、既定値は `"hollow"`。
- 各 Operation は「生成するセル集合」を定義する。セルの並び（配置順）は Palette の乱数消費順に影響するため、本書で定めた順序に従う。
- 「bounds」は、Operation を実行せずに求められる、生成セルをすべて含む直方体である。検証（`size`、最大寸法）に使う。

---

## 基本 Operation

### set

1 ブロックを配置する。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `position` | Pos | ✓ | 配置位置 |

```json
{ "type": "set", "position": [3, 1, 0], "block": "minecraft:oak_door[facing=north,half=lower]" }
```

- bounds: `position` の 1 セル。

### fill

直方体の範囲をすべて充填する。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `from` | Pos | ✓ | 一方の角 |
| `to` | Pos | ✓ | もう一方の角 |

```json
{ "type": "fill", "from": [0, 0, 0], "to": [9, 3, 9], "block": "minecraft:air" }
```

- セル: `min ≤ (x, y, z) ≤ max` のすべて。配置順は y → z → x の昇順（y が最外ループ）。
- bounds: `min` .. `max`。

### box

直方体を生成する。既定では 6 面 1 ブロック厚の中空。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `from` | Pos | ✓ | | 一方の角 |
| `to` | Pos | ✓ | | もう一方の角 |
| `mode` | `"hollow"` / `"solid"` | | `"hollow"` | `hollow`: 外周の面のみ。`solid`: `fill` と同じ |

```json
{ "type": "box", "from": [0, 0, 0], "to": [9, 4, 9], "mode": "hollow", "palette": "stone_wall" }
```

- `hollow` のセル: `min ≤ p ≤ max` かつ、いずれかの軸で `p == min` または `p == max`。
- 辺の長さが 1 または 2 の軸があると、その方向の内部は存在しないため `solid` と同じ結果になる。
- 配置順は `fill` と同じ。
- bounds: `min` .. `max`。

### wall

壁を生成する。`from` と `to` は同じ高さで指定し、その高さから上へ `height` 段積む。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `from` | Pos | ✓ | | 一方の端（底面） |
| `to` | Pos | ✓ | | もう一方の端（底面）。`from[1] == to[1]` が必須 |
| `height` | integer ≥ 1 | ✓ | | 高さ（段数） |
| `thickness` | integer ≥ 1 | | `1` | 壁の厚さ |

形状は `from` / `to` の位置関係で決まる。

| 条件 | 形状 |
|---|---|
| `from.x == to.x` または `from.z == to.z` | **直線の壁**。`from`–`to` を結ぶ 1 列の底面を `height` 段積む |
| それ以外 | **矩形の外周壁**。`from`–`to` を対角とする矩形の外周 1 列を `height` 段積む（内部は空） |

`thickness` が 2 以上のときの拡張方向:

- 直線の壁（X 方向、`from.z == to.z`）: +Z 方向へ `thickness − 1` 列追加。
- 直線の壁（Z 方向、`from.x == to.x`）: +X 方向へ `thickness − 1` 列追加。
- 矩形の外周壁: 内側へ `thickness − 1` 列追加。厚さが矩形の半分を超える場合は内部がすべて埋まる。

```json
{ "type": "wall", "from": [0, 0, 0], "to": [9, 0, 9], "height": 4, "palette": "stone_wall", "comment": "外周壁" }
```

```json
{ "type": "wall", "from": [0, 0, 5], "to": [9, 0, 5], "height": 4, "block": "minecraft:oak_planks", "comment": "仕切り壁" }
```

- 直線かつ `from == to` の場合は 1 列の柱になる。
- 配置順は y → z → x の昇順。
- bounds: 底面の矩形（`thickness` 拡張を含む）を `height` 段分持ち上げた直方体。

### floor

1 段の床を生成する。`fill` の高さ 1 版で、床であることを明示するために用意している。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `from` | Pos | ✓ | 一方の角 |
| `to` | Pos | ✓ | もう一方の角。`from[1] == to[1]` が必須 |

```json
{ "type": "floor", "from": [1, 0, 1], "to": [8, 0, 8], "block": "minecraft:oak_planks" }
```

- 天井も `floor` で作る（高さを天井の y にする）。
- 配置順は z → x の昇順。
- bounds: `min` .. `max`。

### line

2 点間をブロックで結ぶ。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `from` | Pos | ✓ | 始点 |
| `to` | Pos | ✓ | 終点 |

```json
{ "type": "line", "from": [0, 0, 0], "to": [10, 6, 3], "block": "minecraft:oak_fence" }
```

- 生成規則（3D DDA）: `d = to − from`、`n = max(|d.x|, |d.y|, |d.z|)` として、`i = 0..n` について `from + round(d × i / n)` を配置する。`round(v)` は `floor(v + 0.5)`。`n == 0` のときは `from` の 1 セル。
- 両端を含む。`from` 側から順に配置する。
- bounds: `min` .. `max`。

---

## 図形 Operation

### 円・球の判定基準

すべての円・球は次の基準で統一する（WorldEdit の `//cyl` / `//hcyl` と同等）。

- **solid(r)**: 中心からの距離の 2 乗が `(r + 0.5)²` 未満のセル集合。
  - 円: `dx² + dz² < (r + 0.5)²`（`axis: "y"` の場合。他の軸では対応する 2 軸）
  - 球: `dx² + dy² + dz² < (r + 0.5)²`
- **hollow(r)**: `solid(r)` のうち、外側に接するセル（円では 4 近傍、球では 6 近傍のいずれかが `solid(r)` に含まれないセル）。常に 1 ブロック厚で、隙間のない（斜め方向を含めて連結した）リング・殻になる。`r == 1` のときは `solid(1)` から中心 1 セルを除いたもの。

`r` は 1 以上の整数。`dx, dy, dz` は中心からの整数オフセット。

半径 6 の例（`#` が hollow、`.` が solid のみ）:

```text
    #####
  ##.....##
 #.........#
 #.........#
#...........#
#...........#
#...........#
#...........#
#...........#
 #.........#
 #.........#
  ##.....##
    #####
```

### circle

円を生成する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `center` | Pos | ✓ | | 中心 |
| `radius` | integer ≥ 1 | ✓ | | 半径 |
| `axis` | `"x"` / `"y"` / `"z"` | | `"y"` | 円が乗る平面の法線。`"y"` なら水平な円 |
| `mode` | `"hollow"` / `"solid"` | | `"hollow"` | `hollow`: リング。`solid`: 円盤 |

```json
{ "type": "circle", "center": [0, 0, 0], "radius": 6, "mode": "solid", "block": "minecraft:stone_bricks" }
```

- `axis: "y"`: XZ 平面、`center.y` の高さ。`axis: "x"`: YZ 平面。`axis: "z"`: XY 平面。
- 配置順は、平面の 2 軸を（Y があれば Y を外側、なければ Z を外側）昇順に走査する。
- bounds: 平面上で `center ± radius`、法線方向は `center` の 1 段。

### cylinder

円柱を生成する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `center` | Pos | ✓ | | 底面の中心 |
| `radius` | integer ≥ 1 | ✓ | | 半径 |
| `height` | integer ≥ 1 | ✓ | | 高さ |
| `axis` | `"x"` / `"y"` / `"z"` | | `"y"` | 円柱の軸。`center` から軸の正方向へ `height` 段 |
| `mode` | `"hollow"` / `"solid"` | | `"hollow"` | `hollow`: 側面のみ（底・天井なし）。`solid`: 充填 |

```json
{ "type": "cylinder", "center": [0, 0, 0], "radius": 8, "height": 20, "mode": "hollow", "palette": "stone_wall" }
```

- `axis` 方向に `circle` を `height` 枚重ねたものと等価。底や天井が必要な場合は `circle` の `solid` を別に置く。
- 配置順は軸方向の昇順を外側とし、各段は `circle` と同じ。
- bounds: 平面上で `center ± radius`、軸方向は `center` から `height − 1` まで。

### sphere

球を生成する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `center` | Pos | ✓ | | 中心 |
| `radius` | integer ≥ 1 | ✓ | | 半径 |
| `mode` | `"hollow"` / `"solid"` | | `"hollow"` | `hollow`: 球殻。`solid`: 充填 |

```json
{ "type": "sphere", "center": [0, 20, 0], "radius": 6, "mode": "hollow", "block": "minecraft:deepslate_tiles" }
```

- ドーム（半球）が必要な場合は、`sphere` の後に `fill` で下半分を `minecraft:air` にする。
- 配置順は y → z → x の昇順。
- bounds: `center ± radius`。

---

## 構造 Operation

構造 Operation は `block` / `palette` を持たず、`operations` にネストした Operation を持つ。
ネストした Operation には任意の Operation（構造 Operation を含む）を置ける。ネスト深さの上限は 8（トップレベルを深さ 1 とする）。

ネストした Operation 内の Palette は、それぞれの配置で独立に乱数を消費する。したがって鏡像や繰り返しの各コピーで Palette の選択結果は異なる（結果は seed により再現可能）。

### mirror

ネストした Operation を実行し、さらに指定した平面で鏡像化したものも配置する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `axis` | `"x"` / `"y"` / `"z"` | ✓ | | 反転する軸 |
| `at` | number（整数または `.5`） | ✓ | | 鏡面の位置（`axis` 座標）。`.5` を指定するとブロックの境界を鏡面にできる |
| `keepOriginal` | boolean | | `true` | `true`: 元と鏡像の両方を配置。`false`: 鏡像のみ |
| `operations` | array | ✓ | | ネストした Operation（1 件以上） |

```json
{
  "type": "mirror",
  "axis": "x",
  "at": 0,
  "operations": [
    { "type": "wall", "from": [2, 0, -5], "to": [8, 0, -5], "height": 4, "palette": "stone_wall" }
  ]
}
```

- 座標変換: `axis: "x"` なら `x' = 2 × at − x`（y, z も同様）。`at` が `.5` のとき、`x = at − 0.5` のブロックは `x = at + 0.5` に写る。
- 実行順: `keepOriginal: true` のとき、まずネストした Operation を元の座標で先頭から順にすべて実行し、その後に鏡像として先頭から順にすべて実行する。
- ブロック状態の変換: 鏡像側では、向きを持つプロパティを次のとおり書き換える（[座標変換とブロック状態](#座標変換とブロック状態)）。

| `axis` | 変換 |
|---|---|
| `x` | `facing`: `east` ↔ `west`、接続プロパティ `east` ↔ `west` |
| `z` | `facing`: `north` ↔ `south`、接続プロパティ `north` ↔ `south` |
| `y` | `facing`: `up` ↔ `down`、`half`: `top` ↔ `bottom`、`type`: `top` ↔ `bottom`（スラブ） |
| 共通 | `shape`: `inner_left` ↔ `inner_right`、`outer_left` ↔ `outer_right`（階段）、`hinge`: `left` ↔ `right`（ドア） |

- 看板・旗の `rotation`（0〜15）は変換しない。
- bounds: ネストした Operation の bounds の合成と、その鏡像の合成の和（`keepOriginal: false` なら鏡像のみ）。

### repeat

ネストした Operation を一定間隔で繰り返し配置する。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `count` | integer 1〜512 | ✓ | 繰り返し回数（元の位置を含む） |
| `offset` | `[dx, dy, dz]` | ✓ | 1 回ごとの平行移動量 |
| `operations` | array | ✓ | ネストした Operation（1 件以上） |

```json
{
  "type": "repeat",
  "count": 4,
  "offset": [3, 0, 0],
  "operations": [
    { "type": "set", "position": [1, 2, 0], "block": "minecraft:glass_pane" },
    { "type": "set", "position": [1, 3, 0], "block": "minecraft:glass_pane" }
  ]
}
```

- `i = 0, 1, …, count − 1` について、ネストした Operation を `offset × i` だけ平行移動して先頭から順に実行する。
- `offset` が `[0, 0, 0]` でも検証エラーにはならないが、同じ位置に `count` 回上書きするだけになる。
- `mirror` を `repeat` の中に置いた場合、鏡面 (`at`) も一緒に平行移動する。
- bounds: `i = 0` と `i = count − 1` の bounds の和。

### translate

ネストした Operation を平行移動して配置する。`rotation`指定時は原点まわりの回転を先に適用し、1段の入れ子で両変換を行う。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `offset` | `[dx, dy, dz]` | ✓ | 移動量 |
| `rotation` | 0 / 90 / 180 / 270 | | 原点まわりの回転角（既定0） |
| `operations` | array | ✓ | ネストした Operation（1 件以上） |

```json
{ "type": "translate", "offset": [20, 0, 0], "operations": [ { "type": "box", "from": [0, 0, 0], "to": [4, 3, 4], "block": "stone" } ] }
```

- 同じ部品を離れた場所に置くときに使う。`repeat` の `count: 1` と同じだが意図が明確になる。
- bounds: ネストした Operation の bounds を回転・移動したもの。

### rotate

ネストした Operation を、`center` を通る鉛直軸まわりに回転して配置する。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `angle` | `90` / `180` / `270` | ✓ | 上から見て時計回りの角度 |
| `center` | Pos | ✓ | 回転軸が通る位置（`y` は使わない） |
| `operations` | array | ✓ | ネストした Operation（1 件以上） |

```json
{
  "type": "rotate",
  "angle": 90,
  "center": [0, 0, 0],
  "operations": [ { "type": "wall", "from": [1, 0, 0], "to": [5, 0, 0], "height": 3, "block": "stone" } ]
}
```

- 座標変換（90° 時計回り、`center = (cx, cz)`）: `x' = cx − (z − cz)`, `z' = cz + (x − cx)`。東 → 南 → 西 → 北の順に回る。
- 回転の中心はブロックの中心。偶数幅の構造を中心対称に回したい場合は `translate` と組み合わせる。
- ブロック状態は `facing`、`axis`、接続プロパティが回転に追従する（[座標変換とブロック状態](#座標変換とブロック状態)）。
- 4 方向に同じ部品を置くには、元 + `rotate` 90 / 180 / 270 の 4 つを並べる。
- bounds: ネストした Operation の bounds を回転したもの。

---

## 編集 Operation

編集 Operation は、その時点までに配置された結果（先行する Operation の出力）を読んで動作する。配列内での順序が結果を決める。

### replace

範囲内で、一致するブロックを別のブロックへ置き換える。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `from`, `to` | Pos | ✓ | 範囲（両端含む） |
| `match` | string または string の配列 | ✓ | 置き換え対象のブロック指定。プロパティを省略した場合は ID だけで一致、書いたプロパティは値まで一致が必要 |
| `block` / `palette` | | ✓（一方） | 置き換え後のブロック |

```json
{ "type": "replace", "from": [0, 0, 0], "to": [10, 8, 10], "match": "minecraft:stone_bricks", "palette": "stone_wall", "comment": "外壁に苔を混ぜる" }
```

- 範囲内の未設定セルは `minecraft:air` として扱う。`"match": "air"` で隙間を埋められる。
- `"match": "oak_stairs"` は向きに関係なくすべての樫の階段に一致し、`"match": "oak_stairs[facing=east]"` は東向きだけに一致する。
- Palette を指定した場合は置き換えるセルごとに乱数を消費する。
- bounds: `min` .. `max`。

### copy

範囲の現在の内容を、`offset` だけずらした位置へ複製する。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `from`, `to` | Pos | ✓ | コピー元の範囲（両端含む） |
| `offset` | `[dx, dy, dz]` | ✓ | コピー先への移動量 |

```json
{ "type": "copy", "from": [0, 0, 0], "to": [8, 4, 8], "offset": [0, 5, 0], "comment": "1 階をそのまま 2 階に" }
```

- コピーされるのは範囲内で配置済みのセルだけ。未設定セルはコピー先を上書きしない（明示的に置いた `minecraft:air` はコピーされる）。
- コピー元は実行時点で読み取ってから書き込むため、コピー先がコピー元と重なっていても連鎖しない。
- ブロック状態は変換しない（向きはそのまま）。回転・反転したコピーが必要なら、元を `rotate` / `mirror` の中に入れて 2 回書く。
- bounds: コピー元とコピー先の和。

---

## 高レベル建築 Operation

建築でよく使う部品を 1 つの Operation で書けるようにしたもの。内部で基本 Operation の列へ展開して実行するため、`mirror` / `repeat` / `rotate` の中でも使え、bounds も展開結果から求まる。

`block` に階段ブロック（`*_stairs`）を指定すると、`facing` / `half` を省略した場合に向きが自動で設定される（`stairs` / `spiral_stairs` / `roof`）。`palette` を使う場合は向きを自動設定できないので、Palette 内の階段ブロックに向きを書く。

### stairs

直進階段。各段の上に `headroom` 分の空気を確保し、最後の段の先（到着口）の頭上も空ける。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `start` | Pos | ✓ | | 1 段目のブロック（下の階の床の 1 つ上） |
| `direction` | `north` / `south` / `east` / `west` | ✓ | | 登る方向 |
| `height` | integer ≥ 1 | ✓ | | 段数 = 登る高さ。最後の段の上面が上の階の床の上面と同じ高さになる |
| `width` | integer ≥ 1 | | `1` | 幅。`direction` を向いて右側へ広がる |
| `headroom` | integer ≥ 2 | | `3` | 各段の上に確保する空気の高さ |
| `base` | ブロック | | | 段の下を埋めるブロック（省略時は空洞のまま） |
| `block` / `palette` | | ✓（一方） | | 段のブロック |

```json
{ "type": "stairs", "start": [2, 1, 1], "direction": "south", "height": 4, "block": "oak_stairs", "base": "oak_planks" }
```

- `i` 段目は `start + direction × i + (0, i, 0)`。段の上 `headroom` ブロックと、到着口（`start + direction × height` の高さ `start.y + height` から `headroom` ブロック）を `minecraft:air` にする。上の階の床（`y = start.y + height − 1`）は最後の `headroom` 段分だけ自動的に開く。
- 途中に壁や梁があっても頭上分は削られる。削りたくない場合は経路を変える。
- 折り返し階段は、`stairs` を 2 つと踊り場（`floor`）で組み合わせる。

### spiral_stairs

中心軸のまわりを 1 段ごとに 1 ブロック上がる螺旋階段。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `center` | Pos | ✓ | | 軸の位置（最下段の高さ） |
| `radius` | integer 1〜8 | ✓ | | 外周の半径 |
| `height` | integer ≥ 1 | ✓ | | 段数 = 登る高さ |
| `turn` | `clockwise` / `counterclockwise` | | `clockwise` | 上から見た回転方向。東 → 南 → 西 → 北が時計回り |
| `headroom` | integer ≥ 2 | | `3` | 各段の上に確保する空気の高さ |
| `column` | ブロック | | | 中心軸に立てるブロック |
| `block` / `palette` | | ✓（一方） | | 段のブロック |

```json
{ "type": "spiral_stairs", "center": [0, 0, 0], "radius": 2, "height": 15, "block": "stone_bricks", "column": "stone_bricks" }
```

- 段は半径 `radius` のリングのセルを角度順に 1 つずつ使う（東から開始）。踏面はそのセルと、角度が最も近い内側のセル（軸を除く）を合わせた扇形で、同じ (x, z) は 1 周に 1 回しか使われない（段が縦に重ならない）。
- 1 周に必要な段数はリングのセル数（半径 1: 8、半径 2: 12、半径 3: 16）。1 周でこの高さを登る。
- 階段ブロックを使うと進行方向を `facing` にするが、斜めに進む段では見た目が崩れやすい。通常ブロックまたはハーフブロックのほうが無難。

### roof

矩形の上に階段ブロックで屋根を架ける。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `from`, `to` | Pos | ✓ | | 軒の高さの矩形（壁の外周と同じ範囲。`from.y == to.y`） |
| `style` | `gable` / `hip` | | `gable` | 切妻 / 寄棟（正方形なら方形＝ピラミッド） |
| `ridge` | `x` / `z` | | 長い辺 | 切妻の棟の向き |
| `overhang` | integer ≥ 0 | | `1` | 軒の張り出し |
| `gable` | ブロック | | | 切妻の妻壁（三角部分）を埋めるブロック。省略時は空いたまま |
| `ridgeBlock` | ブロック | | 階段なら対応するハーフブロック | 棟（最上段）のブロック |
| `block` / `palette` | | ✓（一方） | | 斜面のブロック |

```json
{ "type": "roof", "from": [0, 5, 0], "to": [10, 5, 8], "block": "dark_oak_stairs", "gable": "spruce_planks" }
```

- 1 段ごとに内側へ 1 ブロック寄せながら 1 段上がる。斜面の階段は内側（棟）を向く。
- 幅が奇数なら最上段が 1 列の棟になり `ridgeBlock` を置く。偶数なら最上段は向かい合う 2 列の階段で終わる。
- `hip` の四隅は `shape=outer_left` / `outer_right` の階段で置く（Minecraft が隣接ブロックから求める形と同じ。角は z 方向の `facing` を持ち、その前にある x 方向の辺の向きで左右が決まる）。最上段が向かい合う 2 列で終わる層は四辺がそろわないため `straight` のままにする。`palette` の場合は各ブロックが階段とは限らないため `shape` を付けない。
- 2 つの `roof` を重ねて L 字の屋根を作る場合、谷（`inner_*`）は自動では付かない。必要なら `set` で補正する。
- `rotate` の中では、角の階段が同じ形の別表現（`facing=east,shape=outer_left` と `facing=north,shape=outer_right` は同じ形）になることがある。見た目は変わらない。
- `gable` は `from` / `to` の壁の位置（張り出しの内側）に三角形の壁を作る。

### pillar

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 最下段 |
| `height` | integer ≥ 1 | ✓ | | 高さ |
| `base` | ブロック | | | 最下段を置き換えるブロック |
| `cap` | ブロック | | | 最上段を置き換えるブロック（`height` が 2 以上のとき） |
| `block` / `palette` | | ✓（一方） | | 柱のブロック |

```json
{ "type": "pillar", "position": [0, 0, 0], "height": 5, "block": "stone_bricks", "base": "chiseled_stone_bricks", "cap": "stone_brick_slab" }
```

### doorway

壁に出入口を空け、必要ならドアを付ける。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 開口の左下（壁の中のブロック） |
| `facing` | `north` / `south` / `east` / `west` | ✓ | | ドアの `facing`（外から中を向く方向。北の壁なら `south`） |
| `width` | integer ≥ 1 | | `1` | 幅。`position` から正の方向（東または南）へ広がる。ドアを付ける場合は 1 または 2 |
| `height` | integer ≥ 2 | | `2` | 高さ |
| `door` | ブロック | | | ドアのブロック（`*_door`）。省略時は開口だけ |
| `depth` | integer ≥ 1 | | `1` | 壁の厚さ。`position` から `facing` の方向へ開口を貫通させる。ドアは手前（`position`）の層だけ |
| `arch` | object | | | 頭上をアーチにする。`{ "style": "round" \| "pointed" \| "flat", "block": 縁のブロック, "trim": 段差の階段, "thickness": 縁の厚さ }`（[arch](#arch) 参照） |

```json
{ "type": "doorway", "position": [5, 1, 0], "facing": "south", "width": 2, "door": "oak_door" }
```

- 開口を `minecraft:air` にしてから、ドアを `half=lower` / `upper` の 2 段で置く。幅 2 のときは両開きになるよう `hinge` を左右に振り分ける。
- `arch` を付けると、`height` 段の開口の最上段を起拱点として上にアーチの曲線を足し、周りを `arch.block` で縁取る（開口は `height` + アーチの高さになる）。庇など他の装飾は別の Operation で足す。

### window

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 窓の左下 |
| `axis` | `x` / `z` | ✓ | | 壁が伸びる方向 |
| `width` | integer ≥ 1 | | `1` | 幅（`axis` の正方向へ） |
| `height` | integer ≥ 1 | | `1` | 高さ |
| `block` | ブロック | | `glass_pane` | 窓のブロック |
| `depth` | integer ≥ 1 | | `1` | 壁の厚さ。`axis` と直交する正の方向（`x` なら +z）へ貫通させる |
| `arch` | object | | | 上部をアーチ窓にする。`{ "style", "block": 縁のブロック, "trim", "thickness" }`。曲線部分も `block`（ガラス板）で埋める（[arch](#arch) 参照） |

```json
{ "type": "window", "position": [2, 2, 0], "axis": "x", "width": 2, "height": 2 }
```

- ガラス板・鉄格子・フェンス・壁ブロックのときは、`axis` 方向の接続プロパティ（`east` / `west` または `north` / `south`）を自動で `true` にする。明示した値は変えない。
- `glass` のような完全ブロックはそのまま置く。

### arch

開口の周りに厚さ 1 の縁（アーチ）を作り、開口を空ける。門・アーチ窓・回廊・橋脚に使う。`doorway` / `window` の `arch` キーからも内部で使われる。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 開口の左下（縁ではなく開口の最初のブロック） |
| `axis` | `x` / `z` | | `x` | 壁が伸びる方向（開口はこの方向へ `width` 分広がる） |
| `width` | integer ≥ 1 | ✓ | | 開口の幅。奇数を推奨（偶数は頂点が 2 ブロックになる） |
| `height` | integer ≥ 1 | ✓ | | 開口の高さ（床から頂点の空気まで）。`style` と `width` で決まる最小値以上 |
| `style` | `round` / `pointed` / `flat` | | `round` | 半円 / 尖頭 / 平（水平のまぐさ） |
| `depth` | integer ≥ 1 | | `1` | 奥行き（`axis` と直交する正の方向へ） |
| `thickness` | integer ≥ 1 | | `1` | 縁の厚さ。2 以上にすると外側の段差も階段で滑らかになる（大きなアーチに推奨） |
| `trim` | ブロック | | | 縁の段差に置くブロック。`*_stairs` は外側の凸部に `half=bottom`（中心向き）、開口側の凹部に `half=top`（外向き）で置き、`*_slab` は `type=bottom` / `type=top`、それ以外はそのまま置く |
| `fill` | ブロック | | | 開口を埋めるブロック（アーチ窓のガラス板など）。省略時は `hollow` に従う |
| `hollow` | boolean | | `true` | `true` なら開口を `minecraft:air` にする。`false` なら縁だけ置く（壁の装飾） |
| `block` / `palette` | | ✓（一方） | | 縁のブロック |

```json
{ "type": "arch", "position": [3, 1, 0], "axis": "x", "width": 3, "height": 4, "style": "round", "block": "stone_bricks", "trim": "stone_brick_stairs" }
```

- 開口は幅 `width` × 高さ `height` の矩形の上部を曲線で狭めた形。上から `rise + 1` 段が曲線部分（最下段の起拱点は全幅）、その下が直線部分。縁は開口に上下左右で接するブロック（床より下は除く）。
- `rise`（起拱点から頂点までの段数）: `round` は直径 `width` の円の上半分（`circle` と同じ判定）、`pointed` は起拱点の外側を中心とする半径 `width` の 2 つの弧、`flat` は 0。`height` は `rise + 1` 以上が必要（不足するとエラー）。

| `width` | 3 | 5 | 7 | 9 |
|---|---|---|---|---|
| `round` の rise / 各段の幅 | 1 / 3,3 | 2 / 5,5,3 | 3 / 7,7,5,3 | 4 / 9,9,9,7,5 |
| `pointed` の rise / 各段の幅 | 2 / 3,3,1 | 4 / 5,5,5,3,1 | 6 / 7,7,7,5,5,3,1 | 8 / 9,9,9,9,7,7,5,3,1 |

- `trim` は縁のセルを置き換える（開口は空いたまま）。開口側の凹部（下が開口で、その外隣の下が開口でない縁のセル。各段の両端）には逆さ階段を置き、外側の凸部（上と外側が空いていて、開口か 1 段広い縁の上にあるセル）には通常の階段を置く。厚さ 1 の縁では同じセルが両方に当たるので開口側を優先し、外側の輪郭は角ばったまま。`thickness: 2` にすると外側にも 45° の階段が並び、手作りのアーチに近くなる。
- 曲線のあるアーチ（最上段が全幅より狭いもの）は頂上を通常ブロックのままにする: 縁の最上列と、開口の最上段の真上（天井）には階段を置かない。頂上まで階段にすると尖って見えるため。幅 3 の `round` と `flat` は最上段が全幅のままで曲線がないので、天井の両端が逆さ階段になり古典的な門になる。
- 色の近いトラップドア（`spruce_trapdoor` など）を縁の外側に `set` で足すと、さらに細い曲線に見せられる（自動では置かない）。
- 展開順: 縁 → 開口（`air` または `fill`）→ `trim`。既にある壁の上に書けば縁が壁を置き換え、開口が空く。
- 支持チェックの対象になるブロック（ランタンなど）を縁の上に置く場合は、縁が完全ブロックであることを確認する。

### room

1 階分の部屋（床・外周壁・天井・四隅の柱）とドア・窓の開口を一括で作る。内部で `floor` / `wall` / `fill` / `doorway` / `window` に展開する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `from`, `to` | Pos | ✓ | | 外寸の対角。`from.y` が床の層、`to.y` が天井の層（天井なしなら壁の最上段） |
| `wall` | ブロック または `{ "palette": 名前 }` | ✓ | | 壁のブロック |
| `floor` | 同上 | | | 床（`from.y` の層全体）。省略時は床を置かない |
| `ceiling` | 同上 | | | 天井（`to.y` の層の内側）。省略時は天井を置かず、内部は `to.y` まで空く |
| `corners` | 同上 | | | 四隅の柱（壁と同じ高さ） |
| `thickness` | integer ≥ 1 | | `1` | 壁の厚さ |
| `interior` | boolean | | `true` | 内部を `minecraft:air` にする |
| `doors` | 配列 | | `[]` | ドア。`{ "side", "offset", "width"(1), "height"(2), "door", "arch" }` |
| `windows` | 配列 | | `[]` | 窓。`{ "side", "offset", "width"(1), "height"(1), "sill"(2), "count"(1), "spacing"(2), "block"(glass_pane), "arch" }` |

ドア・窓の共通キー:

| キー | 説明 |
|---|---|
| `side` | `north` / `south` / `east` / `west`。どの壁に置くか |
| `offset` | 壁の `from` 側の角からの距離（東西の壁なら z 方向）。省略時は壁の中央（`count` 個の窓なら全体を中央に寄せる） |
| `width`, `height` | 開口の大きさ。ドアの `height` は 2 以上 |
| `sill` | 窓の下端の高さ（床の層 `from.y` からの段数）。ドアは常に 1（下段が床の 1 つ上） |
| `count`, `spacing` | 窓の個数と窓どうしの間隔（等間隔に並べる） |
| `door` / `block` | ドア（`*_door`。幅 2 で両開き）/ 窓のブロック |
| `arch` | [arch](#arch) と同じ `{ "style", "block", "trim", "thickness" }`。開口の上にアーチを載せる |

```json
{
  "type": "room", "from": [0, 0, 0], "to": [10, 5, 8],
  "wall": { "palette": "plaster" }, "floor": "stone_bricks", "corners": "oak_log",
  "doors": [ { "side": "north", "door": "oak_door" } ],
  "windows": [ { "side": "north", "count": 2, "spacing": 5, "height": 2 }, { "side": "south", "count": 3, "height": 2 } ]
}
```

- 展開順: 床 → 壁 → 四隅の柱 → 天井 → 内部を空気 → 窓 → ドア。同じ場所に窓とドアがあればドアが勝つ。
- 開口は壁の角（厚さ分）を避けた範囲にしか置けない。範囲外や、アーチを含めて壁の高さに収まらない場合はエラー（使える範囲を表示する）。
- 壁は `to.y` まで立ち上がる。天井は内側だけに張るので、外から見た壁は途切れない。屋根は `roof` の `from` / `to` を `to.y` の高さにして重ねる（[examples/cottage.json](../examples/cottage.json)）。
- 複数階は `room` を階ごとに書く（上の階の `from.y` を下の階の `to.y` にすると、下の天井が上の床になる）。

### building

矩形の一〜二階建て。固定配置の `room` / `stairs` / 切妻 `roof` に展開する。自由間取り、屋根形式の選択、階段位置の変更は扱わない。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 回転前の北西隅の床ブロック位置。回転原点でもある |
| `width` | integer ≥ 5 | ✓ | | ローカル +x 方向の外寸（両壁を含む） |
| `depth` | integer ≥ 5 | ✓ | | ローカル +z 方向の外寸。二階建ては `floorHeight + 4` 以上 |
| `floors` | `1` / `2` | | `1` | 階数 |
| `floorHeight` | integer ≥ 4 | | `4` | 床ブロック層から次の床・最上階天井層までの高さ |
| `angle` | `0` / `90` / `180` / `270` | | `0` | position を中心とする時計回りの回転角。ブロック状態の向きも回る |
| `wall` / `floor` | ブロック | ✓ | | 壁・妻壁／床・天井・階段下地。物理的な完全ブロックを指定する |
| `roof` | ブロック | ✓ | | `*_stairs`。棟には対応する slab を使う |
| `stairs` | ブロック | 二階のみ | | `*_stairs`。一階建てでは省略できる |
| `window` | ブロック | ✓ | | 窓を埋めるガラス等 |
| `door` | ブロック | ✓ | | `*_door`。入口に付けるドア |

材質は単一ブロックで指定し、Palette は受け付けない。`roof` / `stairs` の `facing` / `half` / `shape` は生成規則が決めるため明示指定を拒否する。ほかのプロパティは通常の版別材質検証を受ける。材質の衝突形状や物理更新の完全なシミュレーションは行わない。

```json
{
  "type": "building", "position": [0, 0, 0], "width": 7, "depth": 8,
  "floors": 2, "floorHeight": 4, "angle": 0,
  "wall": "stone_bricks", "floor": "oak_planks", "roof": "oak_stairs",
  "stairs": "oak_stairs", "window": "glass", "door": "oak_door"
}
```

標準配置（回転前、position を `[0, 0, 0]` とする）:

- 各階の床層は `y = 階番号 × floorHeight`（階番号は0始まり）。入口下段は一階の床Y+1。壁厚は1で、各階の室内を air で空ける。最上階にも床材の天井を張る。
- 入口は北壁 `x = floor((width − 1) / 2), z = 0` の幅1・高さ2。偶数幅では中央2セルの西側を選ぶ。ドアは南向きに付ける。
- 東・南壁に各階1つずつ、壁の内側範囲の中央（偶数なら小さい座標側）へ幅1・高さ2・sill2の窓を置く。二階の北壁にも同じ窓を置く。一階北壁は入口、西壁は階段側なので窓を置かない。
- 二階への階段は `[1, 1, 2]` から南へ幅1・`floorHeight` 段、`headroom=3`。段の上だけでなく、半段へ移る途中に身体が残る低い側の頭上も空け、上階床を必要箇所だけ開ける。上階到着床は `[1, floorHeight, floorHeight + 2]` で、その上の足元2セルは air。
- 屋根は最上階天井の高さ `floors × floorHeight` から、棟軸z・軒の出1の切妻屋根。妻壁はwall材。奇数幅はslabの棟、偶数幅は二列の階段が向かい合う頂部となる（既存roofと同じ）。

幅5以上は入口・採光窓を西側階段レーンから分離し、階高4以上は窓上端を次階床の下に収め、階段の頭上を最上階天井の下に収めるために必要。二階の奥行きは階段全段と到着床を外壁内へ収めるため `floorHeight + 4` 以上を生成前に要求する。条件を満たさない寸法は丸めずに拒否する。

Python APIの `BuildingOperation.layout` は生成セルを再解析せず、固定規則から得た不変の `BuildingLayout` を返す。`entrance`（ドア下段）、`entry_direction`（内向き）、`arrivals`（各階の足元セル）、`faces`（開口を含む外周4面の外向き方角とAABB）、`roof_bounds`（軒・棟を含むAABB）、`roof_ridge`（棟軸）を持つ。position / angle 適用後の配置座標であり、外側のtranslate / rotate等による追加変換は含まない。facesの順は回転前の北・東・南・西に対応する。

### join_buildings

既存 `building` 記述を二棟だけ内包し、共有壁と内部開口の生成責任を一つの操作へ置く。初期対応は軸平行の反対向きの面が同じ座標平面で接する、同床高・同階高・同階数（1+1 または 2+2）・壁厚1の二棟。三棟以上、ID参照グラフ、輸入建築の推定、床高差、異階数は扱わない。屋根再接合は `joinRoof` の限定条件だけに対応する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `buildings` | building 配列（要素数2） | ✓ | | `type: building` を含む既存記述そのまま。position/angle はこの操作内の配置座標 |
| `faces` | 方角配列（要素数2） | ✓ | | 各 building の position/angle 適用後の接する外向きの面。互いに反対向き |
| `wall` | Block | ✓ | | 共有壁の材質。パレット不可 |
| `openings` | 開口配列（1つ以上） | ✓ | | 各要素は下記の内部開口。重複不可 |
| `joinRoof` | boolean | | `false` | 全幅の妻面同士が揃う同条件二棟だけを、一続きの切妻屋根にする |

開口は `offset`（integer ≥1）が必須。`floor` は0始まり（既定0）、`width` はinteger ≥1（既定1）、`height` はinteger ≥2（既定2）。共有面の接線軸は east/west 面なら z、それ以外は x。`offset` はその軸の共有範囲の最小座標から数える。両端を1セル以上残し、開口上端はその階の天井層より下に収める。左右・向きによって offset の正方向を変えない。

```json
{
  "type": "join_buildings",
  "buildings": [
    {"type": "building", "position": [0,0,0], "width": 7, "depth": 10, "floors": 2,
     "wall": "stone_bricks", "floor": "oak_planks", "roof": "oak_stairs",
     "stairs": "oak_stairs", "window": "glass", "door": "oak_door"},
    {"type": "building", "position": [6,0,0], "width": 7, "depth": 10, "floors": 2,
     "wall": "stone_bricks", "floor": "birch_planks", "roof": "oak_stairs",
     "stairs": "oak_stairs", "window": "glass", "door": "oak_door"}
  ],
  "faces": ["east", "west"], "wall": "bricks",
  "openings": [{"floor": 0, "offset": 7, "width": 2}, {"floor": 1, "offset": 7, "width": 2}]
}
```

共有面長は角を含めて3以上。部分的な接触では実際の共通範囲だけを共有にし、同じ方角の非共有部分の壁・窓を残す。両棟からは共有範囲（基礎床線を含む）へ配置せず、接続操作だけが各セルを一度生成する。開口は air、各階の床・天井の境界行は先頭棟の floor 材、それ以外は指定 wall 材。窓や外装の共有面への配置も抑止する。元の二棟を生成後に壁を削る方式ではない。

主要入口の下段・上段が共有面へ入る場合、または開口の内側1セルの通路がどちらかの階段予約域（土台・頭上・到着を含む）へ入る場合、生成前に拒否する。予約域は固定階段レーンを包むAABBであり、任意モデルの厳密な衝突判定ではない。異階数は低い独立屋根の軒が高い棟の上側非共有外壁・床へかかるため未対応として拒否する。屋根を黙って切除したり壁を後から戻したりしない。`joinRoof` の省略/falseでは屋根そのものを従来の二つのまま保持する。trueでも異階数は扱わない。

`JoinBuildingsOperation.layout` は不変の `JoinLayout`。`buildings` は二つの既存 `BuildingLayout`、`shared_face` は先頭棟の外向き方向と実共有面AABB（床上1〜共通最上天井）、`floor_y` は基礎床Y、`openings` は空気として生成する開口AABBのtuple。各棟のposition/angle適用後で、外側のtranslate/rotate等は含まない。接続面を使う呼出元は外側変換を別途適用する。操作全体を回転する場合は外側のrotateを使えば、最小座標基準のoffsetを手で反転せずに済む。

Pythonの `BuildingOperation.apply_without_face(ctx, face)` は接続操作へ共有面の生成責務を移譲する内部連携用。face は building のposition/angle適用後、外側変換前の座標。`include_roof=False` は屋根全体の生成を接続操作へ移譲する内部切替。汎用マスクや自由な建物削除用APIではない。`stairs_bounds` は同じ座標系の階段予約域で、一階建てではNone。既存 `BuildingLayout` のフィールドは変更しない。固定fixture・比較・未実施項目は [JOINED_BUILDINGS.md](JOINED_BUILDINGS.md)。

#### 切妻屋根の接合（joinRoof）

`joinRoof: true` は、同幅・同じ棟軸・同軒高・同じ段勾配・同じ屋根材で、全幅の妻面同士が揃って接する場合だけに使う。buildingの屋根は固定gable/軒1/一段ごと1ブロックの勾配で、床高・階高・階数一致から軒高も一致する。長さは異なってよい。T/L字の部分接触、直交棟、異幅/材/軒高/勾配、三棟以上、自由形状は生成前に拒否する。勾配や軒を変更する入力は公開しない。

両buildingの個別roofを生成せず、二棟の外周矩形に一つのroofを生成する。内部の妻壁・端の軒は作らず、外周だけに妻壁を置く。屋根面・軒・棟は既存roofの奇数幅slab/偶数幅の向かい合うstairsと同じ形状。二つの外周妻壁はそれぞれ端を持つbuildingのwall材を維持し、建物の配列順で材質を入れ替えない。共有壁の天井行は残し、その上の内部妻壁を作らない。airによる事後削除は行わない。

例は [JOINED_ROOFS.md](JOINED_ROOFS.md)。既存の北中央主入口を共有面へ潰さないため、典型配置は二棟目を180度回転し、ローカル南妻面同士で接する。接合屋根の開始Yは最上階天井以上であり、headroom3の階段/到着予約域と内部開口はその下に収まる。固定fixtureは#106の限定通行モデルでも連続経路を再確認する。`JoinLayout` / `BuildingLayout` の既存フィールドと座標系は不変で、各buildingのroof_boundsは元記述の屋根範囲を表す。操作全体の範囲は従来どおりbounds()から取得する。

### tower

円形または角形の塔を一括で作る。外壁、各階の床、中央の螺旋階段、屋上（外壁より 1 ブロック張り出す）、胸壁、各階の窓、1 階の入口。内部で `cylinder` / `circle`（角形は `wall` / `floor` / `fill`）、`spiral_stairs`、`window`、`doorway` に展開する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 1 階の床の層の中心。壁は `position.y + 1` から立ち上がる |
| `shape` | `round` / `square` | | `round` | 円形 / 角形 |
| `radius` | integer ≥ 2 | 円形で ✓ | | 外壁の半径（`circle` と同じ判定）。階段を付けるなら 3 以上 |
| `size` | 奇数 integer ≥ 5 | 角形で ✓ | | 外壁の一辺（中心がブロックになるよう奇数） |
| `height` | integer ≥ 3 | ✓ | | 壁の高さ（`position.y + 1` から `position.y + height` まで）。屋上の床は `position.y + height + 1` |
| `wall` | ブロック または `{ "palette" }` | ✓ | | 外壁・屋上の床のブロック |
| `floor` | 同上 | | `wall` | 1 階と各階の床 |
| `floors` | integer ≥ 3 | | | 階の間隔。`position.y + floors`, `+ 2 × floors`, … に床を張る（屋上の 2 段下まで）。省略時は 1 階の床だけ |
| `stairs` | object / `false` | | `{}` | 中央の螺旋階段。`{ "radius"(min(2, radius − 2)), "block"(= floor), "turn"(clockwise), "column" }`。`false` で無し。1 階の床の上から屋上まで登り、各階の床に吹き抜けを空ける |
| `battlement` | object / `true` | | | 胸壁。屋上の縁に 1 段の欄干と、その上に `spacing` 間隔の凸部（`{ "block"(= wall), "spacing"(1) }`）。`spacing: 0` で 2 段の欄干 |
| `windows` | object | | | 各階の窓（東西南北の中央）。`{ "sides"(4 方向), "sill"(2), "width"(1), "height"(1), "block"(glass_pane), "arch" }`。1 階の入口側と、収まらない最上階には置かない |
| `door` | object | | | 1 階の入口。`{ "side"(north), "width"(1), "height"(2), "block"（`*_door`）, "arch" }` |

```json
{
  "type": "tower", "position": [0, 0, 0], "radius": 6, "height": 20, "floors": 5,
  "wall": { "palette": "stone_wall" }, "floor": "spruce_planks",
  "battlement": { "spacing": 1 }, "windows": { "height": 2 }, "door": { "side": "north", "block": "spruce_door" }
}
```

- 展開順: 内部を空気 → 各階の床（外壁の半径まで）→ 外壁 → 屋上の床（半径 + 1）→ 胸壁 → 螺旋階段 → 窓 → 入口。階段の頭上空間が各階の床を抜くので吹き抜けは自動でできる。
- 螺旋階段の半径は `radius − 2` 以下（外壁との間に通路を残す）。最後の段は屋上の床と同じ高さ。
- 円形の窓は東西南北の外壁のセル（`(±radius, 0)`, `(0, ±radius)`）に置く。`width` は 3 まで（それ以上は外壁から外れる）。
- 円錐・ドームなどの屋根は別の Operation で屋上（`position.y + height + 1`）に載せる（[examples/tower.json](../examples/tower.json)）。

### bridge

2 点を結ぶまっすぐな橋。平橋または中央が高い太鼓橋（`arch`）、路面の両端の欄干、川床まで下ろす橋脚。内部で `fill` / `set` に展開する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `from`, `to` | Pos | ✓ | | 路面の中心線の両端（路面のブロックの層。両岸の地面と同じ y）。同じ y で、x または z が一致 |
| `width` | integer ≥ 1 | | `3` | 路面の幅（奇数なら中心線の両側に均等、偶数は正の側に 1 多い） |
| `deck` | ブロック または `{ "palette" }` | ✓ | | 路面と橋体のブロック |
| `style` | `flat` / `arch` | | `flat` | 平橋 / 太鼓橋 |
| `rise` | integer ≥ 1 | `arch` で ✓ | | 中央の盛り上がり。両端から 1 ブロックずつ上がり、中央の平らな部分が 1 以上残る長さが必要（`rise ≤ (長さ − 1) / 2`） |
| `stairs` | ブロック | | | 斜面に使う階段ブロック（`facing` は中央向きに自動）。省略時は `deck` の 1 段ずつの段差になる |
| `railing` | ブロック | | | 欄干（`*_fence` / `*_wall` / `*_pane` / `*_bars` は接続を自動設定、ハーフブロックなども可）。路面の両端の列に置くので通路は `width − 2` |
| `railingHeight` | integer ≥ 1 | | `1` | 欄干の高さ |
| `piers` | object | | | 橋脚。`{ "spacing": 間隔, "bottom": 着地させる y, "block": ブロック（= deck） }`。`from` から `spacing` ごとに、路面の幅いっぱいの壁を `bottom` から路面の下まで立てる |

```json
{ "type": "bridge", "from": [2, 4, 3], "to": [22, 4, 3], "width": 5, "style": "arch", "rise": 3, "deck": "stone_bricks", "stairs": "stone_brick_stairs", "railing": "stone_brick_wall", "piers": { "spacing": 5, "bottom": 1 } }
```

- 路面の上 2 ブロックを `minecraft:air` にしてから欄干を置くので、プレイヤーは必ず通れる。
- `arch` は両端から `rise` 個の段（階段ブロック）で上がり、中央は平ら。盛り上がった部分の下は `deck` で埋める（橋体）。
- 欄干の `*_wall` は段差の位置で柱（`up=true`）になる。`*_fence` の高さ違いの接続は Minecraft 側の見た目に従う。
- 例: [examples/bridge.json](../examples/bridge.json)（両岸と川を `fill` で作り、太鼓橋を渡す）。

### gate

城壁に通す城門。柱壁とアーチの通路、上の歩廊と胸壁、落とし格子、正面のドア、両脇の角塔を一括で作る。内部で `fill` / `arch` / `set` / `doorway` / `tower` に展開する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `position` | Pos | ✓ | | 通路の床（地面の層）の中心。門の正面。通路は `position.y + 1` から |
| `axis` | `x` / `z` | | `x` | 壁が伸びる方向。通路はこれと直交し、本体は `position` から正の方向（`x` なら +z）へ `depth` 分伸びる |
| `width` | integer ≥ 1 | ✓ | | 通路の幅（奇数推奨） |
| `height` | integer ≥ 2 | ✓ | | 通路の直線部分の高さ。アーチの曲線はこの上に加わる |
| `depth` | integer ≥ 1 | | `3` | 本体の奥行き（壁の厚さ） |
| `jamb` | integer ≥ 1 | | `2` | 通路の両脇の柱壁の幅。`arch.thickness` 以上 |
| `top` | integer ≥ 0 | | `1` | アーチの縁の上に積む壁の段数。歩廊の床は `position.y + height + rise + 1 + top` |
| `arch` | object | | `{ "style": "round" }` | `{ style, block \| palette（既定は本体）, trim, thickness }`。[arch](#arch) と同じ |
| `battlement` | object / `true` | | | 歩廊の前後の縁に欄干 1 段と `spacing` 間隔の凸部（`{ block(= 本体), spacing(1) }`）。左右の端は壁や塔とつながるので空ける |
| `portcullis` | object | | | 落とし格子。通路の奥行き中央の層に、開口の上から `height` 段（`{ block(iron_bars), height(1) }`）。通路には 2 段以上の空きを残す |
| `door` | ブロック | | | 正面のドア（`width` 1 / 2 のみ） |
| `towers` | object | | | 両脇の角塔。`{ size(7、奇数), height(歩廊 + 4), wall(= 本体), floors, stairs, battlement, windows, door, floor }`。`size` 5 は螺旋階段が入らないので `stairs` 無し。歩廊の高さに床を張り、塔の壁に歩廊への出入口を空ける |
| `block` / `palette` | | ✓（一方） | | 本体のブロック |

```json
{ "type": "gate", "position": [0, 0, 0], "axis": "x", "width": 5, "height": 5, "depth": 3, "jamb": 3, "palette": "stone_wall",
  "arch": { "trim": "stone_brick_stairs", "thickness": 2 }, "battlement": { "spacing": 1 },
  "portcullis": { "height": 2 }, "towers": { "size": 7, "height": 16, "windows": { "height": 2 } } }
```

- 展開順: 本体の直方体 → 通路（`arch` を `depth` 分貫通）→ 胸壁 → 落とし格子 → ドア → 塔と歩廊への出入口。
- 城壁とつなぐときは、壁の高さを歩廊の床（`position.y + height + rise + 1 + top`）に合わせる。`rise` は `width` と `style` で決まる（[arch](#arch) の表）。
- 例: [examples/castle_gate.json](../examples/castle_gate.json)。

### garden

畑または花壇。畑は湿った耕地と作物と水路、花壇は地面と Palette で散らした草花。周りに柵・門・ランタンを置ける。内部で `floor` / `fill` / `set` に展開する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `from`, `to` | Pos | ✓ | | 地面の層の矩形（`from.y == to.y`）。この層を `ground` / 耕地 / 水に置き換え、1 段上に作物や草花、2 段上まで空気にする |
| `style` | `farm` / `flowers` | | `farm` | 畑 / 花壇 |
| `crop` | ブロック | | `wheat[age=7]` | 畑の作物（`carrots[age=7]`, `potatoes[age=7]`, `beetroots[age=3]` など） |
| `water` | integer ≥ 0 | | `4` | 水路の間隔。耕地 `water` 列ごとに 1 列の水（どの耕地も水から `water` 以内）。`0` で水路なし |
| `rows` | `x` / `z` | | 長い辺 | 水路（と作物の列）の向き |
| `ground` | ブロック または `{ "palette" }` | | `grass_block` | 柵の下と花壇の地面 |
| `plants` | ブロック または `{ "palette" }` | `flowers` で ✓ | | 花壇の草花。Palette に `minecraft:air` を混ぜて密度を下げる |
| `fence` | ブロック | | | 矩形の最外周の 1 段上に置く柵（`*_fence` / `*_wall` / `*_pane` は接続を自動設定）。柵があると内側 1 マス縮めた範囲が畑・花壇になる（3 x 3 以上） |
| `gate` | object | | | 柵の門。`{ side(south), offset（角からの距離。省略で中央）, block(oak_fence_gate) }` |
| `lanterns` | object | | | 柵の上のランタン。`{ spacing(4), block(lantern) }`。北西の角から柵に沿って `spacing` ごと |

```json
{ "type": "garden", "from": [0, 0, 0], "to": [14, 0, 10], "crop": "wheat[age=7]", "fence": "oak_fence", "gate": { "side": "south" }, "lanterns": { "spacing": 6 } }
```

- 耕地は `farmland[moisture=7]`。ゲーム内では水から 4 ブロック以内でないと乾くので、`water` は 4 以下にする。
- 作物は成長段階を含めて指定する（`age` を省くと発芽直後になる）。
- 例: [examples/farm.json](../examples/farm.json)（畑と花壇）。

### path

経由点を結ぶ道。軸に沿った区間ごとに幅のある路面を敷き、高さの違いは区間内に均等に割った 1 段ずつの段差（`stairs` 指定時は階段ブロック）でつなぐ。両脇の縁石と一定間隔の街灯を付けられる。内部で `set` / `fill` / `component` に展開する。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `points` | Pos の配列（2 点以上） | ✓ | | 路面の中心線の経由点（路面のブロックの層）。隣り合う点は x または z が一致（軸平行）。y の差は区間の長さ以下 |
| `width` | integer ≥ 1 | | `2` | 路面の幅。奇数は中心線の両側に均等、偶数は進行方向の右側に 1 多い |
| `block` / `palette` | | ✓（一方） | | 路面のブロック |
| `edge` | ブロック | | | 路面の両脇（区間に沿って 1 列ずつ）に置く縁石。角には置かない |
| `stairs` | ブロック | | | 段差に使う階段ブロック（登る側を向く `facing` を自動設定）。省略時は `block` の段差 |
| `clearance` | integer ≥ 0 | | `2` | 路面の上に空ける段数（草や木を取り除く） |
| `lights` | object | | | 街灯。`{ spacing, component }`（部品を進行方向右側の縁の外に置く。原点は路面の 1 段上）または `{ spacing, block }`（ブロックを同じ位置に置く）。`spacing` ブロックごと、始点から |

```json
{ "type": "path", "points": [[2, 0, 2], [2, 0, 16], [10, 0, 16], [17, 4, 16]], "width": 3, "palette": "road", "edge": "cobblestone", "stairs": "cobblestone_stairs", "lights": { "spacing": 7, "component": "lantern_post" } }
```

- 角（途中の経由点）は入る区間と出る区間の幅で作る正方形で埋め、途切れないようにする。
- 段差は路面の下を埋めない。地形が下がっている場所を登らせるときは、`fill` で段を足しておく。
- 例: [examples/village_path.json](../examples/village_path.json)（草地と高台をつなぐ道）。

### landform

楕円形の丘または矩形の台地を、列ごとの地表高で作る。`from` / `to` は X/Z の範囲と基準 Y を示す（両端を含み、Y は同じ）。`height` は基準 Y からの上昇量。基準面から地表まで列を埋め、地表の最上段に `surface`、その下に `subsurface` を置く。材質はブロック文字列、`{"block": "..."}`、`{"palette": "..."}` のいずれか。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `shape` | `hill` / `plateau` | ✓ | | 楕円形の丘 / 矩形の台地 |
| `from`, `to` | Pos | ✓ | | X/Z の両端を含む範囲。Y は基準面で一致させる。X/Z は各 3 ブロック以上 |
| `height` | integer ≥ 1 | ✓ | | 基準 Y から頂上までの高さ。生成上限は通常の bounds 検査に従う |
| `edgeWidth` | integer ≥ 1 | 台地で ✓ | | 台地の縁の幅。各辺がこの幅以上の内側で平坦になる。丘では指定不可 |
| `surface` | ブロック または `{ "block" }` / `{ "palette" }` | ✓ | | 各列の最上段 |
| `subsurface` | ブロック または `{ "block" }` / `{ "palette" }` | ✓ | | 最上段より下から基準 Y まで |

丘は楕円内だけを生成し、正規化した中心距離に比例して高さを減衰させる。矩形範囲の隅は空き、bounds はその隅も含む包括直方体である。偶数寸法では中央の 2 セル（両軸が偶数なら中央の 4 セル）が指定高さになる。台地は矩形の縁で基準高になり、`edgeWidth` に向けて整数の四捨五入（ちょうど半分は上側）で上昇し、内側は平坦になる。各列は z、x の順、列内は基準 Y から上向きに配置する。同じ位置への後続 Operation は既存規則どおり上書きする。

```json
{ "type": "landform", "shape": "hill", "from": [-8, 63, -8], "to": [8, 63, 8], "height": 7, "surface": "grass_block", "subsurface": "dirt" }
```

```json
{ "type": "landform", "shape": "plateau", "from": [20, 63, 0], "to": [34, 63, 14], "height": 5, "edgeWidth": 3, "surface": { "block": "grass_block" }, "subsurface": { "palette": "rock" } }
```

丘は楕円内の中心を含むセルが最高点で、楕円境界は基準高になる。台地の全列は基準 Y から地表まで連続して書き込まれる。

### component

利用者部品または同梱パターンモジュールを配置する（[FORMAT.md §11](FORMAT.md#11-部品component)）。

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `name` | string | ✓ | | 利用者部品名（`^[a-z0-9_]+$`）または同梱モジュール名（例 `medieval.window`） |
| `position` | Pos | ✓ | | 部品の原点を置く位置 |
| `rotation` | `0` / `90` / `180` / `270` | | `0` | 部品の原点を軸に上から見て時計回りに回転 |

ここでの `name` は参照名で、部品データ内の表示名 `name` とは異なる。単純名は利用者の `components/`、ドット名は同梱モジュールから解決する。利用者の `components/` 内のサブディレクトリやドット付きファイル名はドット名の解決に使わない。探索順は [FORMAT.md §11](FORMAT.md#11-部品component) を参照。

```json
{ "type": "component", "name": "medieval.window", "position": [14, 2, 2], "rotation": 90 }
```

- 部品の Operation を、回転 → 平行移動の変換で実行する。`facing` / `axis` / 接続プロパティは自動で回る。
- 部品の Palette は部品内で優先され、ない名前は呼び出し元の Palette を使う。
- bounds: 部品の Operation の bounds の合成を変換したもの。

---

## 座標変換とブロック状態

`mirror` / `rotate`（および `repeat` / `translate` の平行移動）は、位置だけでなくブロック状態の向きも変換する。

| プロパティ | 変換 |
|---|---|
| `facing`（`north` / `south` / `east` / `west` / `up` / `down`） | 方向ベクトルを変換した先の方向 |
| `axis`（原木など。`x` / `y` / `z`） | 軸を変換した先の軸 |
| 接続プロパティ `north` / `south` / `east` / `west` / `up` / `down`（板ガラス・フェンス・壁など） | プロパティ名の方向を変換して付け替え |
| `half` / `type` の `top` ↔ `bottom` | Y 軸を反転する変換（`mirror` の `axis: "y"`）のときのみ |
| `shape` の `inner_left` ↔ `inner_right`, `outer_left` ↔ `outer_right`、`hinge` の `left` ↔ `right` | 鏡像（反転を含む変換）のときのみ。回転では変わらない |
| `rotation`（看板・旗の 0〜15）、レール `shape` など | 変換しない |

---

## 将来対応

現時点で追加を検討中の Operation はない。要望は Issue に起票する。
