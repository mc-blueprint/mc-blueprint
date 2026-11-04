---
name: minecraft-blueprint
description: Minecraft の建築を BuildSpec から生成し、評価診断に沿って調整する。住宅・塔・城郭の新規作成は mcblueprint CLI を使い、Blueprint の細部修正や既存 .schem / .litematic の編集では従来の Blueprint 手順を使う。
---

# Minecraft 建築

新しい住宅・塔・城郭は BuildSpec を入力し、配置・材質・評価を mcblueprint に任せます。Blueprint は細部の直接修正が必要な場合に使います。.schem / .litematic 自体を直接作成・編集しません。

## BuildSpec の手順

1. `mcblueprint archetypes` で対応語彙を見て、`house`・`tower`・`castle` の対応済みplannerから選びます。
2. `mcblueprint spec-template <archetype> --minecraft-version <版>` の出力を YAML ファイルに保存し、依頼内容を反映します。版は依頼者に確認するか、プロジェクトの既定に従います。
3. `mcblueprint make <spec.yaml> --emit blueprint` を実行し、診断と終了コードを確認します。
4. 評価失敗なら診断に明示された修正対象だけを変更して再実行します。画像は利用者が求めたとき、または数値評価で判断できない見た目の確認に必要なときだけ `mcblueprint preview <emitされたBlueprint.json> --views isometric` で使います。
5. 次のいずれかで試行を止め、結果と残る診断を利用者へ報告します。
   - make を3回実行した
   - 同じ診断コードが連続2回、改善しなかった
   - `unsupported` だけが残り、修正可能な根拠がない

終了コード0はFAILなし（WARNのみを含む）、3は品質不合格で、成果物と failed 状態の記録が残ります。1は入力・構造違反、2は引数・入出力・資源不足です。E101の通常生成での構造欠損はエンジン不具合として停止し、根拠なく寸法を変えません。

E201は `config.decoration.depth`・`density`、E202は `config.decoration.density`、E203は該当override・`config.weathering.missing_blocks`、E305（WARN）は `config.palette.mix` を、診断の `repairPaths` に従って調整します。対象なしの `n/a` と測定失敗を区別し、固定課題の品質合格を推定しません。実測値と構造化診断は出力隣の `.run.json` の `metrics`・`structuredDiagnostics` で確認できます。

診断に示された BuildSpec 項目だけを変えます。診断が対象や修正先を示さない場合は推測で無関係な項目を変えず、状況を報告します。画像や viewer は自動で開きません。

## Blueprint を直接扱う場合

生成済み Blueprint の局所的な細部修正、または既存 Blueprint の編集では、[format.md](references/format.md)、[building-guidelines.md](references/building-guidelines.md)、[operations.md](references/operations.md) を必要な箇所だけ参照します。完全な契約はリポジトリの `docs/FORMAT.md` と `docs/OPERATIONS.md` が正本です。Operation の詳細は `mcblueprint help <topic>` で取得します。検証・出力には `mcblueprint validate` と `mcblueprint build` を使います。

BuildSpec の仕様と CLI の詳細は `mcblueprint help buildspec`、`mcblueprint help materials`、`mcblueprint help resume` を必要なときだけ参照します。全体の手順は [docs/AI_GUIDE.md](../../../docs/AI_GUIDE.md) を参照してください。
