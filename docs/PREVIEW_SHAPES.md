# stairs・slab プレビューの比較

`isometric` は通常の Minecraft stairs と slab を半ブロック単位で描く。色は入力版の blockdata の面色中央値（欠損時は [補完規則](ARCHITECTURE.md#95-previewpreviewpy)）を使い、テクスチャ、光伝播、透過、任意ブロックモデルは扱わない。他の形状や独自名前空間、不明な形状状態値は立方体近似になる。`top`・立面図・水平断面は従来のブロック単位の図である。

## 固定 fixture

`tests/data/preview_shapes.json` は Minecraft Java Edition 1.21.11 用の固定入力。形状名を省略せず、ゲーム内で隣接更新により決まる stairs の角も明示する。

- 単体 stairs: x=0,3,6,9,12 の列が順に `straight`、`inner_left`、`inner_right`、`outer_left`、`outer_right`。z=0,3,6,9 の行が `bottom` の north/east/south/west、z=12,15,18,21 が `top` の同じ順。いずれも y=0。
- 単体 slab: z=24、x=0,3,6 に `bottom`、`top`、`double`。
- 屋根: x=17..25、z=-1..7。通常 stairs の段・角・軒と、中央の bottom slab の棟を石壁の上に置く。
- 接触: z=12、x=18,21,24 の金ブロックに、右隣と真上から bottom/top/double slab を接触させる。覆われる半分だけ隠れ、残りの面が見えることを確認する。

## 画像の再生成

リポジトリの開発環境で次を実行する。出力先 `preview/` は Git 管理外。

```powershell
python -m mcblueprint validate tests/data/preview_shapes.json
python -m mcblueprint preview tests/data/preview_shapes.json -o preview/issue-100 --views isometric --scale 20 --grid 3
python -m mcblueprint preview tests/data/preview_shapes.json -o preview/issue-100 --views top,north,east --layers all --scale 8 --grid 3
python -m mcblueprint build tests/data/preview_shapes.json -o output/preview_shapes.schem
```

stairs の高段は facing 側。inner は facing 側と左右側の和集合、outer は積集合、top は上下反転として比較する。左右は facing を向いたときの左右である。隣接更新から shape を推定する機能はないため、ゲーム内の比較では貼り付け後の状態が入力と一致することも確認する。

## 実機との比較記録

実機撮影は Issue #94 の固定視点手順（前提 commit `4d8568f320fd539625a2396285ebf9179f8a63e5` の `docs/EVALUATION.md`）を前提とする。この変更は同 commit の内容を取り込まない。撮影対象のワールドと配置場所の許可を得てから、同じ fixture を配置する。外観 isometric/north/east/top と必要な水平断面を揃え、Minecraft 版・配置座標・視点位置と向き・描画距離・リソースパックを記録する。南東上空から屋根・軒・角・棟・接触部を撮り、向き・上下・内外角・空隙・隠面を比較する。ゲーム内の遠近投影や陰影とのピクセル一致は求めず、形状と接続の一致を確認する。

Issue #100 の自動確認では占有セル、回転と x/y/z 鏡映、隣接 slab の接触面積、負座標、小スケール、独立したレイ交差による面の前後関係、立方体の既存ピクセル、既存ビューの回帰を検査する。形状一覧と屋根比較の生成画像を目視確認した。実機比較は未実施であり、この自動確認や生成画像を実機確認済みとして扱わない。


## 後続のユーザー照合（2026-10-20）

Minecraft Java Edition 1.21.11の確認用ワールドで、配布した `manual-review-20261020` のREADMEに従うstairs／slabと屋根の軒・角・棟を含む実機表示とpreviewの比較は、すべて正常とのユーザー報告を受けた。上記の実装時点の未実施記録とは別の後続確認である。

比較画像ファイルは未提出であり、AIが新たに撮影した、または提出画像を照合したとは扱わない。この報告は形状照合の範囲に限り、テクスチャ・照明・任意モデルやpixel一致を保証しない。
