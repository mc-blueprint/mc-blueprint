# 配色パック v1

color-statsが生成し、mc-blueprintが受け取る配色統計の契約。
正本は `src/mcblueprint/schema/colorpack.schema.json`、`schema/colorpack.schema.json`
は外部ツール向けの同一コピー。公開語彙と対応表は
`src/mcblueprint/data/colorpack-vocabulary.json` を正本とする。
既存依存のjsonschemaで構造を検証し、Pythonで意味を検証する。依存の追加はない。

makeは検証済み同梱パックをMaterializeへ渡し、生成・評価・再開へ接続する。
実パックの同梱は別工程とし、Blueprint/BuildSpecの保存形式は変わらない。
未リリースの旧snake_case形式と `block_color` は受け付けず、移行処理も持たない。

## 版と公開語彙

トップレベルは `formatVersion: 1`、SemVerの `packVersion`、
`taxonomyVersion: "0.1.0"`、`vocabularyVersion: "1.0.0"`、
`palettePrior`、`materialPrior`、`relationRule`、`accentRule`、`provenance` を必須とする。
4種類の配列は空を許す。未知キー・nullによる分布の代用・非有限数は拒否する。
taxonomyとvocabularyは同梱表に一致する組だけを受け付ける。

taxonomyの出典はmc-blueprint-color-statsのcommit
`d63a596667e58c545d8fb762e8699a8c3af94a9c` の `taxonomy/scene.yaml`、
`taxonomy/parts.yaml`、`taxonomy/terrain.yaml`。暫定 `taxonomy/mcblueprint.yaml` は使わない。
元分類の意味変更はtaxonomy版、対応の意味変更はvocabulary版を上げる。
生成側は本変更の確定commitからschemaと語彙表を取得し、両版を記録する。

- styleMap: medieval/japanese/modernから同名のtaxonomy値への対応。
- buildingTypeMap: house/tower/castleから同名のtaxonomy値への対応。
  castleは輸出語彙であり、生成対応の宣言ではない。
- biomeMap: 名前空間なしのMinecraft biome厳密IDからtaxonomyの16分類への明示対応。
  洞窟biomeはmountains、meadowはplains、groveはsnowyとする。
  the_voidなど未掲載IDはbiomeLikeを付けない。着色用biome判定とは独立して扱う。
- town/city/terrain/cave/underground_cityなど未対応の建物はotherへ写さず、
  buildingTypeなしの条件に進む。未知styleも推測で別styleへ写さない。

| 役割 | 集計対象部位（roleParts） |
|---|---|
| foundation | plinth, stairs_entry |
| wall_main | wall_main, gable_wall |
| wall_secondary | wall_secondary |
| frame | column, beam, half_timber |
| roof | roof_surface |
| trim | eave, ridge, gable_trim, cornice, band_course |
| window_frame | window_frame |
| floor | floor_visible |
| path | path, path_natural |
| accent | accent_other |

同一観測を同一役割へ二重加算しない。floorとroofは区別する。
pairの両端が一対多で曖昧な観測は直積にせず、生成側で除外する。
材質語彙はstone/brick/wood/plaster/terracotta/concrete/metal/glass/soil/sand/
plant/wool/ice_snow/ore/mineral/light/liquid/otherの18語。

## 条件と分布

whenは次の4形だけを許す。キー省略は無条件を表し、null・空文字・`*` は使わない。

```text
{style, buildingType, biomeLike} → {style, biomeLike} → {style} → {}
```

読み手は役割/pairごとに最初の値を採用し、欠損分だけ親へ進み、採用条件を記録する。
検証API自体は検索・抽選・フォールバックを実行しない。

| 配列 | 行の構造 |
|---|---|
| palettePrior | `{when, roles: {役割: {oklab: {L, a, b}, n, subjects, nEff, support?}}}` |
| materialPrior | `{when, roles: {役割: {材質: 確率}}}` |
| relationRule | `{when, pair: [first, second], dL, dC, dh: {origin, quantiles}, n, subjects, nEff, support?}` |
| accentRule | `{when, role, maxAreaRatio}` |

rolesは1役割以上。OKLab各成分・dL・dC・dh.quantilesは5/25/50/75/95%の
有限な非減少5数列。OKLabの範囲はLが[0,1]、a/bが[-0.4,0.4]。
成分の周辺分布であり、観測色ベクトルの組や共分散は保証しない。

pairは異なる2役割。差はsecond−first、C=hypot(a,b)、h=atan2(b,a)のラジアン。
dLは[-1,1]、dCは[-sqrt(0.32),sqrt(0.32)]。
dh.originは[-π,π)、quantilesはoriginからの符号付き差[-π,π]。
読み手はwrap(h_second−h_first−origin)を分位区間と比較する。
例えばorigin=3.1の周囲は、±πをまたいでも小さい符号付き差として表す。
originの算出・無彩色観測の除外は固定snapshot/生成runから追跡できるようにする。

