# 妻面同士の切妻屋根接合の固定確認

異幅・直交屋根・異階数の屋根包絡は [building_junction](OPERATIONS.md#building_junction)
で扱う。既存条件に合う `roofPolicy: join` は従来の屋根施工を使い、以下の固定対照を維持する。

入力の正本は [OPERATIONS.md](OPERATIONS.md#join_buildings)。`join_buildings` に `joinRoof:true` を明示すると、対応条件を検証して個別roofの生成を止め、一つの屋根面・棟・軒と両外周の妻壁を生成する。共有壁の内部開口とheadroom3は維持する。

## 固定入力と対照

- `tests/data/joined_roof_odd.json`: 幅7、長さ10/11、二階建て。接続面z=9、屋根棟はbottom slab。最外妻面はz=0/19。
- `tests/data/joined_roof_even.json`: 幅6、長さ8/12、二階建て。接続面z=7、屋根頂部は向かい合うstairs二列。最外妻面はz=0/18。
- 同名の `_independent.json`: 位置・材質・内部開口が同じで、joinRoofだけfalse。個別屋根による内部妻壁と重複した屋根面を対照とする。

先頭棟の外周妻材はstone_bricks、二棟目はbricks。二棟目は180度回転して北中央の主入口を最外周へ向ける。接続面は両棟の南妻面から決まり、外側回転後も座標/状態を対応させる。配列順を逆にしても外周の所有材を維持する。床行の先頭優先は#108と同じ。

```bash
mcblueprint validate tests/data/joined_roof_odd.json --strict
mcblueprint build tests/data/joined_roof_odd.json --strict -o output/joined_roof_odd.schem
mcblueprint preview tests/data/joined_roof_odd.json -o preview/joined-roof-odd --views isometric,north,east,top --layer 1 --layer 4 --layer 5 --layer 9 --layer 10 --scale 16 --grid 1
```

evenと各対照も同じseed=0、view/scale/gridで実行する。stairs/slab対応画像は確定#100を外部読取利用する。全体外観は同じに見える場合があるため、屋根裏断面y=9/10で内部妻壁の有無と、屋根面の生成回数を併せて確認する。通行する居住階の断面y=1/4/5も比較し、屋根裏の除去と居住階の開口を混同しない。

## 検証契約

対応は同幅・同棟軸・同軒高・固定段勾配・同屋根材・全幅妻面接触の二棟だけ。長さは異なってよい。T/L字、部分接触、直交棟、異幅/材/勾配/軒高、三棟以上、自由形状は事前拒否する。屋根材のfacing/half/shapeはbuildingの既存規約どおり自動指定で、自由なroofパラメータを新設しない。

自動検証では奇偶幅、長さ、接続端、各方位、個別180度、外側変換と最大nest、所有元別の妻材、内部妻壁/内軒がない屋根全セル、共有セル一回生成、RoofOperationが一回だけ実行されることを検査する。失敗する配置では既存volumeへ何も書かない。airによる屋根の事後切除や、後から壁を戻す処理はない。

生成したvolumeを、確定#106 commit `8d96174e80ad5571076e167ef556caf61c826397` のcheck_route/check_buildingへ渡し、入口→階到着と共有開口を越えた昇降を限定モデルで照合する。#106側のregistryで新しいjoin JSONを読み込まず、コードコピーや無断mergeも行わない。境界・経路・実際の版・外側変換・source hashを比較記録に残す。

Minecraft1.21.11の新規確認ワールドへの許可はあるが、画面ツールの応答がなく実機配置・歩行・撮影は未実施。一般/熟練者の品質採点と画像込みモデルtoken使用量も未取得であり、画像目視・限定通行モデルをそれらの成功として扱わない。
