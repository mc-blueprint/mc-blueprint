# 設定 v1

## 地形の寸法換算（H04計画API）

`terrain_field.plan_terrain` はterrain設定から全体2D場を確定する。
地形帯Hは `size.y−1−clearance`。flat/hill/mountain/plateau/valley/islandの
最大形状起伏はそれぞれHの0/0.35/0.85/0.60/0.50/0.55倍にamplitudeを掛ける。
amplitude=0は平坦。波長は `max(2, noise.scale×(2−1.5×roughness))`、
octaves/persistenceノイズは起伏の0.25倍。erosion=0は無処理、正値は全体粗格子の
水滴侵食を適用する。水位nullは島でHの0.25、他は0.15（世界座標の底面を加算）。
明示水位は地形帯内の整数。terrain.vegetationは敷地の木/草候補で、
weathering.vegetationとは別。makeへの接続・洞窟生成は後続Issueで行う。
方式・再開用hash・表層計画の契約は [TERRAIN.md](TERRAIN.md) を参照。

設定は `schema/config.schema.json` を正本とし、wheel 同梱用コピーを `src/mcblueprint/schema/` に置きます。`config.yaml` の重複キー、未知キー、型・範囲違反は拒否されます。

## 優先順位

組込み既定値 → BuildSpec の `style` が参照する同梱様式の役割別材質候補 → 利用者設定（`%APPDATA%/mc-blueprint/config.yaml`。未設定時は `~/.config/mc-blueprint/config.yaml`）→ cwd の `mc-blueprint.yaml` → BuildSpec の `config`・`materials`・`seed` → CLI `--set`。未知の様式は利用可能な同梱様式名を示してエラーになります。

辞書は再帰的に合成し、配列とスカラー値は上位層で置換します。BuildSpecの `materials` と `config.palette.materials` は役割ごとに合成し、同じ役割が異なる材質を指定するとエラーになります。seedは明示値を合成した後に省略時の `0` を適用します。水の `radius` と地面からの `height` は整数です。Python API `resolve_config` は `ResolvedConfig(values, origins)` を返し、各設定葉の出所も提供します。

`decoration.density` と `decoration.depth` は0〜1の強度、`decoration.symmetry` は対称配置を共有する割合です。Detailは様式の密度範囲を `min + density × (max − min)` で選択し、固定モジュールがベイの寸法に合わない場合は基本壁と開口を維持します。

奥行きは役割ごとに様式の層へ写し、`floor(value + 0.5)` で整数化します。外付け材を壁の外へ配置し、必須開口・通路は移動しません。depth=0では任意部品を省略できます。同じ階の部品系列を揃え、対向するベイの選択をsymmetryの割合で共有します。配置後に密度を測り、予約衝突後に不足分を追加する調整はしません。

```console
mcblueprint config show --set weathering.age=0.4
```

表示結果は `mcblueprint.buildspec.resolve_buildspec()` が返す合成設定と出所を JSON で出力します。#151以降の生成処理も同じ公開APIを呼びます。設定値のスキーマは `config.schema.json` を参照してください。