材質確率は有限・非負、少なくとも1値が正、和は1±1e-6。
maxAreaRatioは[0,1]。同条件・同役割/pairは同内容でも別内容でも重複を拒否する。
同条件に逆向きpairを併記して競合させない。別役割は同じwhenの別行へ記載できる。

## 支持と由来

nは当該役割/pairへ直接寄与した適格な重複除外済み入力画像数、subjectsは
確定subjectの異なり数、nEffはsubject重みの実効標本数。
n/subjectsは非負整数、`0 <= nEff <= subjects <= n` を満たす。
各役割/pairのnはprovenance.inputs以下、subjectsはprovenance.subjects以下とする。
いずれも同じ入力集合の画像数・確定subject数であり、役割間の和は比較しない。
部位数・平滑化借用分をnへ加算しない。

直接支持はsupport省略または `{status: "direct"}`。
0支持の分布は3支持値を0とし、`{status: "smoothed", sourceWhen: {...}}` を付ける。
sourceWhenは同じ配布物・同役割または同じ向きのpairの厳密な上位条件へ解決する。
段を飛ばす親参照も許す。条件が必ず減るため循環は成立しない。
materialPriorの支持範囲は、固定レポートをprovenanceのsnapshot/生成runから参照する。

provenanceは以下を必須とする。識別子・出典文字列は空にしない。

- dataLicense: 同梱DATA_LICENSEの識別子。ファイル実在・D15適格性の確認は同梱工程で行う。
- inputs/subjects: 全体の重複除外後件数。subjectsはinputs以下。役割nの和とは比較しない。
- licenses: ライセンス識別子から画像件数へのobject。
- attributions: source/author/license/urlを持つ配列。licenseはlicensesに存在すること。
  入力が1件以上ならlicensesとattributionsは空にしない。
- snapshot: idと64桁小文字16進sha256。
- generator: repositoryと40桁小文字16進commit。

licensesの分母・複数ライセンスの採用規則、materialPrior/accentの観測由来は
固定snapshot/生成runから追跡する。入力0・空帰属の自作fixtureは許すが、
実同梱受入れの代わりには使わない。`tests/data/colorpack-v1.json` は数値だけの人工データであり、
snapshotのゼロhashとgeneratorのダミーcommitは実artifactを指さない。

## 検証API

```python
from mcblueprint.colorpack import parse_colorpack, validate_colorpack

# JSON入口。重複キーも検出する。
pack = parse_colorpack(json_text)
# 既にデコードされたMapping。重複キーの情報は残っていない。
pack = validate_colorpack(raw_mapping)
```

両APIはI/O・通信を行わず、構造検証の後に分位点順序・確率和・重複・支持・参照整合を検証する。
不正なら `$.palettePrior[0].roles.roof.oklab.L` などのJSONパス付きBlueprintErrorを送出する。
JSON構文エラーはルート `$` と行・列を示す。整数の桁数制限による読込エラーもルート `$` を示す。
JSON入力のNaN/Infinity、指数や材質確率の加算によるoverflowも拒否する。

戻り値ColorPackはformat_version/pack_version/taxonomy_version/vocabulary_version、
palette_prior/material_prior/relation_rule/accent_rule、provenanceを持つ。
配列はtuple、objectは読み取り専用Mappingとして保持し、入力から独立する。
後続の読込処理はparse_colorpackを使い、検証済みColorPackをpipelineへ渡す。
読取OSError・lock/hash/DATA_LICENSEの確認と実観測の品質保証は本APIの外で行う。

## 同梱読み込みと版固定

`load_colorpack() -> LoadedColorPack | None` は同梱resourceだけを読み込む。
`src/mcblueprint/data/lock.json` の `colorpack` はrepository・40桁commit・
`packVersion`・`taxonomyVersion`・`vocabularyVersion` と、`sha256` object内の
`pack`・`license`・`vocabulary` の各SHA-256を固定する。実パックとDATA_LICENSEは
まだ同梱しないため、現行lockにcolorpack項目がなく関連ファイルもない場合はNoneとなる。

ロック対象は `data/colorpack/colorpack.json`、`data/colorpack/DATA_LICENSE`、
`data/colorpack-vocabulary.json`。三つ全てが存在し、hashと各版がlockおよび相互に
一致した後、パックを `parse_colorpack` で検証する。完全不在以外の欠損・hash・版・
JSON不整合はBlueprintError、resourceの読み取りI/O失敗はOSErrorとして伝播する。
戻り値は検証済みColorPack、3版、各内容hashを保持する。

