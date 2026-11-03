# BuildSpec v1

BuildSpecは生成条件を記述するYAMLです。`minecraftVersion` は必須です。省略値はhouse、medieval、small、seed `0`、site `{terrain: none, biome: plains}`、interior `minimal` です。

```yaml
minecraftVersion: "26.3"
features:
  floors: 2
  floor_height: 5
  roof: gable
materials:
  wall_main:
    color: [0.62, 0.04, -0.08]
    material: stone
```

houseのfeatureは`floors`（1〜2）、`floor_height`（4以上）、`roof`（gable/hip）です。towerは`shape`（round/square）、`floors`（正整数）、`floor_height`（4以上）、`battlement`（真偽値）です。アーキタイプごとに語彙を閉じ、未知キーは近い候補とともに拒否します。整数項目で真偽値は受理しません。

scaleの外形上限はtiny `[16,16,16]`、small `[32,32,32]`、medium `[64,48,64]`、large `[128,96,128]`、huge `[256,128,256]` です。`size: [x,y,z]` は軒などを含む外形上限としてscaleより優先します。各寸法は1〜256の整数です。階数と階高の指定がsizeの高さを超える場合は拒否します。

BuildSpecの`materials`は材質名（`stone`）、固定ブロックID（`minecraft:stone`）、または`{color: [L,a,b], material: ...}`です。object形式は`color`・`material`の少なくとも一方を指定します。色だけなら様式の当該役割の材質候補から選びます。固定IDは選び直さず、必要な別形状だけ同じfamilyから解決し、混色率を0にします。必要形状がなければエラーです。Lは0〜1、a/bは−0.4〜0.4です。YAML重複キー、未知の設定、型・範囲違反は読み込み時にエラーになります。材質・配色解決の詳細は [Materialize](OPERATIONS.md#materialize-前の役割入力) を参照してください。

現行Cではhouseとtowerを扱います。castleはMassing段で未対応として拒否し、城郭計画はHマイルストーンです。furnished内装はDetail段で未対応として拒否し、Dマイルストーンです。地形生成のhill/mountain/island/caveはSite段で未対応として拒否し、Hマイルストーンです。none/flatと既存terrain snapshotを指定できます。

## Resolve API

`mcblueprint.buildspec.resolve_buildspec(source, ...)` はYAMLパスまたはBuildSpec辞書を受け取り、省略値・設定6層を合成した `ResolveResult` を返します。`result.buildspec` は正規化済みBuildSpec、`result.config` は合成設定、`result.origins` は設定葉ごとの出所です。`source=None` は設定のみをResolveします。生成処理はこのAPIを呼び、`config show` も同じAPIを使用します。

```console
mcblueprint config show --set weathering.age=0.4
```

スキーマ正本は `schema/buildspec.schema.json`、wheel同梱コピーは `src/mcblueprint/schema/` です。

## 住宅・塔の計画 API

`mcblueprint.planning.plan_buildspec(resolve_result)` は `ResolveResult` を受け取り、
`PlanningResult` を返します。入力は変更せず、ブロック生成や材質抽選は行いません。
`site`・`massing`・`detail` は `formatVersion: 1` のJSON辞書、`blueprint` は
Materialize前の役割参照を含むBlueprintです。`palette` は同梱様式の役割定義を含む
Resolveの配色設定のコピーです。`layout` は住宅の `BuildingLayout`、塔は
`None` です。`diagnostics` は計画の追加診断（現行は空）で、入力エラーは
`BlueprintError` として返します。

```python
from mcblueprint.buildspec import resolve_buildspec
from mcblueprint.generator import generate_regions
from mcblueprint.loader import load_blueprint_dict
from mcblueprint.materialize import materialize
from mcblueprint.planning import plan_buildspec

resolved = resolve_buildspec("house.yaml")
plan = plan_buildspec(resolved)
spec = resolved.buildspec
materials = materialize(
    plan.blueprint, style=spec["style"], biome=spec["site"]["biome"], palette=plan.palette,
)
blueprint = load_blueprint_dict(materials.blueprint)
for region in generate_regions(blueprint):
    # 後段はcoreを消費し、この領域を解放する。
    pass
```

Materializeの `roles`・`diagnostics`・`max_area_ratios` は呼出し元で保持してください。
配色関係の診断を計画の成功だけで合格扱いにしません。CLIの `make`・段階保存／再開・
Weatherとの接続は後続Issueの範囲です。従来の `generate` は共有Randomによる抽選を
維持するため、混色時に `generate_regions` と同じ配置になる契約ではありません。

### 寸法・構造・様式

- 住宅は一階、塔は三階が既定です。階高は同梱様式の `grid.story_height` の下限です。
  住宅の幅は `2 * bay + 1`、奥行きは `2 * bay + 3` と階段に必要な `階高 + 4` の大きい方
  （`bay` は様式の下限）です。塔の半径は4以上で、直進階段と着地余白が収まる寸法です。
- `size` は中央配置する敷地と外形の上限です。屋根の軒、塔の張出し・胸壁、階段の頭上空間も
  検査し、不足時はMassing段のエラーにします。指定寸法・階数を縮めて成功にはしません。
- 住宅は既存building/room/roofを再利用します。medieval・japaneseは既定のgable、明示のhipに
  対応し、modernは既定のflatを一枚の屋根として計画します。`features.roof` の明示値は従来どおり
  gable/hipです。塔は既存towerの外殻・床・開口・屋上床を再利用し、`battlement: false` なら
  胸壁を省略します。BuildSpecの階数をtower Operationの階間隔へ換算します。
- 塔の上下階の直進階段は左右に分け、上階階段の土台で下階の頭上を塞がないようにします。
  既存螺旋階段の出力は変更しません。入口と全階・屋上の到着点をMassingに記録します。
- 四隅の柱と各階の梁は `structure`、入口・階段・到着点の空間とその支持床・踊り場は
  同じ通路の責務で `circulation` として予約します。
  Detailの窓は梁と柱を避けます。`minimal` は壁際の床に最小照明を置き、`none` は照明を省略します。
  構造上の床・階段は両方で維持します。`furnished` はResolveで未対応診断になります。
- 材質の役割定義は同梱patternsの各様式から読みます。計画側に様式別の補完を持たず、
  Resolveの優先順に従って利用者指定材を反映します。同梱元commitとSHA-256は
  `src/mcblueprint/data/lock.json` の `patterns` に記録します。

`site.terrain: none` と `flat` は床Y=0の基準面に配置します。snapshotを指定すると、その取得領域の
中央へ配置し、元地形の最上面の一段上を床にします。既存terrain/foundation境界へ委譲し、安定地盤材・
探索帯・支持・施工範囲をloaderで検査します。基礎の造成材は既存foundationが扱えるstoneです。
snapshotのMinecraft版と敷地寸法の不一致はSite段で拒否します。元地形を含む施工は
`generate_regions` が未対応なので、従来の `generate` を使用してください。

### 要素編集とブロック上書き

ルートIDは `house#1` / `tower#1` です。柱梁は `.frame`、住宅の屋根は `.roof`、
階段・開口・床は安定した接尾辞を持ち、各Operationへ `id`・`element`・`role`・`source` を付けます。
同じBuildSpec・seedでIDと配置が再現します。住宅の外壁には面の向きも付けます。

`elements` で編集できるIDは現行ではルートだけです。消えたIDや下位ID、未知パラメータは
Massing段で明示エラーにし、無視しません。

| ID | 編集可能なパラメータ |
|---|---|
| `house#1` | `width`、`depth`、`floors`、`floor_height`、`roof`、`angle` |
| `tower#1` | `radius`、`height`、`floors`、`floor_height`、`shape`、`battlement`、`angle` |

`angle` は0/90/180/270度です。住宅の階数は1〜2、屋根はgable/hip/flat、塔の形はround/squareです。
塔の `height` は壁高で `floors * floor_height - 1` と一致する必要があります。高さだけを編集した場合は
`(height + 1) / floors` を新しい階高にし、割り切れない場合・階高4未満は拒否します。
塔の半径の明示値は4以上かつ `floor_height <= 2 * radius - 4` を満たす必要があります。
省略した半径だけは階高に応じて決定します。いずれの変更も最後に外形上限を再検査します。

`overrides` は契約どおり `{id, at: [x,y,z], block}` または
`{id, region: [[x,y,z],[x,y,z]], block}` を受け取ります。sceneローカル座標を計画の最小座標に
加算します（snapshot施工時はsnapshotの取得領域が基準）。同一ID・同一内容は一度だけ適用し、
同一IDの異なる内容・範囲外・不正座標はDetail段のエラーです。予約と異なる責務で最後に配置するため、
予約セルとの衝突はGenerate時の `BlueprintError` になります。予約を回避する目的で上書きを黙って
skipすることはありません。scene/editsの対象要素照合やCLIとの接続は後続で扱います。

固定課題の入力は `tests/data/buildspec/single-house.yaml` と `standalone-tower.yaml` です。
要求文と必須条件は `evaluation/cases/fixed-cases.json` のまま記載し、課題の意味を変更しません。
