# AI エージェント向けガイド

Codex、Claude Code などのコーディングエージェントから mc-blueprint を使って Minecraft の建築を生成する手順。
エージェントに依存しない共通の流れをここに書き、各エージェント固有の設定は Skill / 設定ファイルに置く。

住宅・塔は [BuildSpecの手順](#buildspecから住宅塔を生成する) で短いYAMLから生成できます。

## 役割分担

```text
自然言語の依頼
      ↓
AI エージェント        …… 建築を考え、Blueprint JSON を書く
      ↓
blueprints/<name>.json
      ↓
mcblueprint validate   …… Schema・ブロック ID・範囲を検証し、全エラーを報告
      ↓
mcblueprint build      …… Operation を実行し .schem を書き出す
      ↓
output/<name>.schem    …… WorldEdit / Litematica で Minecraft へ貼り付け
```

AI は Minecraft のバイナリ形式（NBT / Schematic）を扱わない。`.schem` を直接生成・編集させない。

## 基本ワークフロー

全生成後に領域へ分割する場合は `build --region-size X Y Z` を使う。
配置マニフェストと共通 origin の扱いは [REGION_EXPORT.md](REGION_EXPORT.md) を参照する。

既存地形へ施工するときは [TERRAIN.md](TERRAIN.md) の `terrain` を指定する。
取得範囲・施工矩形・保護矩形と貼付基準を先に確認し、airを含む完成矩形を出力する。
取得後のワールド変更は保存できないため、貼付前にも対象範囲を確認する。
斜面の一棟用敷地は [FOUNDATION.md](FOUNDATION.md) の `foundation` で明示する。
床ブロックYと入口足元Y（床Y+1）を区別し、報告された改変量と支持深さを確認する。
道路の地表高も解決するときは [SURFACE_PATH.md](SURFACE_PATH.md) を参照し、XZ経由点と
入口足元を明示する。全幅・曲がり角・昇降境界の検査に失敗したら、位置や上限を修正する。

1. 要求を整理する（用途、寸法、階数、材質、必須要素）。
2. `blueprints/<name>.json` に Blueprint を書く。仕様は [FORMAT.md](FORMAT.md) と [OPERATIONS.md](OPERATIONS.md)。
3. `mcblueprint validate blueprints/<name>.json` を実行する。
4. エラーがあれば Blueprint を修正して 3 に戻る。
5. `mcblueprint inspect blueprints/<name>.json` で範囲・サイズが要求と合うか確認する（必要なら `mcblueprint stats` でブロック構成も見る）。画像を読めるエージェントは `mcblueprint preview blueprints/<name>.json` で `preview/<name>-isometric.png` などを生成して形を確認し、内部は `--layers all --grid 5` の水平断面で確認する。接合部・入口・階段などを詳しく見るときは `--from X Y Z --to X Y Z` と必要な view/layer だけを使う。範囲は元の Blueprint 座標で、全生成後に描画だけを限定する。全体図との比較と切口の意味は [PREVIEW_REGIONS.md](PREVIEW_REGIONS.md)。合わなければ 2 に戻る。
6. `mcblueprint build blueprints/<name>.json` を実行し、`output/<name>.schem` を得る。
7. 出力パス、サイズ、主な構成、貼り付け方（`//schem load <name>` → `//paste`）を報告する。

## エラー出力の読み方

```text
ERROR operations[4] (cylinder)
  'radius' must be >= 1.
  Current value: -5

ERROR operations[7].operations[1].block (set)
  Unknown block id.
  Current value: minecraft:stone_brick

2 errors found.
```

- 1 行目: JSON パス（`operations` 配列の index。ネストは `operations[7].operations[1]`）と Operation の `type`
- 2 行目: 原因（英語 1 文）
- 3 行目: 問題の値（あれば）

検証は「Schema → 意味（ブロック ID・Palette 参照など）→ 範囲（size・最大寸法）」の順に行い、ある段階でエラーがあると後の段階は省略する。1 回の修正で全部直らないことがあるので、`Blueprint is valid.` になるまで繰り返す。

終了コードは `0` 正常 / `1` 検証エラー / `2` 引数・入出力エラー（ファイル未存在、JSON 構文エラー）。

### 構造化した検証結果と大量の診断

```bash
mcblueprint validate blueprints/house.json --json --limit 5
mcblueprint validate blueprints/house.json --json --kind semantic --path 'operations[4]'
mcblueprint validate blueprints/house.json --json --limit 5 --offset 5
```

`validate --json` は JSON を標準出力へ返す。既定は50件、`--limit N` は詳細の上限、
`--limit 0` は全件、`--offset N` は絞り込み後の0始まりの開始位置。
`--kind schema|semantic|support` と `--path PATH` は複数指定でき、同じフラグ内はOR、
種類とパスの間はANDで表示対象を絞る。パスは要素境界で前方一致し、
`operations[1]` はその配下を含むが `operations[10]` は含まない。
支持警告のJSONパスは不明なのでパス指定時は表示対象外になる。
これらのフラグは人間向け表示にも使える。フラグなしの表示は従来どおり。

固定項目と意味は次のとおり。

| 項目 | 内容 |
| --- | --- |
| `source` | 入力ファイルのパス |
| `valid` / `exitCode` / `strict` | strictを含む成功判定（`valid` は `exitCode == 0`）、終了コード、strict指定 |
| `counts` | `total` / `errors` / `warnings` / `byKind` は絞り込み前の件数。`matched` は絞り込み後、`shown` は表示件数、`filteredOut` は `total - matched`、`omitted` は `matched - shown`。`groups` は一致した原因数、`groupsOmitted` は未表示の原因数 |
| `diagnostics` | 表示ページの詳細。各要素は `id` / `kind` / `severity` / `path` / `message` / `opType` / `value` / `position` を持つ。該当しない詳細は `null` |
| `groups` | 表示ページに含まれる原因の集計。`kind` / `severity` / `message` / `opType` が同じ診断をまとめ、絞り込み後の全件数 `count`、先頭の `firstId` / `firstPath` を返す |
| `retrieval` | 適用した `kinds` / `paths` / `limit` / `offset`、次ページの `nextOffset`（終端は `null`）、再取得方法 `hint` |

`kind` はSchema検査の `schema`、意味・範囲検査の `semantic`、支持検査の `support`。
`severity` は `error` または `warning`。原因は既存検査の `message`、値は `value`、
支持警告の座標はBlueprint座標の `position: [x, y, z]`、そのブロックは `value` に入る。
通常Operation後の道路再検査で見つかった干渉も、診断表示では `semantic` エラーとして
`path: "surfacePath"`、`value: [x, y, z]` に拒否座標を返す。絞り込みで非表示でも失敗判定は残る。
数値の桁あふれ（例: `1e999`）などで検証値が非有限数になった場合、`value` 内では
入れ子も含めて文字列 `"Infinity"` / `"-Infinity"` / `"NaN"` に置き換える。
これは数値として扱えない入力値を示す表示用の表現で、元の検証判定は変えない。
`id` は絞り込み前の1始まりの順番で、同一入力・検証条件内だけで安定する。

省略した詳細は同じ入力・検証条件とフィルタで `--offset <nextOffset>` を指定して取得する。
全詳細は `--limit 0 --offset 0`、全種類・全パスは `--kind` / `--path` も外す。
入力・Minecraftバージョン・寸法上限・部品・生成seedが変わった場合は先頭から再検証する。
絞り込みは検査後に行うため、表示0件でも失敗が成功に変わることはない。
Schema・意味エラーがあると支持検査へ進まない既存の段階も維持する。
引数・入出力エラーは従来どおり標準エラーへ表示し、終了コード2を返す（JSONは返さない）。

この出力の目的は確認する診断量を制限すること。実際のトークン使用量の削減は、
同じ入力での実測が必要であり、JSON化だけで保証されるものではない。

### 警告（支持チェック）

`validate` と `build` は、生成結果の中でランタン・松明・壁付けブロック・ドア・草花などが支持のない位置にあると **警告** を出す（貼り付け時に落ちて消えるため）。

```text
Blueprint is valid.

WARNING [3, 5, 2] minecraft:lantern[hanging=true]
  Needs a solid block above; found minecraft:oak_slab[type=top].

1 warning found.
```

- 座標は Blueprint 座標。`found` は支持先にあったブロック。
- 警告があっても終了コードは `0` で `.schem` は書き出されるが、AI は警告をすべて解消してから報告する。`--strict` を付けると警告で終了コード `1` になり、`build` は書き出さない。
- チェック対象は代表的なブロックに限られる。警告がないことは物理的に正しいことの保証ではない。

## デザインプリセット

`designs/` には様式ごとの Palette・構造ルール・寸法・装飾をまとめたプリセット（`medieval`, `japanese`, `modern`）がある。依頼にプリセット名や様式名を入れると、AI はそのファイルを読んでから Blueprint を書くため、毎回長い指示を書かずに済み、複数の建物で様式をそろえられる。

```text
medieval で直径 15、高さ 30 の塔を作って
```

- 使ったプリセットは Blueprint の `metadata.design` に記録される。
- 自分のプリセットは `designs/<name>.md` に置く（書き方は `designs/README.md`）。リポジトリで共有しない個人用は `designs/local/`（git 管理外）。
- 依頼者が「この建物と同じ様式で」と `.schem` を渡してきたら、`mcblueprint design <file> --name <name>` で `designs/local/<name>.md` を作り、内容を確認・修正してからそのプリセット名で建てる。パーツは `--part name=file` で部品にする。
- プリセットを書いたら `mcblueprint check designs/`（部品は `mcblueprint check components/`、部品の一覧は `mcblueprint components`）で検証する。CI でも同梱分を `--strict` で検査する。

## 既存の建物を元にする・修正を比較する

- 依頼者が `.schem` / `.litematic` を持っている場合は `mcblueprint import <file>` で `blueprints/<name>.json`（`fill` / `set` の列）に変換してから編集する。高レベル Operation には復元されないので、大きく作り替えるなら新しく書いたほうが早い。
- 依頼者のパーツ（窓枠・門・街灯など）は `mcblueprint import <file> --component --name <name>` で `components/<name>.json` にすると、`component` Operation で何度でも配置できる。
- 修正の前後で意図しない変更がないかは `mcblueprint diff old.json new.json` で確認する（`origin` 基準で比較し、air の有無やプロパティの省略は無視される）。

## Skill の配置

| エージェント | 場所 | 備考 |
|---|---|---|
| Codex | `.agents/skills/minecraft-blueprint/` | 正本 |
| Claude Code | `.claude/skills/minecraft-blueprint/` | `.agents` 側の複製。`tests/test_skill_sync.py` が一致を検証 |

Skill の内容は共通で、`SKILL.md`（ワークフローと守ること）と `references/`（Blueprint JSON 早見表、Operation 早見表、建築のコツ）からなる。Skill を更新するときは `.agents` 側を編集し、`.claude` 側へコピーする。

```bash
cp -r .agents/skills/minecraft-blueprint/. .claude/skills/minecraft-blueprint/
```

`AGENTS.md`（Codex）と `CLAUDE.md`（Claude Code。`AGENTS.md` に準じる旨のみ）には全体ルールだけを書き、仕様は複製しない。

## 他のエージェントで使う

Gemini CLI、Cursor、ローカル LLM などでも、次を守れば同じ Core を使える。

1. `docs/FORMAT.md` と `docs/OPERATIONS.md`（または Skill の `references/`）をコンテキストに渡す。
2. Blueprint JSON を `blueprints/` に書かせる。
3. `mcblueprint validate` → `mcblueprint build` を実行させ、エラー出力を読ませて修正させる。

## セキュリティ

- Blueprint は宣言的なデータであり、コマンドやスクリプトを実行する手段を持たない。未知のキーは検証で拒否される。
- エージェントが実行するコマンドは `mcblueprint validate` / `inspect` / `stats` / `preview` / `build`（既存 Schematic を元にするときは `import`、修正前後の比較は `diff`）で足りる。Blueprint に任意のコマンド文字列を書かせない。

## 明示対象の通行検査

`mcblueprint traverse FILE --building INDEX` でトップレベルbuildingの入口・各階到着点を、`--start X Y Z --end X Y Z` で足元セル間を検査できる。通常validateと別の限定モデルであり、未知形状はunsupportedとなる。ネストされたbuildingは変換後の明示座標で指定する。対応条件・結果・終了コードは [TRAVERSAL.md](TRAVERSAL.md) を参照。
## BuildSpecから住宅・塔を生成する

新規の住宅・塔は `mcblueprint archetypes` で対応語彙を確認し、
`mcblueprint spec-template house --minecraft-version <版>` または `tower` の出力を YAML に保存して編集します。
`mcblueprint make <spec.yaml> --emit blueprint` で生成・実測評価・単一出力まで実行できます。
診断に示された BuildSpec 項目だけを直し、失敗時はコードと要素IDを含む診断を確認します。
評価不合格の終了コード3でも成果物と failed 状態の run 記録が出ます。
柱・梁の欠損（E101）は終了コード1で公開を止めます。通常生成の欠損はエンジン不具合として報告し、
推測で設定を変えません。WARNのみは0、引数・入出力・資源不足は2です。
診断の `repairPaths` に従い、E201は奥行き・密度、E202は装飾密度、E203は欠け・該当override、
E305（WARN）は壁材の混合を調整します。指標と診断の契約は [EVALUATION.md](EVALUATION.md#makeの実測品質診断) を参照してください。
3回試行、同じ診断コードが連続2回改善しない、
または `unsupported` だけが残ったときは停止します。画像は必要な場合だけ
`mcblueprint preview <emitされたBlueprint.json> --views isometric` で確認します。Operation の詳細は
`mcblueprint help operations`、BuildSpec の手順は `mcblueprint help buildspec` を参照します。
細部の修正だけ、emitしたBlueprintを `validate`・`build` で扱います。

Skill 本体の長さは tiktoken 0.9.0 の `o200k_base` で **測定後に記録**します。
これは固定版 tokenizer による代理指標で、Claude の実トークン数ではありません。tiktoken 0.9.0 / o200k_base:
Codex 927 tokens、Claude Code 927 tokens（各2000以内）。
