# 二棟接続の固定確認

異幅・異階数・塔・L/T/中庭型・床高差は [building_junction](OPERATIONS.md#building_junction)
を使う。従来の fixture と制限は以下のとおり維持する。
新接合は `tests/test_operations_building_junction.py` と `tests/test_junction_planning.py` で
4方向回転、core分割、歩行、owner、外壁・梁・屋根欠損、emit/resume を検証する。
固定住宅・塔の4種hash比較は `evaluation/records/junction-239-baseline.json` に記録する。

入力契約は [OPERATIONS.md の join_buildings](OPERATIONS.md#join_buildings)。この機能は共有面と内部開口の責務を持つ。`joinRoof` 省略時の固定対照をこの文書で扱い、全幅妻面の限定屋根接合は [JOINED_ROOFS.md](JOINED_ROOFS.md) を参照する。

## fixture と比較

- `tests/data/joined_buildings.json`: 幅7・奥行10・二階建て二棟。先頭は(0,0,0)、二棟目は(6,0,0)、共有面はx=6、z=0..9、y=1..8。両階のz=7..8に幅2・高さ2の内部開口。
- `tests/data/joined_buildings_independent.json`: 材料・配置・寸法が同じ二棟を通常buildingで個別生成する対照。共有面を重複して書き、内部開口を持たない。見かけの壁厚だけでなく共有セルの書込み回数と通行を比較する。

```bash
mcblueprint validate tests/data/joined_buildings.json
mcblueprint build tests/data/joined_buildings.json -o output/joined_buildings.schem
mcblueprint preview tests/data/joined_buildings.json -o preview/joined --views isometric,north,east,top --layer 1 --layer 2 --layer 5 --layer 6 --grid 1 --scale 16
```

対照も同じseed=0、scale=16、view/断面で実行する。stairs/slab対応の比較画像は #100 の確定実装を使う。全体外観は独立した屋根を維持した状態として比較し、水平断面y=1/2とy=5/6で共有壁の角、開口、両棟の足元・頭上空間を確認する。画像だけで実機歩行成功としない。

## 通行の端点

全座標はBlueprint配置座標。貼り付け時にoriginと貼り付け地点から変換する。

| 名称 | 足元セル |
|---|---|
| 先頭棟入口内側 | (3,1,1) |
| 二棟目入口内側 | (9,1,1) |
| 先頭棟2階到着 | (1,5,6) |
| 二棟目2階到着 | (7,5,6) |
| 1階共有開口 | (6,1,7) |
| 2階共有開口 | (6,5,7) |

各階で両棟の端点から開口へ、足元空気・頭上空気・直下床材を持つ水平経路があることを独立した幅優先探索で検証する。取り込み済みのbuilding headroom3修正に合わせて階段予約域の上端もfloorHeight+3へ追随した。#106確定commit `0e0ff92337cbaf155c400012f1ad8509f15e2b90` のAPIを外部読取利用し、両棟の入口→各階到着、両階から共有開口、相手棟の2階への昇降を含む12経路と、外側90/180/270度回転・平行移動後の昇降6経路、合計18経路が限定モデルでreachableとなった。同じ二棟の個別配置対照では棟間の経路はblocked。コードコピーや#106の取り込みは行っていない。実機歩行の成功とは区別する。

## 制限と未実施

共有面に窓・外装を生成せず、非共有の同じ方角の壁・窓は維持する。階段は土台・頭上・到着を包む予約AABBを用い、厳密なブロックモデル衝突を推定しない。異階数は、独立屋根が高い棟の非共有外壁・床へ干渉するため、1+2/2+1とも入力時に拒否する。屋根を切除したり壁を事後に戻したりして成功扱いにしない。後続の屋根接合が異階数を解消するとは仮定しない。

Minecraft 1.21.11で新規確認ワールドへの配置許可はあるが、実機配置・歩行・撮影は専任担当へ集約しており未実施。撮影時は #94 の固定視点契約に従い、版・貼付座標・視点位置と向き・描画距離・リソースパックを記録する。生成画像と自動幾何検証を実機検証の代わりにしない。
