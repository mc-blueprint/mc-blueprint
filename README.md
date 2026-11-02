# mc-blueprint

Codex や Claude Code などの AI コーディングエージェントに Minecraft の建築を設計させ、WorldEdit / Litematica で読み込める `.schem` を生成するツールキットです。

AI に NBT や Schematic 形式を直接書かせるのではなく、人間も編集できる中間形式 **Blueprint JSON** を定義し、検証と変換はツール側が担当します。

```text
ユーザーの自然言語
        ↓
Codex / Claude Code
        ↓
Blueprint JSON  (blueprints/*.json)
        ↓
mcblueprint validate   …… Schema・ブロック ID・範囲を検証し、全エラーを報告
        ↓
mcblueprint build      …… Operation を実行して .schem / .litematic を書き出す
        ↓
.schem  (output/*.schem)
```

## 特徴

- **宣言的な Blueprint JSON**: `fill` / `wall` / `cylinder` / `sphere` / `repeat` / `mirror` などの高レベルな Operation を組み合わせて建築を記述する。1 ブロックずつ座標を並べない。
- **Palette**: 複数ブロックの重み付きランダム配置で壁面に質感を付ける。`seed` で結果を再現できる。
- **AI が読める検証結果**: JSON パスと原因と値を 1 エラー 1 ブロックで全件表示し、AI が自分で修正できる。支えのないランタン・松明・ドアなどは警告として検出する。
- **プレビュー画像**: 上面・立面・アイソメトリックの PNG を出力し、人間も AI も貼り付け前に形を確認できる。
- **Import と diff**: 既存の `.schem` / `.litematic` を Blueprint に変換でき、2 つの Blueprint の生成結果を比較できる。
- **AI 非依存**: Core は Codex / Claude Code に依存しない。両方に同じ Skill を同梱し、他のエージェントからも同じ CLI で使える。
- **デザインプリセット**: 様式・材質・寸法規則を `designs/` のファイルにまとめ、名前で参照できる。
- **部品**: 窓・門・街灯などを `components/` に置き、`component` Operation で回転して配置できる。
- **既存の建築から**: `mcblueprint design` でゲーム内の建物の様式（Palette・階高・屋根・窓）をデザインプリセットに、`mcblueprint import --component` でパーツを部品に変換できる。
- **Git で管理できる**: Blueprint JSON がソース、`.schem` は生成物。

## 要件

- Python 3.11 以上
- 貼り付けには WorldEdit（または FAWE）か Litematica を入れた Minecraft Java Edition

## 対応 Minecraft バージョン

Java Edition の以下のバージョンのブロックデータを同梱しています（`mcblueprint versions` で一覧表示）。

- `1.21.11`
- `26.1.2`, `26.2`, `26.3`（2026 年からの年ベース採番）

Blueprint の `minecraftVersion` にはこのいずれかを書きます。別のバージョンで検証・出力したいときは `--minecraft-version 26.3` のように上書きできます。バージョン間の差分と追加手順は [docs/VERSIONS.md](docs/VERSIONS.md) を参照してください。

## インストール

```bash
git clone https://github.com/mc-blueprint/mc-blueprint.git
```

```bash
cd mc-blueprint
```

```bash
pip install -e .
```

`mcblueprint --version` が表示されればインストール完了です。プレビュー画像（`mcblueprint preview`）を使う場合は `pip install -e ".[preview]"` で Pillow を追加します。

## 使い方

### 1. サンプルを生成する

```bash
mcblueprint build examples/house.json
```

```text
Wrote output/house.schem
Bounds: X -1..11  Y 0..11  Z -1..9  (13 x 12 x 11)
Blocks: 471
```

### 2. Minecraft に貼り付ける

生成された `output/house.schem` を WorldEdit の `schematics/` フォルダにコピーし、ゲーム内で次を実行します。

```text
//schem load house
//paste
```

Blueprint の `origin` で指定した座標がプレイヤーの位置に重なります。Litematica の場合は `.schem` をそのまま読み込めるほか、`--format litematic` で `.litematic` も生成できます。

### 3. AI に建築を依頼する

このリポジトリを Codex または Claude Code で開き、たとえば次のように指示します。

```text
直径 15、高さ 30 の中世風の石の塔を作って。4 階建てで、各階に窓を付けて。
```

エージェントは同梱の `minecraft-blueprint` Skill に従って `blueprints/` に Blueprint JSON を書き、`mcblueprint validate` でエラーを直し、`mcblueprint build` で `output/` に `.schem` を生成します。詳細は [docs/AI_GUIDE.md](docs/AI_GUIDE.md) を参照してください。

様式をそろえたいときは、`designs/` のデザインプリセット名を依頼に含めます（`medieval` / `japanese` / `modern` を同梱。自分のプリセットも追加できます）。

```text
medieval で直径 15、高さ 30 の塔を作って
```

### 4. 自分で Blueprint を書く

