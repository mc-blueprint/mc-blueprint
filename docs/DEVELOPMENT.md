# 開発ガイド

## セットアップ

Python 3.11 以上が必要です。

```bash
python -m venv .venv
```

```bash
pip install -e ".[dev,preview]"
```

`preview` は Pillow を使うプレビュー画像機能の任意依存（利用者は `pip install -e ".[preview]"`）。

## 日常のコマンド

```bash
ruff check .
```

```bash
pytest
```

```bash
mcblueprint --version
```

```bash
mcblueprint validate blueprints/house.json
```

```bash
mcblueprint build blueprints/house.json
```

```bash
mcblueprint inspect blueprints/house.json
```

```bash
mcblueprint stats blueprints/house.json
```

```bash
mcblueprint preview blueprints/house.json
```

```bash
mcblueprint preview blueprints/house.json --layers all --grid 5
```

```bash
mcblueprint versions
```

```bash
mcblueprint check
```

```bash
mcblueprint components
```

`import <file.schem|.litematic>` は既存の Schematic を `blueprints/<name>.json`（`fill` / `set` の列）に変換する。`--component` を付けると、窓枠や街灯などのパーツを部品ファイル `components/<name>.json`（最小コーナーが原点）として書き出し、`component` Operation で配置できるようにする（`--name` で部品名、`--description` で説明）。

`design <file.schem|.litematic|.json>` は既存の建築の様式（役割ごとの Palette、壁の厚さ、階高、屋根の形、窓の寸法と間隔、入口、照明）を推定してデザインプリセット `designs/local/<name>.md` を書く。`--part NAME=FILE` でパーツを部品に変換して md から参照し、`--reference` で建物全体を `components/<name>_reference.json` にする。窓や入口の周りに枠・窓台・アーチなどの装飾があれば `components/<name>_window.json` / `<name>_door.json`（2 種類目以降は `_window2` …）として切り出し、md から参照する（`--no-parts` で省略）。`--no-views` で参考図（平面図・立面図）を省く。書き出し後に `check` と同じ検証を行う。

`check [PATH ...]` はデザインプリセット（`*.md` の ```json ブロック）と部品（`*.json`）を検証する。`patterns/`（`styles/` を含むディレクトリ）を1つ指定した場合はパターン v1 の様式 YAML・モジュール JSON・`sources.yaml` をスキーマと相互参照で検証する。様式名とファイル名、モジュール名と配置の一致、参照先モジュールの存在を確認する。役割参照を含むモジュールは Materialize 前のため検証エラーとなる。終了コードは成功 `0`、データ検証エラー `1`、引数・入出力エラー `2`。既定では最も古い同梱バージョンのブロックデータで検証する（`--minecraft-version` で変更）。`components` は探索パス上の部品の名前・大きさ・説明を一覧する（`--json` 可）。

`preview` は既定で `preview/<名前>-top.png` / `-north.png` / `-east.png` / `-isometric.png` を出力する。`--views` で面を選び、`--layer 3`（複数可）や `--layers 1..5` / `--layers all` で高さごとの水平断面（`-y03.png` など。1 つ下の段を薄く重ねる）を出力する。断面だけ欲しいときは `--views` を省略する。`--grid 5` で 5 ブロックごとの罫線と座標ラベル、`origin` の赤い印を付ける。色が登録されていないブロックは灰色になり、ID を `WARNING` で表示する（`src/mcblueprint/data/colors.json` に追加する）。

局所確認は `mcblueprint preview blueprints/house.json --from 3 0 -1 --to 7 4 2 --views isometric,north --grid 1` のように指定する。`-region-` の画像と座標情報 JSON が全体画像と別に出る。生成処理は全体のままで、切口の扱いと API は [PREVIEW_REGIONS.md](PREVIEW_REGIONS.md) を参照する。

`build` は `output/<入力ファイル名>.schem` に書き出す。`-o` で出力先ファイルまたはディレクトリ、`--seed` で Palette の seed、`--max-dimension` で最大寸法、`--minecraft-version` で対象バージョン（同梱データのあるもの。`versions` で一覧）を変更できる。終了コードは `0` 正常 / `1` 検証エラー / `2` 引数・入出力エラー。

## ディレクトリ構成

```text
src/mcblueprint/   パッケージ本体（CLI, Loader, Validator, Generator, Exporter）
schema/            Blueprint JSON Schema
docs/              仕様書・ガイド（仕様の正本）
examples/          サンプル Blueprint
blueprints/        利用者・AI が作成する Blueprint（作業用）
output/            生成された .schem（git 管理外）
tests/             pytest
.agents/skills/    Codex 用 Skill（正本）
.claude/skills/    Claude Code 用 Skill（.agents 側のコピー）
```

## JSON Schema の更新

Blueprint JSON Schema の正本は `src/mcblueprint/schema/blueprint.schema.json`（パッケージに同梱され、実行時に `mcblueprint.schema.load_schema()` で読む）。
リポジトリ直下の `schema/blueprint.schema.json` はエディタや外部ツール向けのコピーで、`tests/test_schema.py` が両者の一致を検証する。変更時は両方を更新する。

## Minecraft での手動確認

`examples/` の Blueprint は `tests/test_examples.py` で validate / build が通ることを検証しているが、見た目やブロックの向きは実際に Minecraft へ貼り付けて確認する。

1. `mcblueprint build examples/house.json` を実行し、`output/house.schem` を生成する。
2. WorldEdit を導入したサーバーまたはシングルプレイの `config/worldedit/schematics/`（Fabric / NeoForge）または `plugins/WorldEdit/schematics/`（Paper 系）へコピーする。
3. ゲーム内で `//schem load house` → `//paste` を実行する。Blueprint の `origin` がプレイヤーの位置に一致する。
4. 形状、階段・ドア・ガラス板の向きと接続、Palette の混ざり具合を確認する。

