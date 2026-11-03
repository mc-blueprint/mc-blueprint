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

BuildSpecの`materials`は材質名または`{color: [L,a,b], material: ...}`です。Lは0〜1、a/bは−0.4〜0.4です。YAML重複キー、未知の設定、型・範囲違反は読み込み時にエラーになります。

現行Cではhouseとtowerを扱います。castleはMassing段で未対応として拒否し、城郭計画はHマイルストーンです。furnished内装はDetail段で未対応として拒否し、Dマイルストーンです。地形生成のhill/mountain/island/caveはSite段で未対応として拒否し、Hマイルストーンです。none/flatと既存terrain snapshotを指定できます。

## Resolve API

`mcblueprint.buildspec.resolve_buildspec(source, ...)` はYAMLパスまたはBuildSpec辞書を受け取り、省略値・設定6層を合成した `ResolveResult` を返します。`result.buildspec` は正規化済みBuildSpec、`result.config` は合成設定、`result.origins` は設定葉ごとの出所です。`source=None` は設定のみをResolveします。生成処理はこのAPIを呼び、`config show` も同じAPIを使用します。

```console
mcblueprint config show --set weathering.age=0.4
```

スキーマ正本は `schema/buildspec.schema.json`、wheel同梱コピーは `src/mcblueprint/schema/` です。