```json
{
  "formatVersion": 1,
  "minecraftVersion": "1.21.11",
  "name": "Small Tower",
  "palettes": {
    "stone_wall": [
      { "block": "minecraft:stone_bricks", "weight": 70 },
      { "block": "minecraft:mossy_stone_bricks", "weight": 20 },
      { "block": "minecraft:cracked_stone_bricks", "weight": 10 }
    ]
  },
  "operations": [
    { "type": "cylinder", "center": [0, 0, 0], "radius": 5, "height": 16, "mode": "hollow", "palette": "stone_wall" },
    { "type": "circle", "center": [0, 0, 0], "radius": 4, "mode": "solid", "block": "minecraft:oak_planks" },
    { "type": "sphere", "center": [0, 16, 0], "radius": 5, "mode": "hollow", "block": "minecraft:deepslate_tiles" },
    { "type": "set", "position": [0, 1, -5], "block": "minecraft:oak_door[facing=south,half=lower]" },
    { "type": "set", "position": [0, 2, -5], "block": "minecraft:oak_door[facing=south,half=upper]" }
  ]
}
```

```bash
mcblueprint validate blueprints/small_tower.json
```

```bash
mcblueprint build blueprints/small_tower.json
```

仕様は [docs/FORMAT.md](docs/FORMAT.md) と [docs/OPERATIONS.md](docs/OPERATIONS.md) を参照してください。

## CLI

| コマンド | 説明 |
|---|---|
| `mcblueprint validate <file> [--strict]` | Blueprint を検証し、全エラーと支持警告（支えのないランタンなど）を表示する |
| `mcblueprint build <file> [-o DIR\|FILE] [--format schem\|litematic] [--seed N] [--strict]` | 検証してから `.schem`（または `.litematic`）を書き出す（既定 `output/<名前>.schem`） |
| `mcblueprint inspect <file> [--json]` | 生成せずに範囲・サイズ・Operation 数・Palette 数などを表示する |
| `mcblueprint stats <file> [--json] [--seed N]` | 生成してブロック状態ごとの個数を表示する |
| `mcblueprint preview <file> [-o DIR] [--views ...] [--scale N]` | 上面・立面・アイソメトリックの PNG を `preview/` に書き出す（`pip install -e ".[preview]"` が必要） |
| `mcblueprint import <file.schem` / `.litematic> [-o FILE] [--name NAME]` | 既存の Schematic を `fill` / `set` の Blueprint JSON に変換する（既定 `blueprints/<名前>.json`） |
| `mcblueprint diff <a.json> <b.json> [--json]` | 2 つの Blueprint の生成結果を比較し、追加・削除・変更を表示する |

終了コードは `0` 正常 / `1` 検証エラー / `2` 引数・入出力エラー。支持警告は既定では終了コードを変えず、`--strict` で `1` になる。

検証エラーの例:

```text
ERROR operations[4] (cylinder)
  'radius' must be >= 1.
  Current value: -5

1 error found.
```

## Operation 一覧

| 分類 | type |
|---|---|
| 基本 | `set`, `fill`, `box`, `wall`, `floor`, `line` |
| 図形 | `circle`, `cylinder`, `sphere` |
| 構造 | `mirror`, `repeat`, `translate`, `rotate`（ネスト可） |
| 編集 | `replace`, `copy` |
| 建築 | `stairs`, `spiral_stairs`, `roof`, `pillar`, `doorway`, `window`, `arch`, `room`, `tower`, `bridge`, `gate`, `garden`, `path` |
| 部品 | `component`（`components/<name>.json` を配置） |

## ディレクトリ

```text
blueprints/   自分や AI が作成する Blueprint（作業用）
output/       生成された .schem（git 管理外）
examples/     サンプル Blueprint（house.json, cottage.json, tower.json, tower_manual.json, gatehouse.json, bridge.json, castle_gate.json, farm.json, village_path.json）
components/   再利用する建築部品（component Operation から参照）
designs/      デザインプリセット（様式・Palette・寸法規則。local/ は個人用）
docs/         仕様書・ガイド
schema/       Blueprint JSON Schema（エディタ補完用）
.agents/      Codex 用 Skill
.claude/      Claude Code 用 Skill
```

## ドキュメント

- [docs/FORMAT.md](docs/FORMAT.md) — Blueprint JSON の仕様
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — Operation の仕様
- [docs/AI_GUIDE.md](docs/AI_GUIDE.md) — AI エージェントからの利用手順
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 内部構成
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — 開発手順とルール
- [AGENTS.md](AGENTS.md) — AI エージェント向けのルール

## 今後の予定

- `arch` / `room` / `bridge` などの高レベル建築 Operation の追加
- 複数 Minecraft バージョンのブロックデータ同梱

進捗は [Issues](https://github.com/mc-blueprint/mc-blueprint/issues) を参照してください。

## ライセンス

[MIT](LICENSE)