Litematica を使う場合は `.schem` をそのまま読み込めるほか、`mcblueprint build examples/house.json --format litematic` で `.litematic` を生成し、ゲームディレクトリの `schematics/` に置いて Litematica の「Load Schematics」から読み込める。

## ブロックデータの再生成

ブロック ID・プロパティ・デフォルト状態の検証に使うデータは `src/mcblueprint/data/blocks/<version>.json`、DataVersion は `src/mcblueprint/data/versions.json` にあり、どちらもコミット済み（利用者に Java は不要）。同梱バージョンの一覧と差分は [VERSIONS.md](VERSIONS.md) にまとめる。
新しい Minecraft バージョンを追加するときは Java を用意し、次を実行して生成物をコミットする。`1.21.x` のサーバーは Java 21、`26.x` のサーバーは Java 25 が必要で、`--java` で実行ファイルを指定できる（省略時は `PATH` の `java`）。

```bash
python scripts/generate_block_data.py --version 1.21.11 --download
python scripts/generate_block_data.py --version 26.3 --download --java "C:/Program Files/Eclipse Adoptium/jdk-25.0.4.7-hotspot/bin/java.exe"
```

対応バージョンの一覧は `src/mcblueprint/data/versions.json` が正となり、`mcblueprint versions` で表示できる。Minecraft Java Edition は 2026 年から `26.3` のような年ベースの採番へ移行しているが、スクリプトは Mojang の公式マニフェストに載っているバージョン ID をそのまま受け付ける。追加後は `tests/test_blockdata.py`（`versions.json` と `blocks/` の対応、DataVersion の単調増加、旧バージョンのブロックが残っていること）と `tests/test_examples.py`（全バージョンで `examples/` が検証を通ること）が自動で検査する。

`--download` は Mojang の公式マニフェストから `server.jar` を `generated/` に取得し SHA-1 を検証する。手元の `server.jar` を使う場合は `--server-jar <path>` を指定する。スクリプトは公式データジェネレータ（`--reports`）を実行し、`reports/blocks.json` を圧縮形式へ変換する。`generated/` は git 管理外。

## 開発フロー

`develop` を開発用・デフォルトブランチ、`main` をリリース用とする。通常の開発 PR は `develop` 宛てとし、`main` への反映やリリース公開はユーザーの明示指示がある場合だけ行う。

1. 対象の Issue を確認し、範囲を把握する。
2. fetch して最新の `origin/develop` と依存 commit を確認し、タスク専用 worktree に `feature/issue-<番号>` ブランチを作成する。
3. 実装とテストを追加する。仕様変更を伴う場合は `docs/`、`schema/`、Skill の references も同じ変更に含める。
4. `ruff check .` と `pytest` を通す。
5. 許可された commit・push を行い、`develop` へ Pull Request を作成する。

依存 Issue がある場合は前提の確定 commit を記録し、別ブランチ・worktree・PR で扱う。Git 操作の権限と worktree の作成・引継ぎ・削除は [AGENTS.md](../AGENTS.md) に従う。ユーザーが Issue を作成しない直接作業などの例外手順を明示した場合は、その依頼範囲で優先する。

## コミットメッセージ

日本語で、以下の形式にする。

```text
type: 変更内容の概要

- 具体的な変更内容
- 具体的な変更内容
```

`type` は `feat` / `fix` / `refactor` / `docs` / `test` / `build` / `chore` から、変更の主目的に合うものを 1 つ選ぶ。

## Pull Request

タイトルはコミットと同じ `type: 概要` 形式。本文は以下の見出しを使う。

```markdown
## 概要

## 変更内容

## 検証

- [ ] 関連テスト
- [ ] ビルド
- [ ] 必要な実行確認

## 関連Issue

Closes #<番号>
```

検証欄には実際に行った確認だけを `[x]` にする。

## CI

GitHub Actions（`.github/workflows/ci.yml`）が `develop` / `main` への push と PR ごとに Python 3.11 と 3.12 で `ruff check .` と `pytest` を実行する。
