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

## 統計受入れ実行器（#262）

`scripts/evaluate_colorpack.py` は明示パスのパックと固定計画JSONを読み、
全4版×schem/litematicの読戻しを行う。既存のparse_colorpack、Materialize、
用途面色変換、Weatherとexporterを再利用し、新しいruntime依存は追加しない。
手計算サンプラ試験は `tests/test_colorpack_acceptance.py` にある。

Python 3.12の開発venvで、コマンド記録規約に従って実行する。

```powershell
python scripts/evaluate_colorpack.py `
  --pack tests/data/colorpack/acceptance.json `
  --plan tests/data/colorpack/acceptance-plan.json `
  --output benchmarks/.tmp/colorpack-report.json
```

計画はsynthetic、seeds（重複のない整数集合）、mix、weatherAgeと、
conditions（style/buildingType/biome/rolesの配列）を持つ。
添付のダミー計画はseed=0〜255、mix=0/0.3、Weather age=0/既定0.2。
variation=1、contrast=saturation=0.5、accent=1、明示色/材質なしを固定する。
閾値はスクリプトのGATESに初期値を固定し、同じ系列の結果で変更しない。
synthetic=falseの正式実行では、seed集合0〜255、mix集合0/0.3、Weather age集合0/0.2との
一致を生成前に検査し、不足・重複・値の変更を拒否する。縮小系列はsynthetic=trueだけで使う。

生成開始前にパック・計画・blockdata実内容・語彙・engine・実行器のhashと
全条件の支持一覧をレポートへ記録し、各系列の終了時に追記する。
completed=trueになって初めて全系列の測定完了となる。
対象は直接支持subjects>=10かつnEff>=10の役割。低支持・欠損・候補不足を別記する。
色/材質のselectionSourcesがpackでなければ例外にし、D28へ戻った結果を測定しない。
採用whenと親分布の記録は各seedのselectionSourcesへ残す。

条件のscopeを省略またはrolesにするとfull形状の役割単位Materializeプローブとなる。
scope=buildingとbuildspec（例 `{"scale":"tiny"}`）を指定すると、既存のResolve・
Site/Massing/Detail・Materialize・Weatherを通した住宅/塔の実建築を測定する。
style/buildingType/biome/version/seedと配色設定は固定計画から適用する。
rolesにはその建築が実際に使う役割を指定し、使わない役割は別のroles条件で測定する。
明示色/材質は受け付けない。建築結果と役割単位結果はscopeで区別する。
様式の候補材質と正確率の共通集合に条件付けた期待確率に対して、関係再選択前後の
材質選択率と `max(0.05, 3*sqrt(p*(1-p)/seed数))` を報告する。256seedが正式な内部系列。
候補材質に有効familyがない場合は候補不足として残し、確率を補造しない。

最終Weather後の非air coreで、役割用途面の中央値をセル面積で重み付けした代表色を測る。
そのseed集合の5/25/50/75/95%点、材質面積比、役割面積比、関係のΔL/ΔC/Δhを残す。
同じ抽選目標色を、同じ材質で必要形状を持つ当該版のfull familyの用途面中央値へOKLab距離で
最近傍投影し、実現可能な分布も別に報告する。同距離は安定ID順に解決する。
元の統計の中央値への色差<=0.08と投影分布への色差<=0.03をそれぞれ判定し、
投影側だけの合格ではpassedにならない。候補不足・面色欠損も合格扱いしない。

関係は最終代表色からpack規則だけで再計算し、適用数・適合数・割合（内部目標0.9）、
無彩色・低支持等の除外、規則なしを分離する。D28の適合数は加算しない。
明示面積上限の違反件数、同入力/seedの再生成一致、両形式の読戻し一致も記録する。
配置の比較はairを除外し、blockdataの既定状態を補完する。同seed再生成は世界座標で
比較し、形式間は非air最小座標を原点へ移して、形式固有のoffset保存の差を除く。
人工パックは全値が架空なので数値ゲートの合否は問わない。
レポートのgAcceptanceEvidenceは常にfalseで、単独でGの完成を宣言する資料にはしない。

## 一時配布物の検証

`scripts/verify_colorpack_distribution.py` は実リポジトリからhatchlingでsdistを作り、
その全収録ファイルを一時領域へ展開した中だけにパック・DATA_LICENSE・lock項目を注入する。
実作業ツリーのdata/lock.jsonや既定データを書き換えない。
docs・tests・ignore規則を含む実際のbackend選択を使い、再ビルドしたsdistが元の収録範囲と
注入した2ファイルの和に一致すること、必要ファイルの内容hashを検査する。
配布設定は元のままとし、評価記録が参照するリポジトリ自身のPNG等も保持する。
wheel/sdistの検査ではjarと、Minecraft名前空間（assets/minecraft）のtextures・models・
blockstatesを拒否する。評価画像をMojangの生テクスチャ・モデルと同一視しない。

ネットワークが使える準備段階で、実行環境に対応した依存・build用wheelを取得する。
続くビルド・クリーンvenvへのwheel/sdistインストールはno-indexで行い、
読込/makeではsocket接続も拒否する。非editableのインストール先を確認し、
`--plan`のconditionsに指定したstyle/buildingType/biome/rolesで、全4版×両形式の
pack使用と成果出力を確かめる。配布スモークの計画は統計用のseed/mix系列とは独立である。
確認軸はaxes（color/materialの配列、既定color）で指定する。modernの既存プリセットは
材質を明示指定するため、材質priorの使用確認が必要な条件だけmaterialも選ぶ。
選択された役割・軸がpack以外へ戻った場合は失敗する。
面積上限に掛けるaccentは固定計画と同じ1にし、makeの品質終了コード0を確認する。
このスモークの成功を実データの統計的適合の合格とは扱わない。

```powershell
python -m pip install build
python -m pip download --dest benchmarks/.tmp/colorpack-wheelhouse `
  numpy pyfastnoiselite==0.0.7 nbtlib jsonschema PyYAML hatchling
python scripts/verify_colorpack_distribution.py `
  --synthetic `
  --pack tests/data/colorpack/modern-only.json `
  --license tests/data/colorpack/DATA_LICENSE `
  --plan tests/data/colorpack/distribution-plan.json `
  --wheelhouse benchmarks/.tmp/colorpack-wheelhouse `
  --venv-root ../.venv/colorpack-distribution `
  --output benchmarks/.tmp/colorpack-distribution
