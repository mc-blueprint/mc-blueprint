# BuildSpec v1

BuildSpecは生成条件を記述するYAMLです。`minecraftVersion` は必須です。省略値はhouse、medieval、small、seed `0`、site `{terrain: none, biome: plains}`、interior `minimal` です。トップレベルのseedを省略した場合は、設定層のseedを使ってから既定値 `0` を適用します。

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

houseのfeatureは`floors`（1〜2）、`floor_height`（4以上）、`roof`（gable/hip/flat）です。towerは`shape`（round/square）、`floors`（正整数）、`floor_height`（4以上）、`battlement`（真偽値）です。アーキタイプごとに語彙を閉じ、未知キーは近い候補とともに拒否します。整数項目で真偽値は受理しません。

scaleの外形上限はtiny `[16,16,16]`、small `[32,32,32]`、medium `[64,48,64]`、large `[128,96,128]`、huge `[256,128,256]` です。`size: [x,y,z]` は軒などを含む外形上限としてscaleより優先します。各寸法は1〜256の整数です。階数と階高の指定がsizeの高さを超える場合は拒否します。

BuildSpecの`materials`は材質名（`stone`）、固定ブロックID（`minecraft:stone`）、または`{color: [L,a,b], material: ...}`です。object形式は`color`・`material`の少なくとも一方を指定します。色だけなら様式の当該役割の材質候補から選びます。固定IDは選び直さず、必要な別形状だけ同じfamilyから解決し、混色率を0にします。必要形状がなければエラーです。Lは0〜1、a/bは−0.4〜0.4です。YAML重複キー、未知の設定、型・範囲違反は読み込み時にエラーになります。材質・配色解決の詳細は [Materialize](OPERATIONS.md#materialize-前の役割入力) を参照してください。

生成はhouse・tower・castle・town/city・terrain・cave・underground_cityに対応します。
terrain単独の生成は [地形](TERRAIN.md#地形単独の生成) を参照してください。
指定数を黙って減らしたり、空計画を成功にしません。

| archetype | features（括弧内は既定値） |
|---|---|
| castle | curtain_wall / keep / gatehouse（true）、towers（非負整数、6）、moat（false） |
| town / city | buildings（正整数）、plaza / well / market（true） |
| terrain | overhangs / arches（true） |
| cave | entrances（正整数、1） |
| underground_city | buildings（正整数）、entrances（正整数、1）、supports / lights（true） |

buildings の規模別既定は tiny/small/medium/large/huge に対し 1/3/4/8/12。
size だけを指定した場合は small の既定数を使います。city は town と同じ planner を使います。
地形名 none/flat/hill/mountain/plateau/valley/island/cave はパス解決より先に判別します。
住宅・単独塔のmakeはnone/flatと既存terrain snapshotに対応します。
castle・town/cityは生成地形にも対応します（[城郭](CASTLE.md)・[街並み](TOWN.md)）。
cave・underground_cityはnone/flat/caveから主空洞と地下床を計画します
（[地下の生成](UNDERGROUND.md)）。
terrainはnone（flat扱い）/flat/hill/mountain/plateau/valley/islandを生成します。
terrain単独の既存地形ファイル編集とelementsによる地形パラメータ編集は対象外です。
furnished内装はDetail段で未対応として拒否します。

## Resolve API

`mcblueprint.buildspec.resolve_buildspec(source, ...)` はYAMLパスまたはBuildSpec辞書を受け取り、省略値・設定6層を合成した `ResolveResult` を返します。`result.buildspec` は正規化済みBuildSpec、`result.config` は合成設定、`result.origins` は設定葉ごとの出所です。`source=None` は設定のみをResolveします。生成処理はこのAPIを呼び、`config show` も同じAPIを使用します。

```console
mcblueprint config show --set weathering.age=0.4
```

スキーマ正本は `schema/buildspec.schema.json`、wheel同梱コピーは `src/mcblueprint/schema/` です。

## 住宅・塔の計画 API

`mcblueprint.planning.plan_buildspec(resolve_result)` は `ResolveResult` を受け取り、
`PlanningResult` を返します。入力は変更せず、ブロック生成や材質抽選は行いません。
`site`・`massing`・`detail` は `formatVersion: 2` のJSON辞書、`blueprint` は
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
配色関係の診断を計画の成功だけで合格扱いにしません。CLIの `make` は計画・材質・領域生成・
Weather・最小評価・出力を接続します。従来の `generate` は共有Randomによる抽選を
維持するため、混色時に `generate_regions` と同じ配置になる契約ではありません。

### 寸法・構造・様式

- 住宅は一階、塔は三階が既定です。階高は同梱様式の `grid.story_height` の下限です。
  住宅の幅は `2 * bay + 1`、奥行きは `2 * bay + 3` と階段に必要な `階高 + 4` の大きい方
  （`bay` は様式の下限）です。塔の半径は4以上で、直進階段と着地余白が収まる寸法です。
- `size` は中央配置する敷地と外形の上限です。屋根の軒、塔の張出し・胸壁、階段の頭上空間も
  検査し、不足時はMassing段のエラーにします。指定寸法・階数を縮めて成功にはしません。
- 住宅は既存building/room/roofを再利用します。medieval・japaneseは既定のgable、modernは既定のflatを一枚の屋根として計画します。`features.roof` はgable/hip/flatを受け付けます。塔は既存towerの外殻・床・開口・屋上床を再利用し、`battlement: false` なら
  胸壁を省略します。BuildSpecの階数をtower Operationの階間隔へ換算します。
- 塔の上下階の直進階段は左右に分け、上階階段の土台で下階の頭上を塞がないようにします。
  既存螺旋階段の出力は変更しません。入口と全階・屋上の到着点をMassingに記録します。
- 構造グリッドの柱と各階の梁は `structure`、入口・階段・到着点の空間とその支持床・踊り場は
  同じ通路の責務で `circulation` として予約します。
  Detailの窓は梁と柱を避けます。`minimal` は壁際の床に最小照明を置き、`none` は照明を省略します。
  構造上の床・階段は両方で維持します。`furnished` はResolveで未対応診断になります。
- Massing JSONと`metadata.planning`の各棟には、Criticが比較する`columns`（安定IDと期待上下端）、
  `beams`（安定IDと高さ・範囲）、`facades`（安定ID、外向き法線、原点、u/v軸、範囲、開口投影）を
  記録します。住宅の柱間は4を基本にし、窓・入口・階段と重なる候補は除き、外壁の内側へ柱を
  配置します。塔の柱は実際の壁輪郭上に置き、隣接柱間を向き付き面区間として表します。
  回転してもIDは変わらず、座標と面軸は建物とともに回転します。
- Detailは帯・コーニスを含む面ごとの非開口面積から装飾予算を求め、帯とベイの凹凸へ
  配分します。小さい面の帯は予算内へ間引き、部品は原寸のまま階の上端に揃えます。
  付け柱の基部を分節し、部品の装飾部位と基本施工の付け柱を様式の`depth_layers`内で選びます。
  屋根縁による上書きも考慮し、4×4の局所投影で凹凸が不足する箇所を優先します。
  構造・通路予約と開口を保護し、密度上限を超えて凹凸を追加することはありません。
  機能開口は`depth_layers.opening`の最大値に`config.decoration.depth`を掛け、
  `floor(value + 0.5)`で整数化した分だけ内側へ後退します（北面では+z）。
  基準面の開口投影と、後退先までの空間は維持します。部品全体は移動しません。
  後退域が構造・通路予約に重なる開口は後退量を0にし、`opening_recess_blocked`診断を
  Pythonの警告として出し、対象Operationの`comment`にもJSONで保存します。
  診断は対象要素、実効量・要求量、理由、修正先`config.decoration.depth`を含みます。
  整数ブロック数で様式の密度範囲を満たせない面は、Detail段で収容不足として拒否します。
  `DetailError.diagnostic`と例外文中のJSONは`detail_capacity`、対象面、非開口面積・上限個数、
  期待密度範囲、修正先（`elements.<root>`の既知寸法と`size`）を持ちます。
  `make`の終了コードは従来どおり1です。
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

## make と最小評価

```console
mcblueprint archetypes
mcblueprint spec-template house --minecraft-version 26.3
mcblueprint make house.yaml --emit blueprint
mcblueprint make tower.yaml --format litematic --seed 73 -o output/tower.litematic
mcblueprint make house.yaml --set weathering.age=0.4
mcblueprint help operations
```

`spec-template` は版を明示する約30行のコメント付きYAMLを標準出力へ返します。
`help buildspec|operations|materials|resume` は仕様の参照先を返します。
`make` は合成済みの `output.format`・`output.directory`・`output.emit` を使い、
CLIの `--format`・`--seed`・`--set KEY=VALUE` を最上位の設定として適用します。
`-o` はファイルまたはディレクトリです。既定は `output/<spec名>.schem` です。

各coreを一度だけ生成し、`required_halo()` を参照半径にしてWeatherを適用します。
完成coreを一時spoolへ保存してから支持検査をcoreだけ集計し、入口→各階→屋上の順に
既存traversalで確認します。全体のBlockVolumeを復元せず、行キャッシュで領域を跨いで読みます。
探索状態にも `limits.max_memory_mb` から上限を設定し、超過は資源不足として失敗します。
元地形snapshotは不変の領域spoolとして読み込み、造成・道路の列計画を通常Operationより前に
各領域へ適用します。単独建物は元地表の最高Yの1セル上を床にし、従来と同じ造成条件で検査します。
版・施工範囲・保護域と入力hashを検証し、入力配列が予算に収まらない場合は生成前に拒否します。

診断は1件1行で入力パス・要素IDを含みます。supportとwalkability、Materializeの
`palette_relation` を評価し、traversalの `blocked` と `unsupported` を区別します。
柱連続率・平坦率・装飾密度・外壁混色率の正式集計は `not-implemented` と表示し、合格に数えません。
Weatherの遷移診断はコードごとの件数をWARNとして返します。

同梱配色パックがあればmakeが一度検証して読み込み、archetype・style・site.biomeで
分布を選びます。役割ごとの固定ID、明示材質/色、パック、D28の順を守り、分布欠損だけを
D28へ戻します。パック不在は従来どおり生成し、破損・hash不一致・ライセンス欠損は
計画前にエラーとします。実パックの同梱は別工程です。

パック使用時はrunの `colorpack` に版・hash・採用条件・関係検査・試行数・面積上限を残し、
`palette_relation` にpack/d28別の適合数と分母を記録します。統計規則なしはpack評価n/aです。
Weather後のcoreだけから建物別に対象role/全役割セルの面積比を測り、
上限×`palette.accent` の超過や固定IDによる関係の解なしは成果を保存して終了3とします。
詳細は [COLORPACK.md](COLORPACK.md) を参照してください。

終了コードは合格 `0`、入力・構造予約の違反 `1`、引数・I/O・資源不足 `2`、生成後の評価不合格 `3`。
`3` でも成果物を保存します。隣の `<出力名>.run.json` に合否・診断・合成設定・出所・seed・
入力hash・engine版/hash・データlock/hashを記録します。出力は既存の原子的Exporterを使い、
完成まで既存ファイルを置き換えません。Pythonの `make` はsceneを保存し、表示せず
`preview=not-run` を返します。CLIは生成後に `preview.enabled`・`preview.mode` に従い、
既定で3D viewerを起動します。`preview.open_browser=false` はブラウザを開かずURLを表示し、
Ctrl+Cまでサーバーを保持します。終了後に生成結果の `0` または `3` を返します。
`--set preview.enabled=false` で表示を省略し、`--set preview.mode=2d` で
出力先の `preview/` に既存の4視点PNGを保存します（preview extraが必要）。
表示失敗は生成済み成果を残して終了 `2` とし、runの評価 `status` と別に
`preview`（`disabled` / `2d` / `3d` / `failed`）と失敗時の `previewError` を記録します。

## 中間出力と再開

```console
mcblueprint make house.yaml --emit blueprint -o output/house.schem
mcblueprint make house.yaml --resume output/house.detail.json -o output/resumed.schem
mcblueprint validate output/house.blueprint.json
mcblueprint build output/house.blueprint.json -o output/rebuilt.schem
```

`--emit resolve|site|massing|detail|blueprint` は複数指定できます。要求した最も後の段までの
前段も出力隣へ `<出力stem>.<段>.json` として保存します。各ファイルの
`.json.meta.json` sidecarは形式版1、段名、入力・基点パス、入力hash、合成設定hash、
engine版/hash、lock/hash、前段hash、再開用contextとそのhashを持ちます。
JSONとsidecarはそれぞれ一時ファイルから置換します。中断で片方だけ更新された組はhash検査で拒否します。
計画APIはSite/Massing/Detailを一度に返すため、この三段のsidecarにはその計画結果を保持します。
Resolveは計画前に、計画各段はMaterialize前に保存するので、後段失敗の調査にも利用できます。

`--resume` でも元のBuildSpecが必要です。Resolveで現在の合成設定を一度確認し、入力・設定・
前段の連鎖・engine・データに差があれば拒否します。Site以降は保存済み計画を使い、
Blueprintからの再開ではMaterializeも省略します。Generateは常に一度だけ実行します。
配色パック使用時はパック・語彙・DATA_LICENSEの版と実内容hashも世代に含め、
同版の内容変更でもresume・apply-edits・viewer再生成を拒否します。
旧cacheの移行や同じ出力先を共有する実行の排他は提供しません。

emitしたBlueprintは具体ブロック／既存Paletteと `metadata.generation` を持ちます。
このmetadataは `mode: buildspec-v1`、合成設定 `config`、基準面 `groundHeight` を保持します。
`groundHeight` は `{ "type": "constant", "value": Y }` または
`{ "type": "terrain", "inputHash": SHA256 }`。後者は造成前の元地形の(x,z)最上面を
領域ごとに参照します。地形入力の `terrainHash` も保存し、元ファイルが変わった場合は
Blueprint build・resumeを拒否します。地形付きの古いcheckpointは再生成してください。
`build` はこれがある場合に限り領域生成とWeatherを使い、makeと同じ配置を再現します。
metadataがない従来Blueprintの乱数・生成方式は維持します。出力形式内の時刻や圧縮ヘッダーの
バイト一致は保証しません。emit後のOperation編集は `validate`・`build` で扱い、
入力hashが変わった中間ファイルからのresumeはできません。make由来Blueprintのbuildは単一出力のみです。

公開APIは `mcblueprint.pipeline.make(source, *, output, emit, resume, overrides, ...)`。
`MakeResult` は出力・run記録・終了コード・診断を返し、失敗は段と終了コードを持つ
`PipelineError` を送出します。`stage_hook(stage)` は段開始のローカル計測用で、
Generateには領域ごとのWeatherと退避時間を含みます。Critic・Exportは別に通知します。
