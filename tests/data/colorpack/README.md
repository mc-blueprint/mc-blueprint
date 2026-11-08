# 人工配色パック

`acceptance.json` は `../colorpack-v1.json` を基にした数値だけの自作fixture。
色・材質確率・支持数・由来・hash・commitはすべて架空で、実観測の統計的意味を持たない。
subjects/nEff=16は実行器の対象選定を試すための値であり、標本の存在を表さない。
trimだけを低支持にして、除外理由も確認する。屋根の面積上限も架空である。

`acceptance-plan.json` は seed=0〜255、mix=0/0.3、Weather age=0/0.2 を生成前に
固定する。medieval/house/plainsのfull形状の役割プローブであり、建築統計の合否ではない。
ダミーの数値ゲートの合否は問わず、全項目を計算できることを確認する。

パック・このDATA_LICENSEを通常の配布物や既定データへコピーしない。
配布検証スクリプトが作る一時ビルド内だけで同梱経路を確認する。
実パックの受領・実データのlock固定・Gの合格判定は #231 に残る。

`modern-only.json` はmodernのframeだけに架空の色・材質priorを持つ配布用fixture。
`distribution-plan.json` は住宅/塔のmodern・plains・frameを指定する。
medievalやwall_mainの支持を持たないパックでも、支持条件を指定して配布検証できることを
確認する。値と支持数はすべて架空であり、実観測を表さない。
