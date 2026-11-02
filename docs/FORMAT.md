# Blueprint JSON 仕様（formatVersion 1）

Blueprint JSON は、AI エージェントや人間が記述する Minecraft 建築の設計データである。
ブロックを 1 つずつ列挙するのではなく、[Operation](OPERATIONS.md) を組み合わせて建築を宣言する。

本書が Blueprint JSON の正本である。Skill の references や README は本書の要約であり、食い違う場合は本書を優先する。

## 1. 最小例

```json
{
  "formatVersion": 1,
  "minecraftVersion": "1.21.11",
  "name": "example",
  "operations": [
    { "type": "fill", "from": [0, 0, 0], "to": [4, 0, 4], "block": "minecraft:stone_bricks" }
  ]
}
```

## 2. トップレベル項目

| キー | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| `formatVersion` | integer | ✓ | — | Blueprint 形式のバージョン。本書では固定値 `1` |
| `minecraftVersion` | string | ✓ | — | 対象 Minecraft Java Edition のバージョン（例 `"1.21.11"`, `"26.3"`）。ツールに同梱されたブロックデータが存在するバージョンのみ指定できる（[VERSIONS.md](VERSIONS.md)、`mcblueprint versions`）。CLI の `--minecraft-version` で上書きできる |
| `name` | string | ✓ | — | 建築の表示名。出力ファイル名には使わない（出力名は入力ファイル名に従う） |
| `operations` | array | ✓ | — | [Operation](OPERATIONS.md) の配列。1 件以上 |
| `description` | string | | — | 説明文 |
| `author` | string | | — | 作者 |
| `seed` | integer | | `0` | Palette の乱数 seed。同じ Blueprint と seed からは常に同じ結果が生成される |
| `origin` | `[x, y, z]` | | `[0, 0, 0]` | 貼り付け基準点となる Blueprint 座標。WorldEdit の `//paste` ではこの座標がプレイヤー位置に一致する |
| `size` | `[w, h, l]` | | — | 許容する最大サイズ（X, Y, Z 方向のブロック数、各 1 以上）。生成結果のバウンディングボックスがこれを超えると検証エラー。位置の制約ではない |
| `palettes` | object | | `{}` | Palette 名 → エントリ配列。[§5](#5-palette) を参照 |
| `metadata` | object | | — | 自由記述。ツールは中身を読まず、解釈もしない |
| `terrain` | object | | — | 元地形の参照・施工矩形・保護矩形。詳細は [TERRAIN.md](TERRAIN.md) |
| `foundation` | object | | — | terrain上の明示矩形敷地と平坦な基礎を、上限内で造成する。[FOUNDATION.md](FOUNDATION.md) |
| `surfacePath` | object | | — | terrain上のXZ経由点から全幅の地表道路を解決する。[SURFACE_PATH.md](SURFACE_PATH.md) |

- 上記以外のトップレベルキーは検証エラーになる。
- `metadata` の中身だけは任意の JSON を許可する。

## 3. 座標系

- Minecraft と同じ右手系を使う。X: 東が正、Y: 上が正、Z: 南が正。
- 座標はすべて整数で、`[x, y, z]` の 3 要素配列で表す。
- 負の座標を使ってよい。生成結果の最小座標が `.schem` の原点に自動的に補正される。
- 範囲を表す `from` / `to` は両端を含み、順序は問わない（各軸ごとに min / max を取る）。

## 4. ブロック指定

ブロックは 1 つの文字列で指定する。

```text
minecraft:stone_bricks
stone_bricks
minecraft:oak_stairs[facing=north,half=top]
oak_slab[type=top]
```

### 4.1 文法

```text
<block>      ::= [<namespace> ":"] <id> ["[" <property> ("," <property>)* "]"]
<namespace>  ::= [a-z0-9_.-]+
<id>         ::= [a-z0-9_./-]+
<property>   ::= <name> "=" <value>
<name>       ::= [a-z_]+
<value>      ::= [a-z0-9_]+
```

正規表現:

```text
^(?:[a-z0-9_.-]+:)?[a-z0-9_./-]+(?:\[[a-z_]+=[a-z0-9_]+(?:,[a-z_]+=[a-z0-9_]+)*\])?$
```

### 4.2 正規化

読み込み時に次のとおり正規化する。

1. 名前空間が省略されていれば `minecraft:` を補う。
2. プロパティを名前の昇順に並べ替える。
3. 同じプロパティ名が重複していれば検証エラー。

### 4.3 検証

`minecraftVersion` に対応するブロックデータを使い、次を検証する。

- ブロック ID が存在する。
- プロパティ名がそのブロックに存在する。
- プロパティ値がそのプロパティで許容される値である。

### 4.4 書き出し時の補完

`.schem` へ書き出す際、指定されなかったプロパティはブロックデータのデフォルト値で補完し、全プロパティを持つ完全な状態文字列にする。

例: `oak_stairs[facing=north]` → `minecraft:oak_stairs[facing=north,half=bottom,shape=straight,waterlogged=false]`

### 4.5 空気

`minecraft:air` は通常のブロックとして配置できる。先に配置したブロックを消す用途に使う。
Operation で一度も触れていない位置は、書き出し時に `minecraft:air` として扱う。

## 5. Palette

Palette は、複数のブロックから重み付きでランダムに 1 つを選ぶ仕組みである。壁面に質感を付ける用途に使う。

```json
"palettes": {
  "stone_wall": [
    { "block": "minecraft:stone_bricks", "weight": 70 },
    { "block": "minecraft:mossy_stone_bricks", "weight": 20 },
    { "block": "minecraft:cracked_stone_bricks" }
  ]
}
```

| 項目 | 型 | 必須 | 既定値 | 説明 |
|---|---|---|---|---|
| Palette 名（キー） | string | ✓ | — | `^[a-z0-9_]+$` |
| `block` | string | ✓ | — | [§4](#4-ブロック指定) のブロック指定 |
| `weight` | number | | `1` | 正の数。選ばれる確率は `weight / 合計` |

- エントリは 1 件以上。
- 乱数は `seed` で初期化した 1 本の乱数列を、ブロック配置順に消費する。`block` を直接指定した Operation は乱数を消費しない。
- Operation の順序や内容を変えると、それ以降の Palette の選択結果も変わる。

## 6. Operation 共通

各 Operation はオブジェクトで、次の共通項目を持つ。

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `type` | string | ✓ | Operation の種類。[OPERATIONS.md](OPERATIONS.md) に列挙されたもののみ |
| `block` | string | 配置系で必須（`palette` と排他） | 配置するブロック |
| `palette` | string | 配置系で必須（`block` と排他） | 使用する Palette 名 |
| `comment` | string | | 人間・AI 向けのメモ。処理には影響しない |

- 配置系 Operation（`set`, `fill`, `box`, `wall`, `floor`, `line`, `circle`, `cylinder`, `sphere`）は `block` と `palette` のどちらか一方を必ず指定する。両方の指定、両方の省略は検証エラー。
- 構造 Operation（`mirror`, `repeat`）は `block` / `palette` を持たず、`operations` にネストした Operation を持つ。
- 各 Operation で定義されていないキーは検証エラーになる。

## 7. 適用順序

- `operations` は配列の先頭から順に適用する。
- 同じ位置に複数の Operation が触れた場合、後の Operation が前を上書きする。
- 構造 Operation のネスト内も同じ規則で、ネストした `operations` を先頭から順に適用する。

この規則により、「外側を石で満たしてから内側を空気で抜く」「壁を作ってから窓を空ける」といった記述ができる。

## 8. 上限

極端な入力で処理が暴走しないよう、次の上限を設ける。超えた場合は検証エラー。

| 項目 | 上限 |
|---|---|
| バウンディングボックスの各辺 | 1024 ブロック（CLI の `--max-dimension` で変更可） |
| バウンディングボックスの体積 | 100,000,000 セル |
| Operation のネスト深さ | 8（トップレベルを深さ 1 とする） |
| `repeat.count` | 512 |
| `size` 指定時 | 生成結果のバウンディングボックスが `size` 以下 |

## 9. セキュリティ

- Blueprint は宣言的なデータであり、コマンドやスクリプトを実行する手段を持たない。
- 未知のキーはすべて拒否し、`metadata` 以外に自由な文字列を置く場所を作らない。
- `metadata` の中身はツールが読まないため、そこに何を書いても動作に影響しない。

## 10. formatVersion の互換性

- `formatVersion` は Blueprint 形式そのもののバージョンで、ツールのバージョンとは独立している。
- 互換性のない変更（既存 Operation の意味変更、必須項目の追加など）を行う場合は `formatVersion` を上げる。
- Operation の追加や任意項目の追加は同じ `formatVersion` 内で行う。
- ツールは対応していない `formatVersion` を検証エラーとして拒否する。

## 11. 部品（component）

繰り返し使う建築部品を `components/<name>.json` に置き、Blueprint から `component` Operation で配置できる。

- ファイルは Blueprint と同じ書式で、`minecraftVersion` / `origin` / `size` / `seed` は書かない（`formatVersion`, `name`, `operations` が必須。`description`, `author`, `palettes`, `metadata` は任意）。
- 座標は部品の原点 `[0, 0, 0]` からの相対。`component` の `position` がその原点になり、`rotation` で原点を軸に回転してから配置する。
- 部品の `palettes` は部品内で優先され、書かれていない名前は呼び出し元の Blueprint の Palette を参照する。
- 利用者部品は「Blueprint ファイルと同じ階層の `components/`」→「カレントディレクトリの `components/`」から探索する。名前は `^[a-z0-9_]+$`。同梱パターンモジュールは `medieval.window` のような `<style>.<module>` 名で参照する。
- 部品は入れ子にできる。循環参照とネスト深さ 8 超は検証エラー。
- 部品の内容も Blueprint と同じ検証（Schema・ブロック ID・Palette 参照）を受け、エラーは `operations[3]<name>.operations[0].block` のように部品名付きのパスで報告される。

Operation の仕様は [OPERATIONS.md](OPERATIONS.md#component)、書き方の例は `components/README.md` を参照。

## 12. 完全な例

```json
{
  "formatVersion": 1,
  "minecraftVersion": "1.21.11",
  "name": "Small Tower",
  "description": "石レンガの小さな塔",
  "seed": 42,
  "origin": [0, 0, 0],
  "size": [16, 24, 16],
  "palettes": {
    "stone_wall": [
      { "block": "minecraft:stone_bricks", "weight": 70 },
      { "block": "minecraft:mossy_stone_bricks", "weight": 20 },
      { "block": "minecraft:cracked_stone_bricks", "weight": 10 }
    ]
  },
  "operations": [
    {
      "type": "cylinder",
      "center": [0, 0, 0],
      "radius": 5,
      "height": 16,
      "mode": "hollow",
      "palette": "stone_wall",
      "comment": "塔の外壁"
    },
    {
      "type": "circle",
      "center": [0, 0, 0],
      "radius": 4,
      "mode": "solid",
      "block": "minecraft:oak_planks",
      "comment": "1階の床"
    },
    {
      "type": "repeat",
      "count": 3,
      "offset": [0, 5, 0],
      "operations": [
        { "type": "set", "position": [0, 2, -5], "block": "minecraft:air", "comment": "南北の窓" },
        { "type": "set", "position": [0, 2, 5], "block": "minecraft:air" }
      ]
    },
    {
      "type": "sphere",
      "center": [0, 16, 0],
      "radius": 5,
      "mode": "hollow",
      "block": "minecraft:deepslate_tiles",
      "comment": "ドーム屋根"
    },
    {
      "type": "set",
      "position": [0, 1, -5],
      "block": "minecraft:oak_door[facing=north,half=lower]"
    },
    {
      "type": "set",
      "position": [0, 2, -5],
      "block": "minecraft:oak_door[facing=north,half=upper]"
    }
  ]
}
```
