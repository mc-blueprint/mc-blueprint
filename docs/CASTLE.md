# 城郭の生成

`mcblueprint spec-template castle` の雛形を使う。`style: medieval`、
`scale: medium` 以上（`size` は64×32×64以上）を指定する。
`site.terrain` は none / flat / hill / mountain / plateau / valley / island。

`features` は `curtain_wall`、`keep`、`gatehouse`、`towers`、`moat`。
主棟を含む3棟（keep=falseでは付属棟2棟）を既存の住宅として計画し、
異階数接合を `building_junction` に渡す。塔は要求数の角塔を計画し、
収容できなければ塔数を減らさず計画エラーにする。
塔の候補は四辺から選び、間隔の下限と門の占有を使って収容数を判定する。
角塔の入口は城内側へ向け、接続路が城壁を横切らないよう占有列を避けて探索する。
共通街道と門の開口幅にはlayoutの `roadWidth` の下限を使う。
棟・塔への小道は幅1の歩行接続とする。
接合面は両棟の実際に重なる高さ・奥行きから導出し、通路は柱・梁・階段を避ける。

同梱 `medieval.castle` の敷地規則と既存地形場を使い、高さと造成量から
敷地候補を採点する。城壁は塔の輪郭と門を合わせて閉じる。
堀を横切る街道を門へ接続し、棟・塔の入口と計画階まで完成出力を歩行評価する。
棟ごとの構造・外壁・配色・装飾品質は従来のCriticで検証する。
堀底と内外の岸は元地形まで支え、水を浮かせずに囲う。
敷地の接続路は敷地の要素IDで保持し、棟別配色の面積分母へ混ぜない。

内部Siteの `groundHeight: {type: heightfield, field: ...}` は造成前の
`TerrainField.to_dict()` を保持する。Weatherはその元地形高を使う。
checkpointにも場のhashを保存し、再開時に検証する。外部BuildSpec/sceneはv1のまま。

塔と棟のIDは `tower#1…`、`house#1…`。既存の要素編集契約でパラメータを検証し、
接合やsizeから外れる寸法は拒否する。overridesも既存の予約保護を使う。
castleの既存地形ファイルは未対応。
固定課題は `evaluation/cases/large-scale.json` の castle-medieval を使う。
他の固定課題の入力・期待hashは変更しない。

城壁の材質は既存の共有Materializeへ `wall_main/full` を渡して決める。
同梱blockdataでは `pointed_dripstone` が `material: stone`・`shape: full` のため候補になり、
hill・seed42でも選択される。`wall_main` は歩行役割の完全ブロック制限を受けない。
城郭独自の除外や共有材質処理の変更は加えず、最終配色は#244で扱う。
この材質を使った実ゲームの遮蔽・保水は未検証であり、経路と壁の幾何の反証は通常の石材を固定して行う。

回帰照合は `python scripts/verify_castle_regression.py` で再実行できる。
起点ec5a878のplanning/pipelineと変更のない依存実装を使い、住宅・塔・異階数接合の
4版×両形式について、同じ入力・保存先で配置・指標・構造化診断・sceneのhashを比較する。
結果は `evaluation/records/castle-240-regression.json` に保存する。
