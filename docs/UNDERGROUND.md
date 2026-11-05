# 洞窟・地下都市の生成

`mcblueprint spec-template cave` は洞窟単独、`mcblueprint spec-template underground_city`
は中世の地下都市の雛形を返す。地下都市の雛形はhuge、洞窟はmedium。
`make` は通常の計画・Materialize・領域生成・Weather・Critic・出力を使用する。

```yaml
minecraftVersion: "1.21.11"
archetype: underground_city
style: medieval
scale: huge
seed: 42
site:
  terrain: cave
  biome: plains
features:
  buildings: 8
  entrances: 1
  supports: true
  lights: true
interior: minimal
```

## 対応条件

- medieval、生成地形none/flat/caveに対応する。既存地形ファイルは対象外。
- 主空洞と入口の楕円体列は`terrain.caves=0`でも生成する。cavesは追加枝洞量で、
  枝洞の偶然の形状を地下都市の床や接続保証に使わない。
- 入口は西・東・北・南の順で1〜4個。全入口と各棟の入口・階到着点を接続グラフへ記録し、
  完成spoolの歩行検査で到達を判定する。
  各入口から生成範囲内の実地表へ、両端に踊り場を持つ階段を設ける。
  地表到達点もグラフへ記録し、地下内の接続だけでは合格にしない。
- 同梱`medieval.underground_city`の区画寸法・道路幅・床/天井余裕を使う。
  各楕円体の床・天井の和から乾いた床とfloorClearanceの下限の歩行頭上を確保する。
  共通床は主空洞の中心高からfloorClearanceの下限を引いた高さを優先し、
  水位・区画・屋根・全入口と各棟への接続・地表階段を満たす床高を探索する。
  例えばsize `[64,40,64]` の洞窟は、接続できない床高6を避けて床高7を選ぶ。
- 区画には一階・幅9・奥行9の切妻住宅を配置する。`elements.house#1…`で既存の住宅
  パラメータを変更できる（angleは0固定）が、軒・装飾を含む実外形と
  ceilingClearanceの下限を検査する。
  収まらない棟数・寸法、天井不足、水没は拒否し、指定棟数を減らさない。
  mediumの主空洞には8棟を収容できないため、8棟の固定課題にはhugeを使う。
- 地下床の盛土は主空洞内に置き、地表階段には必要な支持列と掘削を計画する。
  街路・入口経路は棟外形を避け、入口前の接続列だけを
  通す。街路・入口経路のair予約高はfloorClearanceの下限、棟外形内の戸口への
  接続部分は既存の柱・梁を維持する2ブロック。経路の床とairはcirculation予約、
  床から天井までの支柱と、その直下・直上の接続岩は同じownerのstructure予約。
  接続岩はstoneを保ち、Criticも両端を含む期待柱として検査する。
  洞窟単独でも柱の欠損は構造エラーにし、外壁を持たない指標だけn/aとする。
- 支柱と光源は街路・棟から離して配置する。光源は全同梱版にある具体IDのglowstone。
  入口と全通路・区画を確定してから、乾燥床の候補を安定順で探索する。
  cave単独でも支柱・光源を配置し、地下都市ではsupports/lightsで省略できる。

## 地下床と再開

Siteは密度計画の地表場と、Weather用の地下床基準場を分ける。Weatherへは空洞の
最下airの直下を基準とするheightfieldを渡し、造成列・通路列は確定した床高にする。
地表階段の列は各段の路面高を基準とする。
建物・屋根・地表の最高点を地下の地面としない。水距離は従来どおり完成前の
領域snapshotのボクセル距離から計算する。

床基準場と主空洞の形状・hashはcheckpointに保存され、emit/resumeでも同じ計画を使う。
外部形式v1・内部計画v2と同梱layout/lockは変更しない。
内部計画v2の地下計画には接続岩を含む`supports`を記録する。
この期待情報がない修正前の地下checkpointは、BuildSpecから再生成する。
地下専用の配色パック条件と性能の最終判定は後続Issueで検証する。