```

outputは未使用の検証先を指定する。distribution.json、各導入環境のsmoke.json、
一時配布物をローカル証跡として保全し、ダミー入りのwheel/sdistを公開しない。
添付の配布用計画はmodern限定・frame限定の人工パックを使い、wall_mainやmedievalの
支持がなくても指定した条件・役割で確認できることを検証する。実パックでは支持範囲に
合わせてパック・ライセンス・計画を差し替える。

## #231に残る実パックの受入れ

1. C4〜C6の確定artifact・M3固定snapshotの支持範囲レポートを受領し、材質prior・accentの
   観測由来、版、生成commit、snapshot/hashを照合する。D15に従いSA/NC等の除外・
   DATA_LICENSE本文・帰属を確認する。fixtureの支持数で代用しない。
2. 実パックとDATA_LICENSEをdata/colorpackへ同梱し、data/lock.jsonに生成元の確定commit、
   pack/taxonomy/vocabulary版とpack/license/vocabularyのSHA-256を固定する。
3. 支持のある全条件・役割と除外理由を計画へ記録し、パック/blockdata/engine/hashとともに
   seed=0〜255の計画を生成前に確定する。synthetic=falseにし、上記実行器のpack/planを
   差し替える。元統計・実現可能分布への未達と再選択トレードオフを残し、閾値で隠さない。
4. 住宅/塔で実際に使う条件はscope=buildingで最終coreの同じ指標を測定し、make/build/resumeと
   正規化配置の一致を確認する。上の役割プローブと配布smokeだけで統合適合を宣言しない。
   未対応の地形等は役割単位の結果をHの接続試験へ引き継ぐ。
5. 実データ入りのwheel/sdistでもクリーンなオフライン導入・make・同梱ファイルを確認し、
   実パックの配布検証では--syntheticを外し、支持不足・候補不足・未解決の乖離をメインへ返す。
   実配色の判定とG完了は#231で行う。