## Materializeでの選択

`materialize(..., colorpack=pack, building_type="house")` に検証済みColorPackを渡す。
内部でパックを読み込まず、既定の `None` は従来のD28（様式材質とblockdata色）を使う。
makeではpipelineが一度だけload_colorpackを呼び、archetype・style・biomeとともに渡す。

条件は公開対応表に写し、`{style, buildingType, biomeLike}` → `{style, biomeLike}` →
`{style}` → `{}` の順で役割・pairごとに探索する。未知の建物種別・biomeは条件から除く。
支持数で再平滑化せず、平滑化済み分布はその値とsupportの親参照を保持する。
材質は様式候補と正確率の共通集合を安定順で再正規化して抽選し、共通候補がない条件は
親へ進む。最後まで分布がなければその軸だけD28を使う。

優先順は固定ID、明示材質・明示色、パック、D28とし、色と材質は独立に選ぶ。
固定IDの状態・必要形状・歩行条件は従来どおり検証し、代用品への変更を行わずmixを0にする。
パック色は用途付き導出seedから `q=0.5+(u-0.5)*0.9*variation` を選び、
5/25/50/75/95%の間を同じqでL/a/bそれぞれ線形補間する。variation=0は中央値。
a/bには `0.5+saturation`、accent役割にはさらに `1+accent` を掛け、明示色には掛けない。

関係は選んだ実ブロックの用途面の中央値で評価する。差はpairのsecond−firstとし、
dL/dCの5〜95%区間を中心からそれぞれ `0.5+contrast`、`0.5+saturation` 倍する。
dhは `wrap(h_second-h_first-origin)` を比較し、片方の彩度が0.02以下ならdhだけ不適用。
パック規則のない既存pairはD28を使う。最大20試行し、パック関係を使う場合は違反件数、
区間幅で正規化した違反量の和、安定ID順で最小解を選ぶ。幅0は指標の固定範囲で正規化する。
固定IDも検査するが再抽選しない。解なしは `palette_relation` 診断として残す。

MaterializeResultの既存フィールドに加え、`selection_sources` は役割ごとの
`color`・`material`・面積上限規則の対象役割の `maxAreaRatio` の選択元を持つ。`source` は
`pack` / `d28` / `explicit` / `fixed_id`、packには `when` と、該当すれば
`supportSourceWhen` を付ける。`relation_checks` は最終解の関係検査の選択元、pair、
指標ごとの適用の有無を持ち、pack/d28ともに実値・区間または不適用理由を記録する。
D28のwall_mainとroof/frameは絶対明度差、wall_mainとwall_secondaryは絶対彩度差と
円周色相差を記録する。無彩色でdhを除外した場合は `applied: false, reason: "achromatic"`
とし、適用可能な指標の適合数とは区別する。
`max_area_ratios[role]` には `accentRule.role` ごとのパック上限×accentを渡す。
accentの規則欠損時は従来の0.08×accentを使い、他の役割へ既定上限は追加しない。
実ブロック色の欠損は不適用理由と関係違反を残し、合格扱いにしない。

## 生成・評価・再開への接続

パック使用時のrunの `colorpack` とBlueprint checkpointのcontextは、3版と
パック・ライセンス・語彙の実内容hash、`selectionSources`、`relationChecks`、
`maxAreaRatios`、`attempts` を保持する。各採用条件・親分布・欠損理由は
Materializeの記録をそのまま使う。統計値を使用した結果の保証とは区別する。

`metrics.palette_relation` は適合数/適用可能数とpack/d28の内訳を持つ。
無彩色によるdh除外は `excluded`、色欠損は `missing` として分ける。
pack規則ゼロは `value: null, status: n/a`。D28適合をpack適合へ加算しない。
0.9は内部目標であり、個々の関係違反があれば品質FAILを維持する。

`metrics.accent_area_ratio` はWeather後の非air coreの役割セルを建物root別に数える。
建物内の全役割セルが分母、対象roleセルが分子。haloを数えず、建物ごとの超過を
全体平均で隠さない。上限との等値は許し、超過は品質FAIL（終了3）として成果を保存する。
計画建物の役割セルを測れない場合もFAIL、対象建物がない場合だけn/aとする。

配色パック・DATA_LICENSE・語彙の版と実内容hashをenvironmentの `colorpack` と
`dataHash` に含める。resume、apply-edits、viewer再生成は同じenvironmentを照合し、
同じ版の内容変更も拒否する。scene/editsのschema版は変えない。
破損パックは計画・生成前に拒否し、既存成果を保持する。
パック完全不在では従来のD28経路を使い、配色用のrun項目・評価項目を追加しない。
